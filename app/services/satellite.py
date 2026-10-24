"""Real Sentinel-1 / Sentinel-2 acquisition through the Microsoft Planetary Computer STAC API.

No credentials are required for public read access; a short-lived SAS token is requested per
collection. Everything is read windowed (never whole scenes) and resampled to one bounded grid.

Outputs (written into RASTER_DIR, co-registered on a shared UTM grid):
  S2 composite: bands [NIR (B08), SWIR (B12), cloud/shadow/nodata mask (1 = no clear observation)]
  S1 composite: bands [VV dB, VH dB, nodata mask (1 = no observation)]
"""
import hashlib
import math
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window  # noqa: F401  (kept for adapters needing explicit windows)

from app.core.geometry import to_shape

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
SAS_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token/{collection}"
S2_CLOUD_CLASSES = (0, 1, 3, 8, 9, 10)  # nodata, saturated, cloud shadow, cloud med/high, thin cirrus
GDAL_ENV = {"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR", "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff", "GDAL_HTTP_TIMEOUT": "45", "GDAL_HTTP_MAX_RETRY": "3", "GDAL_HTTP_RETRY_DELAY": "2", "VSI_CACHE": "TRUE"}


class ProviderError(RuntimeError):
    """Explicit provider failure (network, empty catalogue, region too large...)."""


@dataclass
class Grid:
    crs: str
    transform: Any
    width: int
    height: int
    resolution: float


@dataclass
class AcquisitionConfig:
    window_days: int = 30
    max_cloud_cover: float = 60.0
    max_scenes: int = 6
    resolution_m: float = 20.0
    max_pixels: int = 4_000_000


@dataclass
class Composite:
    sensor: str
    path: str | None
    status: str
    scenes: list[dict[str, Any]] = field(default_factory=list)
    valid_pixel_fraction: float = 0.0
    warnings: list[str] = field(default_factory=list)
    processing: dict[str, Any] = field(default_factory=dict)


def make_grid(geometry: dict[str, Any], cfg: AcquisitionConfig) -> Grid:
    minx, miny, maxx, maxy = to_shape(geometry).bounds
    lon, lat = (minx + maxx) / 2, (miny + maxy) / 2
    crs = f"EPSG:{(32600 if lat >= 0 else 32700) + int((lon + 180) // 6) + 1}"
    t = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    xs, ys = zip(*[t.transform(x, y) for x, y in ((minx, miny), (minx, maxy), (maxx, miny), (maxx, maxy))])
    res = cfg.resolution_m
    x0, y1 = math.floor(min(xs) / res) * res, math.ceil(max(ys) / res) * res
    width, height = math.ceil((max(xs) - x0) / res), math.ceil((y1 - min(ys)) / res)
    if width * height > cfg.max_pixels:
        raise ProviderError(f"Region too large: {width}x{height} px at {res} m exceeds {cfg.max_pixels} px limit; use a smaller region")
    if width * height == 0:
        raise ProviderError("Region has zero extent")
    return Grid(crs, from_origin(x0, y1, res, res), width, height, res)


