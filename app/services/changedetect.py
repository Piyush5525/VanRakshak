"""Rule-based candidate change generator over co-registered raster pairs.

This is a transparent baseline, not a trained model. Output is always labelled
``candidate_inference``; a trained segmentation model should replace the scoring
step behind the same contract.

Raster contract: 3 bands per file -> [NIR (B8), SWIR (B12), cloud/shadow mask (1 = obscured)],
same CRS, transform and shape for the before and after files.
"""
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import rasterio
from rasterio import features
from rasterio.warp import transform_geom
from scipy import ndimage

from app.core.geometry import geodesic_area_hectares

METHOD = "dNBR-baseline-v1"


@dataclass
class ChangeConfig:
    forest_nbr_min: float = 0.35  # baseline forest-like threshold when NO forest mask is supplied (flagged as weak)
    vegetation_nbr_min: float = 0.15  # with a forest mask: baseline must still show vegetation signal
    dnbr_min: float = 0.25  # NBR drop (before - after) that flags candidate loss
    min_area_ha: float = 0.1
    min_valid_fraction: float = 0.6
    max_pixels: int = 25_000_000
    radar_vh_drop_db: float = 1.5  # VH backscatter decrease treated as supporting evidence
    radar_min_valid_fraction: float = 0.5


def nbr(nir: np.ndarray, swir: np.ndarray) -> np.ndarray:
    nir, swir = nir.astype("float64"), swir.astype("float64")
    denom = nir + swir
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(denom > 0, (nir - swir) / denom, np.nan)


def _read(path: str, max_pixels: int):
    with rasterio.open(path) as src:
        if src.width * src.height > max_pixels:
            raise ValueError(f"Raster exceeds {max_pixels} pixel processing limit")
        if src.count < 3:
            raise ValueError("Raster must have bands [NIR, SWIR, cloud_mask]")
        return src.read([1, 2, 3], masked=True), src.transform, src.crs


def _read_radar(path: str, shape, transform, crs, max_pixels: int):
    with rasterio.open(path) as src:
        if src.count < 3 or src.crs != crs or src.transform != transform or (src.height, src.width) != tuple(shape):
            raise ValueError("Sentinel-1 composite is not co-registered with the optical composite")
        if src.width * src.height > max_pixels:
            raise ValueError("Radar raster exceeds processing limit")
        d = src.read([1, 2, 3])
    return d[1], d[2] > 0  # VH dB, missing mask


def detect_from_rasters(before_path: str, after_path: str, config: ChangeConfig | None = None, before_s1: str | None = None, after_s1: str | None = None, forest_mask_path: str | None = None) -> dict[str, Any]:
    cfg = config or ChangeConfig()
    b, tb, crs_b = _read(before_path, cfg.max_pixels)
    a, ta, crs_a = _read(after_path, cfg.max_pixels)
    if crs_b is None or crs_b != crs_a or tb != ta or b.shape != a.shape:
        raise ValueError("Rasters are not co-registered (CRS, transform, or shape differ) or have no CRS")

    invalid = np.ma.getmaskarray(b[0]) | np.ma.getmaskarray(b[1]) | np.ma.getmaskarray(a[0]) | np.ma.getmaskarray(a[1])
    cloud = (b[2].filled(1) > 0) | (a[2].filled(1) > 0)
    valid = ~(invalid | cloud)
    valid_fraction = float(valid.mean()) if valid.size else 0.0

    base: dict[str, Any] = {"method": METHOD, "claim_type": "candidate_inference", "config": asdict(cfg), "valid_pixel_fraction": round(valid_fraction, 4), "cloud_fraction": round(float(cloud.mean()), 4), "polygons": []}
    if valid_fraction < cfg.min_valid_fraction:
        return {**base, "status": "INSUFFICIENT_EVIDENCE", "reason": f"valid pixel fraction {valid_fraction:.2f} below {cfg.min_valid_fraction}"}

    nbr_b = nbr(b[0].filled(0), b[1].filled(0))
    nbr_a = nbr(a[0].filled(0), a[1].filled(0))
    if forest_mask_path:
        with rasterio.open(forest_mask_path) as fm:
            if fm.crs != crs_b or fm.transform != tb or (fm.height, fm.width) != b.shape[1:]:
                raise ValueError("Forest mask is not co-registered with the optical composite")
            tree = fm.read(1) > 0
        forest_before = valid & tree & (nbr_b >= cfg.vegetation_nbr_min)
        base["baseline_forest"] = {"source": "forest mask raster", "pixels": int(forest_before.sum())}
    else:
        forest_before = valid & (nbr_b >= cfg.forest_nbr_min)
        base["baseline_forest"] = {"source": "NBR threshold only", "warning": "No forest mask supplied; a spectral threshold is a weak forest definition", "pixels": int(forest_before.sum())}
    if not forest_before.any():
        return {**base, "status": "INSUFFICIENT_EVIDENCE", "reason": "empty baseline forest mask"}

    drop = nbr_b - nbr_a
    candidate = forest_before & np.nan_to_num(drop, nan=0.0).__ge__(cfg.dnbr_min)
    candidate = ndimage.binary_opening(candidate, structure=np.ones((2, 2)))
    score = np.clip(np.nan_to_num(drop, nan=0.0), 0, 1)

    radar_drop = radar_ok = None
    if before_s1 and after_s1:
        vh_b, miss_b = _read_radar(before_s1, b.shape[1:], tb, crs_b, cfg.max_pixels)
        vh_a, miss_a = _read_radar(after_s1, b.shape[1:], tb, crs_b, cfg.max_pixels)
        radar_ok = ~(miss_b | miss_a)
        radar_drop = (vh_b - vh_a) >= cfg.radar_vh_drop_db
        base["radar"] = {"used": True, "feature": "VH dB decrease", "threshold_db": cfg.radar_vh_drop_db, "valid_fraction": round(float(radar_ok.mean()), 4)}
    else:
        base["radar"] = {"used": False, "reason": "Sentinel-1 composites not supplied"}

    labeled, _ = ndimage.label(candidate)
    polygons = []
    for geom, value in features.shapes(labeled.astype("int32"), mask=labeled > 0, transform=tb):
        wgs = transform_geom(crs_b, "EPSG:4326", geom)
        area = geodesic_area_hectares(wgs)
        if area < cfg.min_area_ha:
            continue
        pix = labeled == int(value)
        poly = {"geometry": wgs, "area_hectares": round(area, 4), "mean_score": round(float(score[pix].mean()), 4), "score_kind": "uncalibrated_dnbr_magnitude", "evidence_level": "optical_only"}
        if radar_ok is not None:
            valid_px = int((pix & radar_ok).sum())
            frac = float((pix & radar_ok & radar_drop).sum()) / valid_px if valid_px else None
            poly["radar_valid_fraction"] = round(valid_px / int(pix.sum()), 4)
            poly["radar_support_fraction"] = None if frac is None else round(frac, 4)
            if frac is None or poly["radar_valid_fraction"] < cfg.radar_min_valid_fraction:
                poly["evidence_level"] = "optical_only_radar_insufficient"
            else:
                poly["evidence_level"] = "optical_radar_agree" if frac >= 0.5 else "optical_radar_disagree"
        polygons.append(poly)
    return {**base, "status": "CANDIDATES_FOUND" if polygons else "NO_CANDIDATES", "polygons": polygons}
