"""Monthly forest-loss alert engine (CL-01 Phase 2).

Time-series change detection on per-scene stacks:
  * optical: Sentinel-2 NBR drops below its pre-period median by `opt_drop`
  * radar:   Sentinel-1 VH backscatter falls `rad_k` robust sigmas below its pre-period median
A change needs TWO consecutive valid observations to confirm (rain, haze, shadow and speckle make single
dips common). The loss date is the midpoint between the last unchanged observation and the first changed
one; the half-gap is reported as the date uncertainty. Alerts are issued when the confirming observation is
available, so each alert uses only data up to its issue date (causal).

Evidence classes (per pixel):
  1 optical+radar agree   2 optical (radar absent)   3 optical, radar saw no change (low)
  4 radar, optical blind  5 radar, optical clear and saw nothing (low)
Classes 3 and 5 are kept out of the issued alerts by default.
"""
import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
from pyproj import Transformer
from rasterio import features
from rasterio.warp import transform_geom
from scipy import ndimage
from shapely.ops import transform as shp_transform

from app.core.config import get_settings
from app.core.geometry import geodesic_area_hectares, to_shape
from app.db.database import audit, connection, get_one, insert, now, update
from app.services.acquisition import provider
from app.services.satellite import AcquisitionConfig, ProviderError, make_grid
from app.services.stacks import add_months, s1_orbit, series

EPOCH = date(2015, 1, 1)
MONSOON_MONTHS = (6, 7, 8, 9)
EVID = {1: "optical_radar_agree", 2: "optical", 3: "optical_radar_unsupported", 4: "radar_optical_blind", 5: "radar_only_optical_clear"}
WEIGHT = {1: 1.0, 2: 0.8, 3: 0.3, 4: 0.7, 5: 0.25}
LOW = (3, 5)


