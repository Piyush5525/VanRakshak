"""Scoring of the monthly alert service against public GLAD-L alert dates (Global Forest Watch tile cache).

Reference caveats (always returned with the metrics): GLAD-L is itself a Landsat-based alert product (30 m,
detection dates come from Landsat passes, it has its own commission/omission errors, and it is not field truth).
Agreement is measured with a one-pixel tolerance (~40 m) because the grids differ.
"""
import io
import json
import math
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from PIL import Image
from pyproj import Transformer
from rasterio.features import rasterize
from rasterio.transform import Affine, from_bounds
from rasterio.warp import Resampling, reproject
from scipy import ndimage
from shapely.geometry import shape
from shapely.ops import transform as shp_transform

from app.core.config import get_settings
from app.db.database import audit, get_one, insert, list_rows, now
from app.services.alerts import EPOCH, EVID, LOW, MONSOON_MONTHS, AlertParams, extract_patches, month_index
from app.services.satellite import Grid

TILE_URL = "https://tiles.globalforestwatch.org/umd_glad_landsat_alerts/latest/default/{z}/{x}/{y}.png"
GFW_EPOCH = date(2014, 12, 31)
Z = 12
TOL = np.ones((3, 3), dtype=bool)  # one-pixel agreement tolerance
TIMELY_DAYS = 45
MIN_N = 30  # below this many patches a percentage is not informative


def decode_tile(png: bytes) -> tuple[np.ndarray, np.ndarray]:
    """GFW default tile -> (days since 2015-01-01 or NaN, confidence 0/1/2). R*255+G = days since 2014-12-31; B >= 200 = high confidence."""
    a = np.array(Image.open(io.BytesIO(png)).convert("RGBA")).astype(np.int32)
    d = a[..., 0] * 255 + a[..., 1]
    has = d > 0
    day = np.where(has, d - 1, np.nan)  # shift epoch from 2014-12-31 to 2015-01-01
    conf = np.where(has, np.where(a[..., 2] >= 200, 2, 1), 0)
    return day, conf


def _merc(lon: float, lat: float) -> tuple[float, float]:
    t = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    return t.transform(lon, lat)


def _tile_xy(lon: float, lat: float) -> tuple[int, int]:
    n = 2 ** Z
    return int((lon + 180) / 360 * n), int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)


def load_reference(grid: Grid, client: httpx.Client | None = None) -> dict[str, Any]:
    """GLAD-L alert day/confidence resampled (nearest) onto `grid`."""
    to_wgs = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
    xs = [grid.transform.c, grid.transform.c + grid.transform.a * grid.width]
    ys = [grid.transform.f, grid.transform.f + grid.transform.e * grid.height]
    lons, lats = zip(*[to_wgs.transform(x, y) for x in xs for y in ys])
    x0, y1 = _tile_xy(min(lons), min(lats)); x1, y0 = _tile_xy(max(lons), max(lats))
    nx, ny = x1 - x0 + 1, y1 - y0 + 1
    if nx * ny > 36:
        raise ValueError("Region needs more than 36 reference tiles; evaluate a smaller region")
    day = np.full((ny * 256, nx * 256), np.nan, dtype="float32"); conf = np.zeros((ny * 256, nx * 256), dtype="uint8")
    c = client or httpx.Client(timeout=60, follow_redirects=True)
    for tx in range(x0, x1 + 1):
        for ty in range(y0, y1 + 1):
            r = c.get(TILE_URL.format(z=Z, x=tx, y=ty))
            if r.status_code != 200:
                raise RuntimeError(f"Reference tile {tx}/{ty} unavailable (HTTP {r.status_code})")
            d, cf = decode_tile(r.content)
            day[(ty - y0) * 256:(ty - y0 + 1) * 256, (tx - x0) * 256:(tx - x0 + 1) * 256] = d
            conf[(ty - y0) * 256:(ty - y0 + 1) * 256, (tx - x0) * 256:(tx - x0 + 1) * 256] = cf
    # EPSG:3857 bounds of the mosaic
    n = 2 ** Z; world = 20037508.342789244
    left = -world + x0 * 2 * world / n; right = -world + (x1 + 1) * 2 * world / n; top = world - y0 * 2 * world / n; bottom = world - (y1 + 1) * 2 * world / n
    src_tf = from_bounds(left, bottom, right, top, day.shape[1], day.shape[0])
    out_day = np.full((grid.height, grid.width), np.nan, dtype="float32"); out_conf = np.zeros((grid.height, grid.width), dtype="uint8")
    filled = np.where(np.isnan(day), -1, day).astype("float32")
    reproject(filled, out_day, src_transform=src_tf, src_crs="EPSG:3857", dst_transform=grid.transform, dst_crs=grid.crs, resampling=Resampling.nearest, src_nodata=None, dst_nodata=-9)
    reproject(conf, out_conf, src_transform=src_tf, src_crs="EPSG:3857", dst_transform=grid.transform, dst_crs=grid.crs, resampling=Resampling.nearest)
    out_day[out_day < 0] = np.nan
    return {"day": out_day, "conf": out_conf, "tiles": nx * ny, "source": "GLAD-L alerts via Global Forest Watch tile cache (UMD/GLAD)"}


