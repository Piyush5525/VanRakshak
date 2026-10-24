"""Render stored composites to web-map PNGs (reprojected to WGS84 so Leaflet overlays align exactly)."""
import io
from typing import Any

import numpy as np
import rasterio
from PIL import Image
from rasterio.transform import array_bounds
from rasterio.warp import Resampling, calculate_default_transform, reproject

CLOUD_GREY = (225, 225, 225)


def _to_wgs84(arr: np.ndarray, src_tf, src_crs, fill: float) -> tuple[np.ndarray, tuple[float, float, float, float]]:
    h, w = arr.shape[-2:]
    left, bottom, right, top = array_bounds(h, w, src_tf)
    tf, dw, dh = calculate_default_transform(src_crs, "EPSG:4326", w, h, left, bottom, right, top)
    out = np.full((dh, dw), fill, dtype="float32")
    reproject(arr.astype("float32"), out, src_transform=src_tf, src_crs=src_crs, dst_transform=tf, dst_crs="EPSG:4326", resampling=Resampling.nearest, src_nodata=None, dst_nodata=fill)
    west, south, east, north = array_bounds(dh, dw, tf)
    return out, (south, west, north, east)


def render(path: str, sensor: str, kind: str = "auto", max_px: int = 900) -> tuple[bytes, list[list[float]]]:
    with rasterio.open(path) as src:
        data, tf, crs = src.read(), src.transform, src.crs
    if sensor == "sentinel-2":
        nir, swir, cloud = data[0], data[1], data[2]
        with np.errstate(divide="ignore", invalid="ignore"):
            nbr = np.where((nir + swir) > 0, (nir - swir) / (nir + swir), 0)
        val, bounds = _to_wgs84(nbr, tf, crs, -9.0)
        cl, _ = _to_wgs84(cloud, tf, crs, 2.0)
        t = np.clip((val + 0.1) / 0.7, 0, 1)
        rgb = np.stack([(190 - 140 * t), (150 + 70 * t), (100 - 50 * t)], -1)
        rgb[(cl > 0) | (val < -5)] = CLOUD_GREY
        alpha = np.where(val < -5, 0, 235)
    elif sensor == "sentinel-1":
        vh, missing = data[1], data[2]
        val, bounds = _to_wgs84(vh, tf, crs, -99.0)
        miss, _ = _to_wgs84(missing, tf, crs, 2.0)
        g = np.clip((val + 25) / 20, 0, 1) * 255  # -25..-5 dB
        rgb = np.stack([g, g, g], -1)
        alpha = np.where((val < -90) | (miss > 0), 0, 235)
    else:  # forest mask
        tree, nodata = data[0], data[1]
        val, bounds = _to_wgs84(tree, tf, crs, 0.0)
        nd, _ = _to_wgs84(nodata, tf, crs, 2.0)
        rgb = np.where(val[..., None] > 0, np.array([34, 120, 60]), np.array([200, 200, 190]))
        alpha = np.where(nd > 0, 0, 200)
    img = Image.fromarray(np.dstack([rgb.clip(0, 255), alpha]).astype("uint8"), "RGBA")
    if max(img.size) > max_px:
        img.thumbnail((max_px, max_px), Image.NEAREST)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    s, w, n, e = bounds
    return buf.getvalue(), [[s, w], [n, e]]
