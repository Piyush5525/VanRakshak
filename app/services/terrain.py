"""Real elevation from the Copernicus 30 m DEM (Planetary Computer `cop-dem-glo-30`, no key).

Grid is in UTM metres, so x = east, y = north: directly usable as a local frame for AR/3D scenes.
"""
import hashlib
import json
import math
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.transform import Affine
from shapely.ops import transform as shp_transform

from app.core.config import get_settings
from app.core.geometry import to_shape
from app.services.acquisition import provider
from app.services.satellite import AcquisitionConfig, Grid, ProviderError, make_grid

SLOPE_SCREEN_DEG = 15.0  # screening convention for erosion susceptibility; not a validated threshold


def elevation_grid(geometry: dict[str, Any], max_dim: int = 96, margin_deg: float = 0.0) -> dict[str, Any]:
    shape = to_shape(geometry)
    minx, miny, maxx, maxy = shape.bounds
    box = {"type": "Polygon", "coordinates": [[[minx - margin_deg, miny - margin_deg], [maxx + margin_deg, miny - margin_deg], [maxx + margin_deg, maxy + margin_deg], [minx - margin_deg, maxy + margin_deg], [minx - margin_deg, miny - margin_deg]]]}
    extent_m = max((maxx - minx + 2 * margin_deg) * 111_320 * math.cos(math.radians((miny + maxy) / 2)), (maxy - miny + 2 * margin_deg) * 110_540)
    res = max(30.0, extent_m / max_dim)
    cfg = AcquisitionConfig(resolution_m=res, max_pixels=4_000_000)
    key = hashlib.sha1(json.dumps([box, res]).encode()).hexdigest()[:20]
    cache = Path(get_settings().raster_dir) / f"dem-{key}.npz"
    grid = make_grid(box, cfg)
    if cache.is_file():
        z = np.load(cache)
        heights = z["heights"]
    else:
        pc = provider()
        items = pc.search("cop-dem-glo-30", [minx - margin_deg, miny - margin_deg, maxx + margin_deg, maxy + margin_deg], date(2010, 1, 1), date(2026, 1, 1))
        if not items:
            raise ProviderError("No Copernicus DEM coverage for this region")
        acc = np.full((grid.height, grid.width), np.nan)
        warnings: list[str] = []
        for item in items:
            try:
                data = pc._read_band("cop-dem-glo-30", item["assets"]["data"], grid, Resampling.bilinear, nodata_override=-32768.0)
            except Exception as exc:  # noqa: BLE001 - one bad neighbouring tile must not sink the DEM
                warnings.append(f"DEM tile {item['id']} skipped: {type(exc).__name__}")
                continue
            arr = data.filled(np.nan)
            acc = np.where(np.isnan(acc), arr, acc)
        if np.isnan(acc).all():
            raise ProviderError("DEM read returned no data")
        heights = acc
        if not warnings:  # only cache complete reads
            cache.parent.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(cache, heights=heights)
    missing = float(np.isnan(heights).mean())
    return {"heights": heights, "missing_fraction": round(missing, 4), "grid": grid, "source": "Copernicus DEM GLO-30 (ESA) via Microsoft Planetary Computer", "resolution_m": grid.resolution}


def slope_degrees(heights: np.ndarray, res: float) -> np.ndarray:
    h = np.where(np.isnan(heights), np.nanmean(heights), heights)
    gy, gx = np.gradient(h, res)
    return np.degrees(np.arctan(np.hypot(gx, gy)))


def stats_for(geometry: dict[str, Any], dem: dict[str, Any] | None = None) -> dict[str, Any]:
    """Elevation and slope statistics inside `geometry` (WGS84 GeoJSON)."""
    dem = dem or elevation_grid(geometry, margin_deg=0.0)
    grid: Grid = dem["grid"]
    to_utm = Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True)
    mask = rasterize([(shp_transform(to_utm.transform, to_shape(geometry)), 1)], out_shape=(grid.height, grid.width), transform=grid.transform, fill=0, all_touched=True).astype(bool)
    slope = slope_degrees(dem["heights"], grid.resolution)
    sel = mask & ~np.isnan(dem["heights"])
    if not sel.any():
        return {"status": "no_cells", "source": dem["source"]}
    h, s = dem["heights"][sel], slope[sel]
    return {"status": "ok", "source": dem["source"], "resolution_m": grid.resolution, "cells": int(sel.sum()), "elevation_m": {"min": round(float(h.min()), 1), "mean": round(float(h.mean()), 1), "max": round(float(h.max()), 1)}, "slope_deg": {"mean": round(float(s.mean()), 1), "p90": round(float(np.percentile(s, 90)), 1)}, "share_steeper_than_screen": round(float((s > SLOPE_SCREEN_DEG).mean()), 3), "screen_threshold_deg": SLOPE_SCREEN_DEG}


def mesh_payload(geometry: dict[str, Any], max_dim: int = 96, margin_deg: float = 0.01) -> dict[str, Any]:
    """Heights as rows north->south in metres plus a local frame for 3D/AR clients."""
    dem = elevation_grid(geometry, max_dim=max_dim, margin_deg=margin_deg)
    grid: Grid = dem["grid"]
    h = dem["heights"]
    h = np.where(np.isnan(h), np.nanmean(h), h)
    tf: Affine = grid.transform
    to_wgs = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
    cx, cy = tf * (grid.width / 2, grid.height / 2)
    lon, lat = to_wgs.transform(cx, cy)
    return {"width": grid.width, "height": grid.height, "cell_size_m": grid.resolution, "heights_m": np.round(h, 1).tolist(), "min_m": round(float(h.min()), 1), "max_m": round(float(h.max()), 1), "origin": {"lon": round(lon, 7), "lat": round(lat, 7), "utm_crs": grid.crs, "utm_x": cx, "utm_y": cy}, "frame": "x east, y north, metres from the grid centre; row 0 is the northern edge", "source": dem["source"]}