# ---------------------------------------------------------------------------------------------
def prf(pred: np.ndarray, ref: np.ndarray) -> dict[str, Any]:
    """Tolerance-based precision / recall / F1 on boolean masks."""
    p, r = int(pred.sum()), int(ref.sum())
    tp_p = int((pred & ndimage.binary_dilation(ref, TOL)).sum())
    tp_r = int((ref & ndimage.binary_dilation(pred, TOL)).sum())
    prec = tp_p / p if p else None
    rec = tp_r / r if r else None
    f1 = (2 * prec * rec / (prec + rec)) if prec and rec else (0.0 if p and r else None)
    return {"pred_px": p, "ref_px": r, "precision": None if prec is None else round(prec, 4), "recall": None if rec is None else round(rec, 4), "f1": None if f1 is None else round(f1, 4)}


def _nearby_ref_day(ref_day: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Median reference day in the 3x3 neighbourhood for each masked pixel (NaN if none)."""
    pad = np.pad(ref_day, 1, constant_values=np.nan)
    stack = np.stack([pad[i:i + ref_day.shape[0], j:j + ref_day.shape[1]] for i in range(3) for j in range(3)])
    with np.errstate(all="ignore"):
        med = np.nanmedian(stack, axis=0)
    return np.where(mask, med, np.nan)


def _stats_days(err: np.ndarray) -> dict[str, Any]:
    err = err[np.isfinite(err)]
    if err.size == 0:
        return {"n": 0, "median_abs_error_days": None, "median_signed_error_days": None, "p90_abs_error_days": None}
    return {"n": int(err.size), "median_abs_error_days": round(float(np.median(np.abs(err))), 1), "median_signed_error_days": round(float(np.median(err)), 1), "p90_abs_error_days": round(float(np.percentile(np.abs(err), 90)), 1)}


def score_variant(pix: dict[str, Any], grid, params: AlertParams, ref_day: np.ndarray, forest: np.ndarray, period: tuple[int, int], top_k: int = 50) -> dict[str, Any]:
    """Metrics for one alert variant (fused or optical-only)."""
    ev = pix["evidence"]; keep = (ev > 0) & ~np.isin(ev, LOW)
    in_period = np.isfinite(ref_day) & (ref_day >= period[0]) & (ref_day <= period[1]) & forest
    allm = prf(keep, in_period)
    ref_near = _nearby_ref_day(ref_day, keep & ndimage.binary_dilation(in_period, TOL))
    est_err = pix["est_day"] - ref_near
    issue_delay = pix["issue_day"] - ref_near
    tp = keep & np.isfinite(ref_near)
    res: dict[str, Any] = {"pixel": allm, "date_estimate_error": _stats_days(est_err[tp]), "issue_delay_after_reference": _stats_days(issue_delay[tp])}
    patches = extract_patches(pix, grid, params)
    ref_dil = ndimage.binary_dilation(in_period, TOL)
    hits, hits_dated = [], []
    near_all = _nearby_ref_day(np.where(in_period, ref_day, np.nan), np.ones(ref_day.shape, dtype=bool))
    for p in patches:
        ys, xs = np.array(p["pixels"][0]), np.array(p["pixels"][1])
        inside = ref_dil[ys, xs]
        hit = bool(inside.any())
        nd = near_all[ys, xs]
        d_ok = bool(np.isfinite(nd).any() and abs(p["est_day"] - float(np.nanmedian(nd))) <= 60)
        hits.append(hit); hits_dated.append(hit and d_ok)
        p["_hit"], p["_hit_dated"] = hit, hit and d_ok
    k = min(top_k, len(patches))
    res["patches"] = {"n": len(patches), "precision_at_k": {"k": k, "k_requested": top_k, "spatial": round(sum(hits[:k]) / k, 4) if k else None, "spatial_and_date_within_60d": round(sum(hits_dated[:k]) / k, 4) if k else None}, "precision_all": round(sum(hits) / len(hits), 4) if hits else None, "reliable_sample": len(patches) >= MIN_N}
    by_month: dict[str, dict] = {}
    for p in patches:
        m = by_month.setdefault(p["issue_month"], {"alerts": 0, "hits": 0, "monsoon": p["monsoon"]})
        m["alerts"] += 1; m["hits"] += int(p["_hit"])
    for m in by_month.values():
        m["precision"] = round(m["hits"] / m["alerts"], 3)
    res["by_month"] = dict(sorted(by_month.items()))
    # seasonal split: precision by the season an alert was ISSUED in; recall by the season the REFERENCE loss happened in
    midx = month_index(pix["issue_day"])
    issue_season = (midx % 12) + 1
    mon_issue = keep & (midx >= 0) & np.isin(issue_season, MONSOON_MONTHS)
    non_issue = keep & (midx >= 0) & ~np.isin(issue_season, MONSOON_MONTHS)
    ref_month = (month_index(np.where(in_period, ref_day, np.nan)) % 12) + 1
    ref_sets = {"monsoon": in_period & np.isin(ref_month, MONSOON_MONTHS), "non_monsoon": in_period & ~np.isin(ref_month, MONSOON_MONTHS)}
    issue_sets = {"monsoon": mon_issue, "non_monsoon": non_issue}
    # earliest issue date of any alert within the 1-pixel neighbourhood of each reference pixel
    issue_inf = np.where(keep & np.isfinite(pix["issue_day"]), pix["issue_day"], np.inf)
    earliest_near = ndimage.minimum_filter(issue_inf, footprint=TOL, mode="constant", cval=np.inf)
    res["season"] = {}
    for name in ("monsoon", "non_monsoon"):
        r_s, a_s = ref_sets[name], issue_sets[name]
        prec = prf(a_s, in_period)["precision"]
        eventual = prf(keep, r_s)["recall"]
        timely = None
        if r_s.any():
            lag = earliest_near[r_s] - ref_day[r_s]
            timely = round(float(np.mean(np.isfinite(lag) & (lag <= TIMELY_DAYS))), 4)
        f1t = None if not prec or not timely else round(2 * prec * timely / (prec + timely), 4)
        res["season"][name] = {"precision": prec, "recall": eventual, "timely_recall_45d": timely, "f1_timely": f1t, "f1": prf(a_s, r_s)["f1"], "reference_px": int(r_s.sum()), "alert_px": int(a_s.sum()), "alert_patches": sum(1 for p in patches if p["monsoon"] == (name == "monsoon")), "issue_delay_days": _stats_days(issue_delay[tp & a_s])}
    res["_patch_objs"] = patches
    return res


def phase1_mask(change: dict[str, Any], grid) -> np.ndarray:
    to_utm = Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True)
    geom = shp_transform(to_utm.transform, shape(change["geometry"]))
    return rasterize([(geom, 1)], out_shape=(grid.height, grid.width), transform=grid.transform, fill=0, all_touched=False).astype(bool)


def evaluate_arrays(f: dict, o: dict, grid, params: AlertParams, ref: dict, period: tuple[int, int], phase1: np.ndarray | None, top_k: int = 50) -> dict[str, Any]:
    forest = f["forest"]
    fused = score_variant(f, grid, params, ref["day"], forest, period, top_k)
    optical = score_variant(o, grid, params, ref["day"], forest, period, top_k)
    in_period = np.isfinite(ref["day"]) & (ref["day"] >= period[0]) & (ref["day"] <= period[1]) & forest
    lab, n_ref_patches = ndimage.label(ndimage.binary_dilation(in_period, np.ones((3, 3))))
    out: dict[str, Any] = {"reference": {"source": ref["source"], "period": [(EPOCH + timedelta(days=period[0])).isoformat(), (EPOCH + timedelta(days=period[1])).isoformat()], "reference_alert_pixels_in_forest": int(in_period.sum()), "reference_patches": int(n_ref_patches), "high_confidence_share": round(float((ref["conf"][in_period] == 2).mean()), 3) if in_period.any() else None},
                           "fused_s1_s2": {k: v for k, v in fused.items() if k != "_patch_objs"}, "optical_only_s2": {k: v for k, v in optical.items() if k != "_patch_objs"}}
    # radar gain
    def g(a, b):
        return None if a is None or b is None else round(a - b, 4)
    out["radar_gain"] = {season: {"alert_patches_gain": fused["season"][season]["alert_patches"] - optical["season"][season]["alert_patches"], "recall_gain": g(fused["season"][season]["recall"], optical["season"][season]["recall"]), "timely_recall_45d_gain": g(fused["season"][season]["timely_recall_45d"], optical["season"][season]["timely_recall_45d"]), "precision_change": g(fused["season"][season]["precision"], optical["season"][season]["precision"]), "f1_timely_gain": g(fused["season"][season]["f1_timely"], optical["season"][season]["f1_timely"]), "fused": {k: fused["season"][season][k] for k in ("precision", "recall", "timely_recall_45d")}, "optical_only": {k: optical["season"][season][k] for k in ("precision", "recall", "timely_recall_45d")}} for season in ("monsoon", "non_monsoon")}
    # Phase 1 guard: F1 of the whole-year change map vs the original two-date optical detector
    f1_p2 = fused["pixel"]["f1"]
    if phase1 is not None:
        p1 = prf(phase1 & forest, in_period)
        out["phase1_check"] = {"phase1_two_date_optical": p1, "phase2_all_alerts": fused["pixel"], "delta_f1_points": None if p1["f1"] is None or f1_p2 is None else round((f1_p2 - p1["f1"]) * 100, 2), "within_5_points": None if p1["f1"] is None or f1_p2 is None else bool(f1_p2 >= p1["f1"] - 0.05)}
    else:
        out["phase1_check"] = {"status": "not_computed", "reason": "No stored Phase-1 (two-date) change detection for this region and year. Run Change Detection from the start to the end of the year, then evaluate again."}
    warns = []
    if n_ref_patches < MIN_N:
        warns.append(f"Only {n_ref_patches} reference alert patches in this region and period; percentages are anecdotal, not statistically meaningful.")
    if fused["patches"]["n"] < MIN_N:
        warns.append(f"Only {fused['patches']['n']} issued alert patches; precision@50 is computed over all of them (K={fused['patches']['precision_at_k']['k']}).")
    warns.append("GLAD-L is an independent alert product, not field truth: disagreement can be its omission as well as ours.")
    out["warnings"] = warns
    return out


def evaluate_run(run_id: str, phase1_change_id: str | None, actor: str, top_k: int = 50) -> dict[str, Any]:
    run = get_one("alert_runs", "id", run_id)
    if not run or run["status"] != "COMPLETE":
        raise ValueError("Alert run not found or not complete")
    region = get_one("regions", "id", run["region_id"])
    z = np.load(Path(get_settings().raster_dir) / f"alertpix-{run_id}.npz")
    tf = Affine(*z["transform"].tolist())
    grid = Grid(str(z["crs"]), tf, z["f_evidence"].shape[1], z["f_evidence"].shape[0], abs(tf.a))
    f = {k[2:]: z[k] for k in z.files if k.startswith("f_")}; o = {k[2:]: z[k] for k in z.files if k.startswith("o_")}
    params = AlertParams(**{k: (tuple(v) if k == "radar_support_window" else v) for k, v in run["params"].items() if k in AlertParams.__dataclass_fields__})
    year = run["year"]; n_months = run["params"]["n_months"]
    period = ((date(year, 1, 1) - EPOCH).days, (date(*_end_ym(year, n_months), 1) - EPOCH).days - 1)
    ref = load_reference(grid)
    phase1 = None
    if phase1_change_id:
        ch = get_one("changes", "id", phase1_change_id)
        if not ch or ch["region_id"] != run["region_id"]:
            raise ValueError("Phase-1 change record not found for this region")
        phase1 = phase1_mask(ch, grid)
    res = evaluate_arrays(f, o, grid, params, ref, period, phase1, top_k)
    res.update({"run_id": run_id, "region": region["name"], "params_digest": run["params"].get("digest"), "held_out_declared": bool(run["held_out"]), "generated_at": now(), "id": str(uuid.uuid4())})
    insert("alert_evaluations", {"id": res["id"], "run_id": run_id, "status": "COMPLETE", "result": res, "created_at": res["generated_at"]})
    audit(actor, "alert_evaluation_created", "alert_run", run_id, {"evaluation_id": res["id"]})
    return res


def _end_ym(year: int, n_months: int) -> tuple[int, int]:
    i = year * 12 + n_months  # first day of the month after the last alert month
    return i // 12, i % 12 + 1
