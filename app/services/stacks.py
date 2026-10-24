"""Per-scene Sentinel-2 (NBR) and Sentinel-1 (VH/VV dB) time-series stacks for a region, cached by month.

Unlike composites, stacks keep each acquisition date so change can be dated. Same-day overlapping
tiles are merged. Arrays are float32 with NaN where there is no valid observation.
"""
import calendar
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
from rasterio.enums import Resampling

from app.core.config import get_settings
from app.core.geometry import to_shape
from app.services.acquisition import provider
from app.services.satellite import S2_CLOUD_CLASSES, AcquisitionConfig, Grid, ProviderError, make_grid

WORKERS = 4


def month_bounds(y: int, m: int) -> tuple[date, date]:
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def add_months(y: int, m: int, k: int) -> tuple[int, int]:
    i = y * 12 + (m - 1) + k
    return i // 12, i % 12 + 1


def _key(geometry: dict, sensor: str, y: int, m: int, extra: str = "") -> str:
    return hashlib.sha1(json.dumps([geometry, sensor, y, m, extra]).encode()).hexdigest()[:20]


def _cache(sensor: str, key: str) -> Path:
    return Path(get_settings().raster_dir) / f"stack-{sensor}-{key}.npz"


def _merge_by_day(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by: dict[str, list[dict]] = {}
    for it in items:
        by.setdefault(it["when"].date().isoformat(), []).append(it)
    out = []
    for day, group in sorted(by.items()):
        arrs = np.stack([g["data"] for g in group])
        with np.errstate(all="ignore"):
            merged = np.nanmean(arrs, axis=0) if len(group) > 1 else arrs[0]
        out.append({"when": group[0]["when"], "ids": [g["id"] for g in group], "data": merged.astype("float32")})
    return out


def _read_s2(pc, item: dict, grid: Grid) -> dict[str, Any]:
    def scaled(band: str):
        asset = item["assets"][band]
        meta = (asset.get("raster:bands") or [{}])[0]
        d = pc._read_band("sentinel-2-l2a", asset, grid, Resampling.bilinear)
        return np.ma.masked_less_equal(d, 0) * meta.get("scale", 1e-4) + meta.get("offset", 0.0)

    nir, swir = scaled("B08"), scaled("B12")
    scl = pc._read_band("sentinel-2-l2a", item["assets"]["SCL"], grid, Resampling.nearest)
    bad = np.isin(scl.filled(0), S2_CLOUD_CLASSES) | np.ma.getmaskarray(nir) | np.ma.getmaskarray(swir)
    n, s = nir.filled(np.nan), swir.filled(np.nan)
    with np.errstate(all="ignore"):
        nbr = np.where((n + s) > 0, (n - s) / (n + s), np.nan)
    nbr[bad] = np.nan
    return {"id": item["id"], "when": datetime.fromisoformat(item["properties"]["datetime"].replace("Z", "+00:00")), "data": nbr.astype("float32")}


def _read_s1(pc, item: dict, grid: Grid) -> dict[str, Any]:
    vh = pc._read_band("sentinel-1-rtc", item["assets"]["vh"], grid, Resampling.bilinear)
    vh = np.ma.masked_less_equal(vh, 0)
    db = np.where(np.ma.getmaskarray(vh), np.nan, 10 * np.log10(np.where(np.ma.getmaskarray(vh), 1.0, vh.filled(1.0))))
    return {"id": item["id"], "when": datetime.fromisoformat(item["properties"]["datetime"].replace("Z", "+00:00")), "data": db.astype("float32")}


def s1_orbit(geometry: dict, start: date, end: date) -> dict[str, Any]:
    """Pick one (orbit direction, relative orbit) for the whole run so incidence geometry stays constant."""
    pc = provider()
    bbox = list(to_shape(geometry).bounds)
    items = []
    cur = start
    while cur <= end:  # STAC search is limited per call, so walk in 3-month windows
        y2, m2 = add_months(cur.year, cur.month, 2)
        win_end = min(month_bounds(y2, m2)[1], end)
        items += pc.search("sentinel-1-rtc", bbox, cur, win_end, {"sar:instrument_mode": {"eq": "IW"}}, limit=100)
        y3, m3 = add_months(cur.year, cur.month, 3)
        cur = date(y3, m3, 1)
    items = [i for i in items if "vh" in i["assets"]]
    if not items:
        raise ProviderError("No Sentinel-1 RTC scenes for the period")
    keys = [(i["properties"].get("sat:orbit_state"), i["properties"].get("sat:relative_orbit")) for i in items]
    best = max(set(keys), key=keys.count)
    return {"orbit_state": best[0], "relative_orbit": best[1], "scenes_in_period": keys.count(best)}


def month_stack(geometry: dict, sensor: str, y: int, m: int, cfg: AcquisitionConfig, orbit: dict | None = None, max_cloud: float = 85.0) -> dict[str, Any]:
    """All usable acquisitions in calendar month (y, m). Cached once complete."""
    grid = make_grid(geometry, cfg)
    key = _key(geometry, sensor, y, m, json.dumps(orbit, sort_keys=True) if orbit else "")
    f = _cache(sensor, key)
    if f.is_file():
        z = np.load(f, allow_pickle=False)
        return {"data": z["data"], "when": [datetime.fromisoformat(x) for x in z["when"].tolist()], "ids": json.loads(str(z["ids"])), "grid": grid, "cache_hit": True}
    pc = provider()
    start, end = month_bounds(y, m)
    bbox = list(to_shape(geometry).bounds)
    if sensor == "sentinel-2":
        items = pc.search("sentinel-2-l2a", bbox, start, end, {"eo:cloud_cover": {"lt": max_cloud}}, limit=100)
        reader = _read_s2
    else:
        q = {"sar:instrument_mode": {"eq": "IW"}}
        items = [i for i in pc.search("sentinel-1-rtc", bbox, start, end, q, limit=100) if "vh" in i["assets"]]
        if orbit:
            items = [i for i in items if i["properties"].get("sat:orbit_state") == orbit["orbit_state"] and i["properties"].get("sat:relative_orbit") == orbit["relative_orbit"]]
        reader = _read_s1
    warnings: list[str] = []

    def one(it):
        for attempt in range(2):
            try:
                return reader(pc, it, grid)
            except Exception as exc:  # noqa: BLE001 - one bad scene must not sink the month
                err = type(exc).__name__
        warnings.append(f"{it['id']}: {err}")
        return None

    with ThreadPoolExecutor(WORKERS) as pool:
        got = [g for g in pool.map(one, items) if g]
    merged = _merge_by_day(got)
    if not merged:
        arr = np.zeros((0, grid.height, grid.width), dtype="float32"); whens: list[datetime] = []; ids: list[list[str]] = []
    else:
        arr = np.stack([g["data"] for g in merged]); whens = [g["when"] for g in merged]; ids = [g["ids"] for g in merged]
    if not warnings and end < date.today() - timedelta(days=10):  # a month still receiving scenes must not be frozen in the cache
        f.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(f, data=arr, when=np.array([w.isoformat() for w in whens]), ids=np.array(json.dumps(ids)))
    return {"data": arr, "when": whens, "ids": ids, "grid": grid, "cache_hit": False, "warnings": warnings}


def series(geometry: dict, sensor: str, start_ym: tuple[int, int], n_months: int, cfg: AcquisitionConfig, orbit: dict | None = None, progress=None) -> dict[str, Any]:
    arrays, whens, ids, grid = [], [], [], None
    for k in range(n_months):
        y, m = add_months(*start_ym, k)
        r = month_stack(geometry, sensor, y, m, cfg, orbit)
        grid = r["grid"]
        if len(r["when"]):
            arrays.append(r["data"]); whens += r["when"]; ids += r["ids"]
        if progress:
            progress(f"{sensor} {y}-{m:02d}: {len(r['when'])} acquisition date(s){' (cached)' if r['cache_hit'] else ''}")
    data = np.concatenate(arrays) if arrays else np.zeros((0, grid.height, grid.width), dtype="float32")
    order = np.argsort([w.timestamp() for w in whens]) if whens else []
    return {"data": data[order] if len(order) else data, "when": [whens[i] for i in order], "ids": [ids[i] for i in order], "grid": grid}