class PlanetaryComputer:
    name = "planetary_computer"

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=60, headers={"User-Agent": "VanaNetra/0.2"})
        self._tokens: dict[str, tuple[str, float]] = {}

    # ---------------------------------------------------------------- catalogue
    def search(self, collection: str, bbox: list[float], start: date, end: date, query: dict[str, Any] | None = None, limit: int = 50) -> list[dict[str, Any]]:
        body = {"collections": [collection], "bbox": bbox, "datetime": f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z", "limit": limit}
        if query:
            body["query"] = query
        last: Exception | None = None
        for attempt in range(3):
            try:
                resp = self.client.post(f"{STAC_URL}/search", json=body)
                resp.raise_for_status()
                return resp.json().get("features", [])
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                time.sleep(1.5 * (attempt + 1))
        raise ProviderError(f"STAC search failed for {collection}: {type(last).__name__}")

    def token(self, collection: str) -> str:
        cached = self._tokens.get(collection)
        if cached and cached[1] > time.time() + 120:
            return cached[0]
        data, last = None, None
        for attempt in range(5):
            try:
                resp = self.client.get(SAS_URL.format(collection=collection))
                resp.raise_for_status()
                data = resp.json()
                break
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                time.sleep(2 * (attempt + 1))
        if data is None:
            raise ProviderError(f"Could not obtain data-access token for {collection}: {type(last).__name__}")
        expiry = datetime.fromisoformat(data["msft:expiry"].replace("Z", "+00:00")).timestamp()
        self._tokens[collection] = (data["token"], expiry)
        return data["token"]

    def signed(self, collection: str, href: str) -> str:
        if collection == "cop-dem-glo-30":  # collection token is rejected by this storage account; use per-asset signing
            last = None
            for attempt in range(3):
                try:
                    r = self.client.get("https://planetarycomputer.microsoft.com/api/sas/v1/sign", params={"href": href})
                    r.raise_for_status()
                    return r.json()["href"]
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    last = exc
                    time.sleep(1.5 * (attempt + 1))
            raise ProviderError(f"Could not sign DEM asset: {type(last).__name__}")
        return f"{href}?{self.token(collection)}"

    # ---------------------------------------------------------------- reading
    def _read_band(self, collection: str, asset: dict[str, Any], grid: Grid, resampling: Resampling, nodata_override: float | None = None) -> np.ma.MaskedArray:
        url = self.signed(collection, asset["href"])
        with rasterio.Env(**GDAL_ENV):
            with rasterio.open(url) as src:
                nodata = nodata_override if nodata_override is not None else src.nodata
                with WarpedVRT(src, crs=grid.crs, transform=grid.transform, width=grid.width, height=grid.height, resampling=resampling, nodata=nodata, src_nodata=(src.nodata if src.nodata is not None else nodata_override), dtype="float32") as vrt:
                    data = vrt.read(1, masked=True)
        return data

    # ---------------------------------------------------------------- Sentinel-2
    def sentinel2_composite(self, geometry: dict[str, Any], centre: date, cfg: AcquisitionConfig, out_path: Path) -> Composite:
        grid = make_grid(geometry, cfg)
        bbox = list(to_shape(geometry).bounds)
        items = self.search("sentinel-2-l2a", bbox, centre - timedelta(days=cfg.window_days), centre + timedelta(days=cfg.window_days), {"eo:cloud_cover": {"lt": cfg.max_cloud_cover}})
        items = sorted(items, key=lambda i: i["properties"].get("eo:cloud_cover", 100))[: cfg.max_scenes]
        comp = Composite("sentinel-2", None, "NO_SCENES", processing={"collection": "sentinel-2-l2a", "bands": ["B08", "B12", "SCL"], "cloud_mask_scl_classes": list(S2_CLOUD_CLASSES), "composite": "median of clear observations", "resolution_m": grid.resolution, "crs": grid.crs, "window_days": cfg.window_days, "max_cloud_cover": cfg.max_cloud_cover})
        if not items:
            comp.warnings.append("No Sentinel-2 L2A scenes in the window below the cloud threshold")
            return comp
        nir_stack, swir_stack = [], []
        for item in items:
            try:
                nir = self._scaled(item, "B08", grid)
                swir = self._scaled(item, "B12", grid)
                scl = self._read_band("sentinel-2-l2a", item["assets"]["SCL"], grid, Resampling.nearest)
            except Exception as exc:  # noqa: BLE001 - a bad scene must not sink the composite
                comp.warnings.append(f"Skipped {item['id']}: {type(exc).__name__}")
                continue
            bad = np.isin(scl.filled(0), S2_CLOUD_CLASSES) | np.ma.getmaskarray(nir) | np.ma.getmaskarray(swir)
            nir_stack.append(np.ma.array(nir.filled(np.nan), mask=bad))
            swir_stack.append(np.ma.array(swir.filled(np.nan), mask=bad))
            p = item["properties"]
            comp.scenes.append({"scene_id": item["id"], "acquired_at": p["datetime"], "cloud_cover_percent": p.get("eo:cloud_cover"), "platform": p.get("platform"), "tile": p.get("s2:mgrs_tile"), "processing_baseline": p.get("s2:processing_baseline"), "bbox": item.get("bbox")})
        if not nir_stack:
            comp.status = "PROVIDER_ERROR"
            comp.warnings.append("All candidate scenes failed to read")
            return comp
        nir_c = np.ma.median(np.ma.stack(nir_stack), axis=0)
        swir_c = np.ma.median(np.ma.stack(swir_stack), axis=0)
        obscured = np.ma.getmaskarray(nir_c) | np.ma.getmaskarray(swir_c)
        comp.valid_pixel_fraction = round(float((~obscured).mean()), 4)
        self._write(out_path, grid, [nir_c.filled(0), swir_c.filled(0), obscured.astype("float32")])
        comp.path, comp.status = str(out_path), "OK"
        return comp

    def _scaled(self, item: dict[str, Any], band: str, grid: Grid) -> np.ma.MaskedArray:
        asset = item["assets"][band]
        meta = (asset.get("raster:bands") or [{}])[0]
        data = self._read_band("sentinel-2-l2a", asset, grid, Resampling.bilinear)
        return np.ma.masked_less_equal(data, 0) * meta.get("scale", 1e-4) + meta.get("offset", 0.0)

    # ---------------------------------------------------------------- baseline forest extent
    def worldcover_forest(self, geometry: dict[str, Any], cfg: AcquisitionConfig, out_path: Path) -> Composite:
        """ESA WorldCover 2021 (10 m) tree-cover class on the shared grid. Band 1: 1 = tree cover, 0 = other; band 2: 1 = no data."""
        grid = make_grid(geometry, cfg)
        bbox = list(to_shape(geometry).bounds)
        items = self.search("esa-worldcover", bbox, date(2021, 1, 1), date(2021, 12, 31))
        comp = Composite("esa-worldcover", None, "NO_SCENES", processing={"collection": "esa-worldcover", "year": 2021, "class": "10 (tree cover)", "resolution_m": grid.resolution, "crs": grid.crs, "note": "2021 extent; clearing between 2021 and the baseline date is handled by requiring baseline vegetation signal"})
        if not items:
            comp.warnings.append("No WorldCover coverage for this region")
            return comp
        mask = None
        for item in items:
            try:
                lc = self._read_band("esa-worldcover", item["assets"]["map"], grid, Resampling.nearest)
            except Exception as exc:  # noqa: BLE001
                comp.warnings.append(f"Skipped {item['id']}: {type(exc).__name__}")
                continue
            comp.scenes.append({"scene_id": item["id"], "acquired_at": item["properties"].get("start_datetime", item["properties"].get("datetime")), "bbox": item.get("bbox")})
            tree = (lc.filled(0) == 10)
            known = ~np.ma.getmaskarray(lc) & (lc.filled(0) > 0)
            mask = (tree, known) if mask is None else (mask[0] | tree, mask[1] | known)
        if mask is None:
            comp.status = "PROVIDER_ERROR"
            return comp
        tree, known = mask
        comp.valid_pixel_fraction = round(float(known.mean()), 4)
        self._write(out_path, grid, [tree.astype("float32"), (~known).astype("float32")])
        comp.path, comp.status = str(out_path), "OK"
        return comp

    def worldcover_tree_fraction(self, box: dict[str, Any], grid: Grid) -> np.ndarray:
        """Tree-cover fraction (0-1) per cell of `grid`, from 10 m WorldCover class 10 averaged over the cell."""
        k = int(min(max(round(grid.resolution / 10), 1), 8))
        fine = Grid(grid.crs, grid.transform * grid.transform.scale(1 / k, 1 / k), grid.width * k, grid.height * k, grid.resolution / k)
        items = self.search("esa-worldcover", list(to_shape(box).bounds), date(2021, 1, 1), date(2021, 12, 31))
        if not items:
            raise ProviderError("No WorldCover coverage for this region")
        tree = np.zeros((fine.height, fine.width), dtype=bool)
        known = np.zeros_like(tree)
        for item in items:
            lc = self._read_band("esa-worldcover", item["assets"]["map"], fine, Resampling.nearest)
            tree |= lc.filled(0) == 10
            known |= (~np.ma.getmaskarray(lc)) & (lc.filled(0) > 0)
        if not known.any():
            raise ProviderError("WorldCover returned no data")
        return tree.reshape(grid.height, k, grid.width, k).mean(axis=(1, 3))

    def worldcover_classes(self, box: dict[str, Any], grid: Grid) -> np.ndarray:
        """ESA WorldCover 2021 class codes (10 tree, 20 shrub, 30 grass, 40 crop, 50 built, 60 bare, 70 snow, 80 water, ...) on `grid`; 0 = unknown."""
        items = self.search("esa-worldcover", list(to_shape(box).bounds), date(2021, 1, 1), date(2021, 12, 31))
        if not items:
            raise ProviderError("No WorldCover coverage for this location")
        out = np.zeros((grid.height, grid.width), dtype="uint8")
        for item in items:
            lc = self._read_band("esa-worldcover", item["assets"]["map"], grid, Resampling.mode if hasattr(Resampling, "mode") else Resampling.nearest)
            vals = np.where(np.ma.getmaskarray(lc), 0, lc.filled(0)).astype("uint8")
            out = np.where(out == 0, vals, out)
        if not out.any():
            raise ProviderError("WorldCover returned no data")
        return out

    # ---------------------------------------------------------------- Sentinel-1
    def sentinel1_composite(self, geometry: dict[str, Any], centre: date, cfg: AcquisitionConfig, out_path: Path, orbit_state: str | None = None) -> Composite:
        grid = make_grid(geometry, cfg)
        bbox = list(to_shape(geometry).bounds)
        items = self.search("sentinel-1-rtc", bbox, centre - timedelta(days=cfg.window_days), centre + timedelta(days=cfg.window_days), {"sar:instrument_mode": {"eq": "IW"}})
        items = [i for i in items if "vv" in i["assets"] and "vh" in i["assets"]]
        comp = Composite("sentinel-1", None, "NO_SCENES", processing={"collection": "sentinel-1-rtc", "product": "radiometrically terrain-corrected gamma0 (linear power) -> dB", "mode": "IW", "polarizations": ["VV", "VH"], "composite": "median in linear power, single orbit direction", "resolution_m": grid.resolution, "crs": grid.crs, "window_days": cfg.window_days})
        if not items:
            comp.warnings.append("No Sentinel-1 RTC scenes in the window")
            return comp
        if orbit_state is None:  # same orbit direction avoids ascending/descending geometry differences
            states = [i["properties"].get("sat:orbit_state") for i in items]
            orbit_state = max(set(states), key=states.count)
        items = [i for i in items if i["properties"].get("sat:orbit_state") == orbit_state][: cfg.max_scenes]
        comp.processing["orbit_state"] = orbit_state
        vv_stack, vh_stack = [], []
        for item in items:
            try:
                vv = self._read_band("sentinel-1-rtc", item["assets"]["vv"], grid, Resampling.bilinear)
                vh = self._read_band("sentinel-1-rtc", item["assets"]["vh"], grid, Resampling.bilinear)
            except Exception as exc:  # noqa: BLE001
                comp.warnings.append(f"Skipped {item['id']}: {type(exc).__name__}")
                continue
            vv_stack.append(np.ma.masked_less_equal(vv, 0))
            vh_stack.append(np.ma.masked_less_equal(vh, 0))
            p = item["properties"]
            comp.scenes.append({"scene_id": item["id"], "acquired_at": p["datetime"], "platform": p.get("platform"), "orbit_state": p.get("sat:orbit_state"), "relative_orbit": p.get("sat:relative_orbit"), "bbox": item.get("bbox")})
        if not vv_stack:
            comp.status = "PROVIDER_ERROR"
            comp.warnings.append("All candidate scenes failed to read")
            return comp
        vv_c = np.ma.median(np.ma.stack(vv_stack), axis=0)
        vh_c = np.ma.median(np.ma.stack(vh_stack), axis=0)
        missing = np.ma.getmaskarray(vv_c) | np.ma.getmaskarray(vh_c)
        comp.valid_pixel_fraction = round(float((~missing).mean()), 4)
        db = lambda a: 10 * np.log10(np.where(np.ma.getmaskarray(a), 1.0, a.filled(1.0)))  # noqa: E731
        self._write(out_path, grid, [db(vv_c), db(vh_c), missing.astype("float32")])
        comp.path, comp.status = str(out_path), "OK"
        return comp

    @staticmethod
    def _write(path: Path, grid: Grid, bands: list[np.ndarray]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(path, "w", driver="GTiff", height=grid.height, width=grid.width, count=len(bands), dtype="float32", crs=grid.crs, transform=grid.transform, compress="deflate") as dst:
            for i, band in enumerate(bands, 1):
                dst.write(band.astype("float32"), i)


def file_checksum(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