@dataclass(frozen=True)
class AlertParams:
    opt_drop: float = 0.25
    rad_k: float = 3.0
    rad_sigma_floor_db: float = 0.8
    rad_smooth: int = 3
    min_ref_obs: int = 3
    corroboration_days: int = 45
    optical_blind_window_days: int = 20
    radar_support_window: tuple[int, int] = (15, 30)
    min_patch_ha: float = 0.2
    forest_nbr_min: float = 0.15
    include_low_confidence: bool = False
    seasonal: bool = True            # compare each scene with the same time of year in the baseline period
    season_window_days: int = 30
    min_season_obs: int = 2

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------------------------
def confirmed_onset(bad: np.ndarray, valid: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """For each pixel: index of the first changed observation whose NEXT valid observation is also changed.

    Returns (onset_idx, confirm_idx, prev_ok_idx); -1 where none. `prev_ok_idx` is the last valid,
    unchanged observation before onset (-1 if the series has none).
    """
    T, H, W = bad.shape
    prev = np.full((H, W), -1, dtype=np.int16)
    prev_by_t = np.empty((T, H, W), dtype=np.int16)
    for t in range(T):
        prev_by_t[t] = prev
        prev = np.where(valid[t] & ~bad[t], t, prev)
    nxt = np.full((H, W), -1, dtype=np.int16)
    nxt_bad = np.zeros((H, W), dtype=bool)
    cond = np.zeros((T, H, W), dtype=bool)
    nxt_by_t = np.empty((T, H, W), dtype=np.int16)
    for t in range(T - 1, -1, -1):
        cond[t] = bad[t] & valid[t] & (nxt >= 0) & nxt_bad
        nxt_by_t[t] = nxt
        upd = valid[t]
        nxt = np.where(upd, t, nxt)
        nxt_bad = np.where(upd, bad[t], nxt_bad)
    anyc = cond.any(0)
    onset = np.where(anyc, cond.argmax(0), -1).astype(np.int16)
    take = np.clip(onset, 0, None)[None]
    confirm = np.where(anyc, np.take_along_axis(nxt_by_t, take, 0)[0], -1).astype(np.int16)
    prev_ok = np.where(anyc, np.take_along_axis(prev_by_t, take, 0)[0], -1).astype(np.int16)
    return onset, confirm, prev_ok


def _days(whens: list[datetime]) -> np.ndarray:
    return np.array([(w.date() - EPOCH).days + w.hour / 24 for w in whens], dtype="float64")


def _est(onset, confirm, prev_ok, t_days: np.ndarray, start_day: float):
    has = onset >= 0
    o = t_days[np.clip(onset, 0, None)]
    p = np.where(prev_ok >= 0, t_days[np.clip(prev_ok, 0, None)], np.maximum(start_day, o - 30))
    c = t_days[np.clip(confirm, 0, None)]
    est = np.where(has, (o + p) / 2, np.nan)
    unc = np.where(has, np.minimum((o - p) / 2, 45), np.nan)
    return est, unc, np.where(has, c, np.nan)


def _doy(whens: list[datetime]) -> np.ndarray:
    return np.array([w.timetuple().tm_yday for w in whens], dtype="int32")


def _window_median(ref: np.ndarray, ref_doy: np.ndarray, t_doy: int, window: int, min_obs: int) -> np.ndarray:
    """Per-pixel median of baseline observations within +-window days of the same day of year (circular)."""
    diff = np.abs(ref_doy - t_doy)
    sel = np.minimum(diff, 365 - diff) <= window
    if sel.sum() == 0:
        return np.full(ref.shape[1:], np.nan, dtype="float32")
    sub = ref[sel]
    with np.errstate(all="ignore"):
        med = np.nanmedian(sub, axis=0)
    n = np.isfinite(sub).sum(0)
    return np.where(n >= min_obs, med, np.nan).astype("float32")


def seasonal_baseline(data: np.ndarray, whens: list[datetime], is_ref: np.ndarray, params: "AlertParams") -> np.ndarray:
    """(T,H,W) baseline: for every scene, the median of baseline-period scenes from the same time of year."""
    ref, ref_doy, doy = data[is_ref], _doy([w for w, r in zip(whens, is_ref) if r]), _doy(whens)
    out = np.full(data.shape, np.nan, dtype="float32")
    cache: dict[int, np.ndarray] = {}
    for t in range(len(whens)):
        if is_ref[t]:
            continue
        key = int(doy[t])
        if key not in cache:
            cache[key] = _window_median(ref, ref_doy, key, params.season_window_days, params.min_season_obs)
        out[t] = cache[key]
    return out


def seasonal_sigma(ref: np.ndarray, ref_doy: np.ndarray, params: "AlertParams") -> np.ndarray:
    """Robust per-pixel spread of baseline scenes around their own seasonal median."""
    resid = np.stack([ref[i] - _window_median(ref, ref_doy, int(ref_doy[i]), params.season_window_days, params.min_season_obs) for i in range(len(ref))])
    with np.errstate(all="ignore"):
        return np.maximum(1.4826 * np.nanmedian(np.abs(resid), axis=0), params.rad_sigma_floor_db)


def detect(opt: dict | None, rad: dict | None, ref_end_day: float, start_day: float, params: AlertParams, use_radar: bool = True) -> dict[str, Any]:
    """Pixel-level alert arrays. `opt`/`rad` are {'data': (T,H,W), 'when': [datetime]}; reference = obs with day < ref_end_day."""
    shape = (opt or rad)["data"].shape[1:]
    out = {"evidence": np.zeros(shape, dtype=np.uint8), "est_day": np.full(shape, np.nan), "unc_days": np.full(shape, np.nan), "issue_day": np.full(shape, np.nan), "forest": np.zeros(shape, dtype=bool)}
    onset_o = est_o = unc_o = conf_o = None
    if opt is not None and len(opt["when"]):
        d = opt["data"]; to = _days(opt["when"]); isref = to < ref_end_day
        ref = d[isref]
        nref = np.isfinite(ref).sum(0)
        with np.errstate(all="ignore"):
            base = np.nanmedian(ref, axis=0) if ref.shape[0] else np.full(shape, np.nan)
        ok_base = nref >= params.min_ref_obs
        out["forest"] = ok_base & (base >= params.forest_nbr_min)
        valid = np.isfinite(d)
        if params.seasonal:
            base_t = seasonal_baseline(d, opt["when"], isref, params)
            bad = valid & np.isfinite(base_t) & (d < base_t - params.opt_drop) & (~isref)[:, None, None]
        else:
            bad = valid & (d < (base - params.opt_drop)[None]) & ok_base[None] & (~isref)[:, None, None]
        o_idx, c_idx, p_idx = confirmed_onset(bad, valid)
        o_idx[~(ok_base & out["forest"])] = -1
        est_o, unc_o, conf_o = _est(o_idx, c_idx, p_idx, to, ref_end_day)
        onset_o = o_idx
    onset_r = est_r = unc_r = conf_r = None
    rvalid = None
    if use_radar and rad is not None and len(rad["when"]):
        d = rad["data"].astype("float32"); tr = _days(rad["when"]); isref = tr < ref_end_day
        if params.rad_smooth > 1:
            d = np.stack([_nan_smooth(x, params.rad_smooth) for x in d])
        ref = d[isref]
        nref = np.isfinite(ref).sum(0)
        rvalid = np.isfinite(d)
        if params.seasonal:
            med_t = seasonal_baseline(d, rad["when"], isref, params)
            sig = seasonal_sigma(ref, _doy([w for w, r in zip(rad["when"], isref) if r]), params)
            with np.errstate(all="ignore"):
                z = (d - med_t) / sig[None]
            bad = rvalid & np.isfinite(med_t) & (z < -params.rad_k) & (~isref)[:, None, None]
        else:
            with np.errstate(all="ignore"):
                med = np.nanmedian(ref, axis=0) if ref.shape[0] else np.full(shape, np.nan)
                sig = np.maximum(1.4826 * np.nanmedian(np.abs(ref - med[None]), axis=0), params.rad_sigma_floor_db) if ref.shape[0] else np.full(shape, np.nan)
            ok_base = nref >= params.min_ref_obs
            with np.errstate(all="ignore"):
                z = (d - med[None]) / sig[None]
            bad = rvalid & (z < -params.rad_k) & ok_base[None] & (~isref)[:, None, None]
        o_idx, c_idx, p_idx = confirmed_onset(bad, rvalid)
        if opt is not None:
            o_idx[~out["forest"]] = -1
        est_r, unc_r, conf_r = _est(o_idx, c_idx, p_idx, tr, ref_end_day)
        onset_r = o_idx
    has_o = onset_o is not None and (onset_o >= 0)
    has_r = onset_r is not None and (onset_r >= 0)
    if not isinstance(has_o, np.ndarray):
        has_o = np.zeros(shape, dtype=bool)
    if not isinstance(has_r, np.ndarray):
        has_r = np.zeros(shape, dtype=bool)
    with np.errstate(all="ignore"):
        agree = has_o & has_r & (np.abs(np.nan_to_num(est_o) - np.nan_to_num(est_r)) <= params.corroboration_days) if (has_o.any() and has_r.any()) else np.zeros(shape, dtype=bool)
    opt_only = has_o & ~agree
    rad_only = has_r & ~agree
    # radar support check for optical-only pixels, optical-blind check for radar-only pixels
    if opt_only.any() and rvalid is not None:
        cnt = np.zeros(shape, dtype=np.int16)
        lo, hi = params.radar_support_window
        for t in range(len(tr)):
            cnt += (rvalid[t] & (tr[t] >= est_o - lo) & (tr[t] <= est_o + hi)).astype(np.int16)
        radar_saw_nothing = cnt > 0
    else:
        radar_saw_nothing = np.zeros(shape, dtype=bool)
    if rad_only.any() and opt is not None:
        cnt = np.zeros(shape, dtype=np.int16)
        ov = np.isfinite(opt["data"]); to = _days(opt["when"]); w = params.optical_blind_window_days
        for t in range(len(to)):
            cnt += (ov[t] & (np.abs(to[t] - est_r) <= w)).astype(np.int16)
        optical_clear = cnt > 0
    else:
        optical_clear = np.zeros(shape, dtype=bool)
    ev = out["evidence"]
    ev[agree] = 1
    ev[opt_only & ~radar_saw_nothing] = 2
    ev[opt_only & radar_saw_nothing] = 3
    ev[rad_only & ~optical_clear] = 4
    ev[rad_only & optical_clear] = 5
    if opt is None:
        ev[rad_only] = 4
    for code, (est, unc, conf) in ((1, (est_o, unc_o, conf_o)), (2, (est_o, unc_o, conf_o)), (3, (est_o, unc_o, conf_o)), (4, (est_r, unc_r, conf_r)), (5, (est_r, unc_r, conf_r))):
        if est is None:
            continue
        m = ev == code
        out["est_day"][m], out["unc_days"][m], out["issue_day"][m] = est[m], unc[m], conf[m]
    m1 = ev == 1
    if m1.any():
        # corroborated pixels: date from the modality with the tighter bracket (optical is vague across a cloud gap),
        # issued at the earlier confirmation
        use_r = unc_r[m1] < unc_o[m1]
        out["est_day"][m1] = np.where(use_r, est_r[m1], est_o[m1])
        out["unc_days"][m1] = np.where(use_r, unc_r[m1], unc_o[m1])
        out["issue_day"][m1] = np.fmin(conf_o[m1], conf_r[m1])
    return out


def _nan_smooth(x: np.ndarray, size: int) -> np.ndarray:
    v = np.isfinite(x)
    num = ndimage.uniform_filter(np.where(v, x, 0.0), size, mode="nearest")
    den = ndimage.uniform_filter(v.astype("float32"), size, mode="nearest")
    with np.errstate(all="ignore"):
        out = np.where(den > 0.5, num / den, np.nan)
    return np.where(v, out, np.nan)


# ---------------------------------------------------------------------------------------------
def to_date(day: float) -> date:
    return EPOCH + timedelta(days=int(np.floor(day)))


def month_key(day: float) -> str:
    d = to_date(day)
    return f"{d.year}-{d.month:02d}"


def month_index(days: np.ndarray) -> np.ndarray:
    """Months since 1970-01 for each day number; -1 where NaN. Vectorised."""
    ok = np.isfinite(days)
    d = np.where(ok, np.floor(days), 0).astype("int64")
    m = (np.datetime64("2015-01-01") + d.astype("timedelta64[D]")).astype("datetime64[M]").astype("int64")
    return np.where(ok, m, -1)


def extract_patches(pix: dict[str, Any], grid, params: AlertParams, include_low: bool | None = None) -> list[dict[str, Any]]:
    """Group alert pixels into patches per issue month; rank by score."""
    include_low = params.include_low_confidence if include_low is None else include_low
    ev = pix["evidence"]
    keep = ev > 0
    if not include_low:
        keep &= ~np.isin(ev, LOW)
    issue = pix["issue_day"]
    px_ha = abs(grid.transform.a * grid.transform.e) / 1e4
    min_px = max(1, int(round(params.min_patch_ha / px_ha)))
    to_wgs = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
    patches: list[dict[str, Any]] = []
    midx = month_index(issue)
    for mi in sorted({int(v) for v in midx[keep]}):
        mk = f"{1970 + mi // 12}-{mi % 12 + 1:02d}"
        mm = keep & (midx == mi)
        lab, n = ndimage.label(mm, structure=np.ones((3, 3)))
        if n == 0:
            continue
        sizes = ndimage.sum(mm, lab, range(1, n + 1))
        for i, size in enumerate(sizes, start=1):
            if size < min_px:
                continue
            sel = lab == i
            classes, counts = np.unique(ev[sel], return_counts=True)
            dom = int(classes[counts.argmax()])
            est = float(np.nanmedian(pix["est_day"][sel])); unc = float(np.nanmedian(pix["unc_days"][sel])); iss = float(np.nanmax(pix["issue_day"][sel]))
            geom = None
            for g, v in features.shapes(sel.astype("uint8"), mask=sel, transform=grid.transform):
                wg = transform_geom(grid.crs, "EPSG:4326", g)
                geom = wg if geom is None else {"type": "MultiPolygon", "coordinates": ([geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]) + [wg["coordinates"]]}
            area = float(size) * px_ha
            score = WEIGHT[dom] * (area ** 0.5) * (1 - 0.3 * min(unc, 60) / 60)
            ys, xs = np.nonzero(sel)
            patches.append({"issue_month": mk, "est_date": to_date(est).isoformat(), "est_day": est, "date_uncertainty_days": round(unc, 1), "issue_date": to_date(iss).isoformat(), "issue_day": iss, "area_ha": round(area, 3), "evidence": EVID[dom], "evidence_mix": {EVID[int(c)]: int(n_) for c, n_ in zip(classes, counts)}, "geometry": geom, "score": round(score, 4), "monsoon": int(mk[5:]) in MONSOON_MONTHS, "pixels": [ys.tolist(), xs.tolist()]})
    patches.sort(key=lambda p: -p["score"])
    for r, p in enumerate(patches, 1):
        p["rank"] = r
    return patches


# ---------------------------------------------------------------------------------------------
def run(region_id: str, year: int, n_months: int, ref_months: int, use_radar: bool, params: AlertParams, actor: str, held_out: bool = False, run_id: str | None = None) -> dict[str, Any]:
    if not 1 <= n_months <= 12 or not 2 <= ref_months <= 12:
        raise ValueError("n_months must be 1-12 and ref_months 2-12")
    region = get_one("regions", "id", region_id)
    if not region:
        raise ValueError("Monitoring region not found")
    run_id = run_id or str(uuid.uuid4())
    started = now()
    log: list[str] = []

    def progress(msg: str, frac: float | None = None):
        log.append(msg)
        res = {"log": log[-40:]}
        update("alert_runs", "id", run_id, {"status": "RUNNING", "result": res, **({"progress": frac} if frac is not None else {})})

    insert("alert_runs", {"id": run_id, "region_id": region_id, "year": year, "status": "RUNNING", "progress": 0.0, "params": {**asdict(params), "n_months": n_months, "ref_months": ref_months, "use_radar": use_radar, "digest": params.digest()}, "held_out": int(held_out), "result": {"log": []}, "created_at": started})
    audit(actor, "alert_run_started", "alert_run", run_id, {"region_id": region_id, "year": year})
    try:
        geom = region["geometry"]
        cfg = AcquisitionConfig(resolution_m=20.0)
        total_start = add_months(year, 1, -ref_months)
        total_n = ref_months + n_months
        end_date = date(*add_months(year, 1, n_months - 1), 28)
        orbit = s1_orbit(geom, date(*total_start, 1), end_date) if use_radar else None
        progress(f"Sentinel-1 orbit: {orbit}" if orbit else "Radar disabled", 0.02)
        opt = series(geom, "sentinel-2", total_start, total_n, cfg, progress=lambda m: progress(m))
        rad = series(geom, "sentinel-1", total_start, total_n, cfg, orbit=orbit, progress=lambda m: progress(m)) if use_radar else None
        progress("Detecting change…", 0.85)
        ref_end = (date(year, 1, 1) - EPOCH).days
        start_day = ref_end
        fused = detect(opt, rad, ref_end, start_day, params, use_radar=use_radar)
        optical_only = detect(opt, None, ref_end, start_day, params, use_radar=False)
        grid = opt["grid"]
        patches = extract_patches(fused, grid, params)
        # persist
        pixdir = Path(get_settings().raster_dir); pixdir.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(pixdir / f"alertpix-{run_id}.npz", **{f"f_{k}": v for k, v in fused.items()}, **{f"o_{k}": v for k, v in optical_only.items()}, transform=np.array(grid.transform)[:6], crs=np.array(grid.crs), ref_end=ref_end)
        for p in patches:
            rec = {k: p[k] for k in ("issue_month", "est_date", "date_uncertainty_days", "issue_date", "area_ha", "evidence", "evidence_mix", "geometry", "score", "rank")}
            insert("alerts", {"id": str(uuid.uuid4()), "run_id": run_id, "region_id": region_id, "monsoon": int(p["monsoon"]), **{k: v for k, v in rec.items() if k != "evidence_mix"}, "details": {"evidence_mix": rec["evidence_mix"], "claim_type": "candidate_inference"}, "created_at": now()})
        by_month = {}
        for p in patches:
            m = by_month.setdefault(p["issue_month"], {"alerts": 0, "area_ha": 0.0, "radar_contributed": 0, "monsoon": p["monsoon"]})
            m["alerts"] += 1; m["area_ha"] = round(m["area_ha"] + p["area_ha"], 3); m["radar_contributed"] += int(p["evidence"] in ("optical_radar_agree", "radar_optical_blind"))
        clear_frac = {}
        for k in range(n_months):
            y, mth = add_months(year, 1, k)
            idx = [i for i, w in enumerate(opt["when"]) if (w.year, w.month) == (y, mth)]
            clear_frac[f"{y}-{mth:02d}"] = {"optical_dates": len(idx), "mean_clear_fraction": round(float(np.isfinite(opt["data"][idx]).mean()), 3) if idx else 0.0, "radar_dates": sum(1 for w in (rad["when"] if rad else []) if (w.year, w.month) == (y, mth))}
        result = {"summary": {"alerts": len(patches), "area_ha": round(sum(p["area_ha"] for p in patches), 3), "forest_pixels": int(fused["forest"].sum()), "alert_pixels": int((fused["evidence"] > 0).sum()), "suppressed_low_confidence_pixels": int(np.isin(fused["evidence"], LOW).sum()), "optical_only_pixels": int((optical_only["evidence"] > 0).sum())}, "by_month": by_month, "data_coverage": clear_frac, "orbit": orbit, "n_optical_dates": len(opt["when"]), "n_radar_dates": len(rad["when"]) if rad else 0, "log": log[-40:], "started_at": started, "finished_at": now()}
        update("alert_runs", "id", run_id, {"status": "COMPLETE", "progress": 1.0, "result": result})
        audit(actor, "alert_run_completed", "alert_run", run_id, {"alerts": len(patches)})
        return {"run_id": run_id, **result}
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised for the job
        update("alert_runs", "id", run_id, {"status": "FAILED", "result": {"error": str(exc)[:500], "log": log[-40:]}})
        raise
