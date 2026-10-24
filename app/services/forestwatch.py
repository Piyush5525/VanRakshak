"""Forest-change watch: scan a map area for recent tree loss, keep reviewable records, verify them against
government records, find the responsible forest office, route to the site and notify, with a redacted activity log.

It reuses the CL-01 monthly alert engine (`alerts.detect` / `extract_patches`) over a user-chosen window ending today.
Everything here is a *candidate* from satellite evidence. Nothing states that an activity is unlawful: that is for the
forest authority after field verification. Missing data is reported as missing, never filled in.
"""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
import threading
import math
import re
import smtplib
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from typing import Any

import httpx
import numpy as np
from pyproj import Transformer
from scipy import ndimage
from rasterio import features as rio_features
from rasterio.warp import transform_geom
from shapely.geometry import Polygon, box, mapping
from shapely import set_precision as shp_set_precision
from shapely.ops import transform as shp_transform, unary_union

from app.core.config import get_settings
from app.core.geometry import geodesic_area_hectares, to_shape
from app.db.database import audit, connection, decode_row, get_one, insert, now, update
from app.services import alerts, boundary, firecomms, geoanalysis, navigation
from app.services.acquisition import provider
from app.services.osm import OsmUnavailable, fetch
from app.services.routing import haversine_km
from app.services.satellite import AcquisitionConfig, ProviderError
from app.services.stacks import add_months, s1_orbit, series

settings = get_settings()
HI_RES_KM2 = 60.0     # up to this size a scan reads 30 m pixels; larger areas use 60 m so they stay quick
MAX_AREA_KM2 = 240.0
MIN_WINDOW, MAX_WINDOW = 3, 180
HISTORY_DAYS = 90   # every scan reaches back this far so the tree-cover history (90 / 30 / 7 days ago) can be drawn
SNAPSHOTS = (90, 30, 7, 0)
STATUSES = ("NEEDS_REVIEW", "CONFIRMED", "FALSE_POSITIVE", "FIELD_CHECK", "REPORTED", "RESOLVED", "SUPERSEDED")
SETTABLE = ("NEEDS_REVIEW", "CONFIRMED", "FALSE_POSITIVE", "FIELD_CHECK", "RESOLVED")
NOTIFIABLE = ("CONFIRMED", "FIELD_CHECK", "REPORTED")
CATEGORIES = {"suspected_felling": "Suspected tree felling", "fire_damage": "Fire damage", "agriculture": "Agriculture / clearing for crops", "authorised_work": "Authorised work", "natural": "Natural cause (storm, landslide, flood)", "infrastructure": "Road / building / infrastructure", "other": "Other"}
VERIFICATION = {
    "AUTHORIZED_RECORD_MATCH": ("A matching, valid government record was found", "A person must still confirm that the work matches its scope and conditions."),
    "PARTIAL_OR_UNCLEAR_RECORD": ("A record overlaps but does not clearly cover this change", "Ask the issuing authority whether this clearing is covered."),
    "EXPIRED_OR_REVOKED_RECORD": ("A record overlaps but it is expired, revoked or outside its dates", "Check the record's validity with the issuing authority."),
    "NO_RECORD_FOUND": ("No government record in this system covers the area", "This does not mean the work is illegal."),
    "INSUFFICIENT_EVIDENCE": ("The satellite evidence is too weak to act on yet", "Wait for another clear pass or inspect on the ground."),
}
_ORDER = ["AUTHORIZED_RECORD_MATCH", "PARTIAL_OR_UNCLEAR_RECORD", "EXPIRED_OR_REVOKED_RECORD"]
_MAP = {"MATCH_REQUIRES_REVIEW": "AUTHORIZED_RECORD_MATCH", "POTENTIAL_MATCH": "AUTHORIZED_RECORD_MATCH", "POSSIBLE_SCOPE_OR_CONDITION_MISMATCH": "PARTIAL_OR_UNCLEAR_RECORD", "AUTHORIZATION_STATUS_UNCLEAR": "PARTIAL_OR_UNCLEAR_RECORD", "RECORD_EXPIRED_OR_REVOKED_CANDIDATE": "EXPIRED_OR_REVOKED_RECORD"}


def _dec(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """`authority` is plain text in other tables, so it is decoded here rather than globally."""
    if row and isinstance(row.get("authority"), str):
        try:
            row["authority"] = __import__("json").loads(row["authority"])
        except ValueError:
            pass
    return row


def _get(record_id: str) -> dict[str, Any] | None:
    return _dec(get_one("fc_records", "id", record_id))


class WatchError(RuntimeError):
    """A workflow or safety gate refused an action; the message is shown to the user."""


# ------------------------------------------------------------------ logging with redaction
_PATTERNS = ((re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"), "[token]"), (re.compile(r"([?&](?:key|token|access_token|api_key)=)[^&\s]+", re.I), r"\1[hidden]"), (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"), (re.compile(r"(?<![\d-])(?:\+\d[\d\s-]{8,}\d|\d{10,})(?![\d-])"), lambda m: "…" + re.sub(r"\D", "", m.group())[-3:]))


def redact(text: Any) -> Any:
    """Remove tokens, API keys in URLs, e-mail addresses and phone numbers from log text (recursively for dicts/lists)."""
    if isinstance(text, dict):
        return {k: ("[hidden]" if re.search(r"token|secret|password|api_?key", str(k), re.I) else redact(v)) for k, v in text.items()}
    if isinstance(text, (list, tuple)):
        return [redact(v) for v in text]
    if not isinstance(text, str):
        return text
    for pat, rep in _PATTERNS:
        text = pat.sub(rep, text)
    return text


def log(event_type: str, service: str, status: str, message: str, *, run_id: str | None = None, record_id: str | None = None, error: str | None = None, actor: str | None = None, details: dict | None = None) -> None:
    with connection() as conn:
        conn.execute("INSERT INTO fc_events (ts, run_id, record_id, event_type, service, status, message, error, actor, details) VALUES (?,?,?,?,?,?,?,?,?,?)", (now(), run_id, record_id, event_type, service, status, redact(message)[:600], redact(error)[:400] if error else None, actor, __import__("json").dumps(redact(details or {}))))


def events(record_id: str | None = None, run_id: str | None = None, since_id: int = 0, limit: int = 200, service: str | None = None) -> list[dict[str, Any]]:
    q, a = "SELECT * FROM fc_events WHERE id > ?", [since_id]
    for col, v in (("record_id", record_id), ("run_id", run_id), ("service", service)):
        if v:
            q += f" AND {col} = ?"; a.append(v)
    with connection() as conn:
        rows = [decode_row(dict(r)) for r in conn.execute(q + " ORDER BY id DESC LIMIT ?", (*a, limit))]
    return list(reversed(rows))


# ------------------------------------------------------------------ validation
def bbox_area_km2(b: list[float]) -> float:
    w, s, e, n = b
    return abs(e - w) * 111.32 * math.cos(math.radians((s + n) / 2)) * abs(n - s) * 110.57


def polygon_area_km2(polygon: dict[str, Any]) -> float:
    return geodesic_area_hectares(to_shape(polygon)) / 100.0


def resolution_for(area_km2: float) -> float:
    return 30.0 if area_km2 <= HI_RES_KM2 else 60.0


def validate(bbox: list[float], window_days: int | None, start: date | None, end: date | None, polygon: dict[str, Any] | None = None) -> tuple[list[float], date, date]:
    if len(bbox) != 4 or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in bbox):
        raise WatchError("bbox must be [west, south, east, north]")
    w, s, e, n = (float(x) for x in bbox)
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise WatchError("bbox is not a valid area")
    area = bbox_area_km2([w, s, e, n])
    if polygon is not None:
        if polygon.get("type") not in ("Polygon", "MultiPolygon") or not to_shape(polygon).is_valid:
            raise WatchError("The drawn shape is not a valid area")
        area = polygon_area_km2(polygon)
    if area > MAX_AREA_KM2:
        raise WatchError(f"This area is {area:.0f} km², above the {MAX_AREA_KM2:.0f} km² a single scan can read from the satellites. Draw a smaller area (several scans build up the map).")
    if not boundary.contains((s + n) / 2, (w + e) / 2):
        raise WatchError("The centre of this area is outside India; this prototype scans India only")
    today = datetime.now(timezone.utc).date()
    end = end or today
    if end > today:
        raise WatchError("The window cannot end in the future")
    start = start or (end - timedelta(days=window_days or 30))
    days = (end - start).days
    if days < MIN_WINDOW or days > MAX_WINDOW:
        raise WatchError(f"The window must be {MIN_WINDOW}-{MAX_WINDOW} days (got {days})")
    return [round(w, 5), round(s, 5), round(e, 5), round(n, 5)], start, end


# ------------------------------------------------------------------ scan
def _step(run_id: str, steps: list[dict], msg: str, frac: float | None = None, status: str = "ok") -> None:
    with connection() as conn:
        row = conn.execute("SELECT status FROM fc_runs WHERE id = ?", (run_id,)).fetchone()
    if row and row[0] == "CANCELLED":
        raise WatchError("Cancelled")
    steps.append({"t": now(), "status": status, "message": msg})
    update("fc_runs", "id", run_id, {"status": "RUNNING", "result": {"steps": steps[-60:]}, **({"progress": frac} if frac is not None else {})})
    log("scan_step", "pipeline", status, msg, run_id=run_id)


def scan(run_id: str, bbox: list[float], start: date, end: date, use_radar: bool, actor: str, polygon: dict[str, Any] | None = None) -> dict[str, Any]:
    steps: list[dict] = []
    params = alerts.AlertParams()
    geom = mapping(box(*bbox))
    try:
        area_km2 = polygon_area_km2(polygon) if polygon else bbox_area_km2(bbox)
        cfg = AcquisitionConfig(resolution_m=resolution_for(area_km2))
        _step(run_id, steps, f"Scanning {area_km2:.1f} km² at {cfg.resolution_m:.0f} m from {start} to {end} ({(end - start).days} days) with Sentinel-2 optical" + (" + Sentinel-1 radar" if use_radar else " (radar off)"), 0.02)
        ref_ym = add_months(start.year, start.month, -12)
        months = needed_months(start, end)
        n_months = len(months)
        orbit = None
        if use_radar:
            try:
                orbit = s1_orbit(geom, date(*ref_ym, 1), end)
                _step(run_id, steps, f"Sentinel-1 orbit chosen: {orbit['orbit_state']} #{orbit['relative_orbit']} ({orbit['scenes_in_period']} scenes)", 0.05)
            except ProviderError as exc:
                use_radar = False
                _step(run_id, steps, f"Radar unavailable ({exc}); continuing with optical only", 0.05, "warning")
        total = n_months * (2 if use_radar else 1)
        counts = {"n": 0}

        def tick(msg: str, done: int, of: int) -> None:
            counts["n"] += 1
            _step(run_id, steps, msg, round(0.06 + 0.8 * counts["n"] / total, 3))

        with ThreadPoolExecutor(2) as pool:   # optical and radar are independent: read them together
            f_opt = pool.submit(series, geom, "sentinel-2", ref_ym, n_months, cfg, None, tick, months)
            f_rad = pool.submit(series, geom, "sentinel-1", ref_ym, n_months, cfg, orbit, tick, months) if use_radar else None
            opt = f_opt.result()
            rad = None
            if f_rad is not None:
                try:
                    rad = f_rad.result()
                except ProviderError as exc:
                    _step(run_id, steps, f"Radar series failed ({exc}); continuing with optical only", None, "warning")
        if not len(opt["when"]):
            raise ProviderError("No Sentinel-2 image could be read for this area" + (f" ({opt['warnings'][0]})" if opt.get("warnings") else "") + ". Try again in a minute.")
        _step(run_id, steps, f"Sentinel-2: {len(opt['when'])} usable date(s); Sentinel-1: {len(rad['when']) if rad else 0} date(s)", 0.88)
        ref_end = (start - alerts.EPOCH).days
        fused = alerts.detect(opt, rad, ref_end, ref_end, params, use_radar=rad is not None)
        grid = opt["grid"]
        if polygon:   # keep only what falls inside the shape the user drew
            inside = rio_features.rasterize([(transform_geom("EPSG:4326", grid.crs, polygon), 1)], out_shape=fused["forest"].shape, transform=grid.transform, fill=0, dtype="uint8").astype(bool)
            fused["evidence"][~inside] = 0
            fused["forest"] &= inside
        forest_basis = "greenness before the window (no land-cover map available)"
        try:   # farmland, gardens and scattered greenery are green too: keep only what the land-cover map calls tree cover
            lc = provider().worldcover_classes(geom, grid)
            tree = ndimage.binary_dilation(lc == 10, iterations=1)   # one pixel of tolerance between the 10 m map and the 30 m grid
            fused["evidence"][~tree] = 0
            fused["forest"] &= tree
            forest_basis = "ESA WorldCover tree cover (10 m), plus a green baseline"
            _step(run_id, steps, f"Forest limited to mapped tree cover: {int(fused['forest'].sum())} of the area's pixels", 0.9)
        except Exception as exc:  # noqa: BLE001 - the scan still works without it, and says so
            _step(run_id, steps, f"Land-cover map unavailable ({type(exc).__name__}); forest judged by greenness only", 0.9, "warning")
        patches = alerts.extract_patches(fused, grid, params)
        px_ha = abs(grid.transform.a * grid.transform.e) / 1e4
        win = [w for w in opt["when"] if w.date() >= start]
        rwin = [w for w in (rad["when"] if rad else []) if w.date() >= start]
        idx = [i for i, w in enumerate(opt["when"]) if w.date() >= start]
        clear = float(np.isfinite(opt["data"][idx]).mean()) if idx else 0.0
        warnings = []
        if len(win) < 2:
            warnings.append(f"Only {len(win)} clear Sentinel-2 date(s) fall inside this window. Change needs two consecutive clear observations to be confirmed, so recent clearing may not be visible yet. Try a longer window.")
        if clear < 0.35 and idx:
            warnings.append(f"Mean clear-sky coverage in the window is {clear:.0%}: clouds hide much of the area.")
        forest_ha = float(fused["forest"].sum()) * px_ha
        created, linked = _ingest(run_id, patches, actor)
        retired = _retire_unseen(run_id, polygon if polygon else mapping(box(*bbox)), start)
        timeline = _timeline(fused, patches, grid, end, (end - start).days)
        if forest_basis.startswith("greenness"):
            warnings.append("No land-cover map was available, so crops and gardens may be counted as forest.")
        summary = {"retired_spots": retired, "forest_basis": forest_basis, "forest_ha": round(forest_ha, 1), "loss_ha": round(sum(p["area_ha"] for p in patches), 2), "loss_pct_of_forest": round(100 * sum(p["area_ha"] for p in patches) / forest_ha, 2) if forest_ha else None, "patches": len(patches), "new_records": created, "linked_to_existing": linked, "optical_dates_in_window": len(win), "radar_dates_in_window": len(rwin), "mean_clear_fraction_in_window": round(clear, 3), "radar_used": rad is not None, "resolution_m": cfg.resolution_m, "warnings": warnings}
        summary["timeline_points"] = len(timeline)
        _step(run_id, steps, f"Done: {len(patches)} loss patch(es), {created} new record(s), {linked} matched to existing records" + (f". Note: {warnings[0]}" if warnings else ""), 1.0)
        update("fc_runs", "id", run_id, {"status": "COMPLETE", "progress": 1.0, "result": {"steps": steps[-60:], "summary": summary, "timeline": timeline}, "finished_at": now()})
        audit(actor, "forest_scan_completed", "fc_run", run_id, {"records": created})
        return {"run_id": run_id, **summary}
    except Exception as exc:  # noqa: BLE001 - recorded then re-raised for the job
        if isinstance(exc, WatchError) and str(exc) == "Cancelled":
            raise
        _step(run_id, steps, f"Scan failed: {type(exc).__name__}: {str(exc)[:200]}", None, "failed")
        update("fc_runs", "id", run_id, {"status": "FAILED", "result": {"steps": steps[-60:], "error": str(exc)[:300]}, "finished_at": now()})
        raise


def _poly(mask: np.ndarray, grid, tol: float = 0.00015, min_deg2: float = 1.5e-7) -> dict[str, Any] | None:
    """Pixels -> simplified WGS84 MultiPolygon (None when empty). Specks under ~0.15 ha are dropped to keep the payload small."""
    if not mask.any():
        return None
    polys = []
    for g, _ in rio_features.shapes(mask.astype("uint8"), mask=mask, transform=grid.transform):
        p = to_shape(transform_geom(grid.crs, "EPSG:4326", g)).simplify(tol, preserve_topology=True)
        for q in getattr(p, "geoms", [p]):
            if q.is_empty or q.area < min_deg2:
                continue
            holes = [h for h in q.interiors if Polygon(h).area >= min_deg2 * 4]   # tiny holes are cloud/shadow gaps, not clearings
            polys.append(Polygon(q.exterior, holes))
    if not polys:
        return None
    u = shp_set_precision(unary_union(polys), 1e-5)
    return mapping(u) if not u.is_empty else None


def _timeline(fused: dict[str, Any], patches: list[dict[str, Any]], grid, end: date, span_days: int) -> list[dict[str, Any]]:
    """Tree cover and cumulative loss as they stood N days ago, for N in 90/30/7/0 (only those inside the scanned span)."""
    loss_pix = np.zeros(fused["forest"].shape, dtype=bool)
    for p in patches:
        loss_pix[tuple(np.array(p["pixels"]))] = True
    end_day = (end - alerts.EPOCH).days
    px_ha = abs(grid.transform.a * grid.transform.e) / 1e4
    out = []
    for k in SNAPSHOTS:
        if k > span_days:
            continue
        lost = loss_pix & (fused["est_day"] <= end_day - k)
        cover = fused["forest"] & ~lost
        out.append({"days_ago": k, "date": (end - timedelta(days=k)).isoformat(), "forest_ha": round(float(cover.sum()) * px_ha, 2), "lost_ha": round(float(lost.sum()) * px_ha, 2), "forest": _poly(cover, grid), "lost": _poly(lost, grid)})
    return out


def needed_months(start: date, end: date) -> list[tuple[int, int]]:
    """Months worth reading. A pixel is compared with the same time of year (+/- 30 days) before the window, so only three stretches
    matter: the same season last year, the weeks just before the window, and the window itself. The months in between are skipped."""
    spans = [(start - timedelta(days=365 + 31), end - timedelta(days=365 - 31)), (start - timedelta(days=31), end)]
    out: list[tuple[int, int]] = []
    for a, b in spans:
        d = date(a.year, a.month, 1)
        while d <= b:
            if (d.year, d.month) not in out:
                out.append((d.year, d.month))
            d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return sorted(out)


def run_area(run: dict[str, Any]):
    """The shape a scan covered: the polygon the user drew, or the box."""
    poly = (run.get("params") or {}).get("polygon")
    return to_shape(poly) if poly else box(*run["bbox"])


def _col(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def grid(run_id: str, cell_m: float | None = None) -> dict[str, Any]:
    """Split a scanned area into square cells (A1, A2, B1…). Each cell reports how much tree cover it had 90 / 30 / 7 days ago and
    today, and how much was lost in the last 7 / 30 / 90 days, with the loss spots inside it."""
    run = get_one("fc_runs", "id", run_id)
    if not run or run["status"] != "COMPLETE":
        raise WatchError("That scan is not finished")
    res = run["result"] or {}
    snaps = {s["days_ago"]: s for s in res.get("timeline", [])}
    area = run_area(run)
    c = area.centroid
    to_m = Transformer.from_crs("EPSG:4326", f"+proj=aeqd +lat_0={c.y} +lon_0={c.x} +datum=WGS84", always_xy=True)
    to_ll = Transformer.from_crs(f"+proj=aeqd +lat_0={c.y} +lon_0={c.x} +datum=WGS84", "EPSG:4326", always_xy=True)
    area_m = shp_transform(to_m.transform, area)
    minx, miny, maxx, maxy = area_m.bounds
    cell = cell_m or max(250.0, math.ceil(math.sqrt(area_m.area / 360.0) / 50.0) * 50.0)
    cover = {d: (shp_transform(to_m.transform, to_shape(s["forest"])) if s.get("forest") else None) for d, s in snaps.items()}
    today = date.fromisoformat(run["window_end"])
    with connection() as conn:
        rows = [_dec(decode_row(dict(r))) for r in conn.execute("SELECT * FROM fc_records WHERE lon BETWEEN ? AND ? AND lat BETWEEN ? AND ? AND status NOT IN ('FALSE_POSITIVE', 'SUPERSEDED')", (run["bbox"][0], run["bbox"][2], run["bbox"][1], run["bbox"][3]))]
    spots = [(r, shp_transform(to_m.transform, to_shape(r["geometry"])), (today - date.fromisoformat(r["est_date"])).days) for r in rows]
    spots = [x for x in spots if 0 <= x[2] <= 90 and area_m.intersects(x[1])]
    ha = lambda g: round(g.area / 1e4, 2) if g is not None and not g.is_empty else 0.0  # noqa: E731
    cells, ncols = [], int(math.ceil((maxx - minx) / cell))
    nrows = int(math.ceil((maxy - miny) / cell))
    for ix in range(ncols):
        for iy in range(nrows):
            sq = box(minx + ix * cell, maxy - (iy + 1) * cell, minx + (ix + 1) * cell, maxy - iy * cell)
            part = sq.intersection(area_m)
            if part.is_empty or part.area < cell * cell * 0.05:
                continue
            cov = {str(d): ha(g.intersection(part)) if g is not None else 0.0 for d, g in cover.items()}
            loss = {"7": 0.0, "30": 0.0, "90": 0.0}
            inside = []
            for r, g, age in spots:
                if not g.intersects(part):
                    continue
                a = g.intersection(part).area / 1e4
                for w in (7, 30, 90):
                    if age <= w:
                        loss[str(w)] += a
                inside.append({"id": r["id"], "code": r["code"], "area_ha": r["area_ha"], "est_date": r["est_date"], "age_days": age, "status": r["status"]})
            if not inside and not any(cov.values()):
                continue
            loss = {k: round(v, 2) for k, v in loss.items()}
            cells.append({"id": f"{_col(ix)}{iy + 1}", "polygon": mapping(shp_set_precision(shp_transform(to_ll.transform, part), 1e-5)), "area_ha": ha(part), "cover_ha": cov, "loss_ha": loss, "spots": sorted(inside, key=lambda s: -s["area_ha"])})
    cells.sort(key=lambda c_: (-c_["loss_ha"]["90"], c_["id"]))
    return {"run_id": run_id, "cell_m": cell, "window_end": run["window_end"], "cells": cells, "totals": {k: round(sum(c_["loss_ha"][k] for c_ in cells), 2) for k in ("7", "30", "90")}}


def _retire_unseen(run_id: str, area: dict[str, Any], start: date) -> int:
    """A spot that no one has judged, from an earlier scan of this same ground, that a fresh and stricter scan did not find again,
    is set aside as 'not seen again' (it stays in the record and can be brought back with Undo). People's decisions are never touched."""
    from shapely.geometry import Point
    shp = to_shape(area).buffer(1e-4)
    w, s_, e, n = shp.bounds
    with connection() as conn:
        rows = [dict(r) for r in conn.execute("SELECT id, lon, lat, history FROM fc_records WHERE status = 'NEEDS_REVIEW' AND last_run_id != ? AND est_date >= ? AND lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?", (run_id, start.isoformat(), w, e, s_, n))]
    count = 0
    for r in rows:
        if not shp.contains(Point(r["lon"], r["lat"])):
            continue
        hist = (json.loads(r["history"]) if r["history"] else []) + [{"at": now(), "by": "system", "change": "not seen again in a newer scan of this ground (set aside; Undo brings it back)"}]
        update("fc_records", "id", r["id"], {"status": "SUPERSEDED", "history": hist, "updated_at": now()})
        count += 1
    return count


def severity(area_ha: float) -> str:
    return "HIGH" if area_ha >= 5 else "MEDIUM" if area_ha >= 1 else "LOW"


def _ingest(run_id: str, patches: list[dict[str, Any]], actor: str) -> tuple[int, int]:
    created = linked = 0
    for p in patches:
        shp = to_shape(p["geometry"])
        pt = shp.representative_point()
        hit = _overlapping(shp)
        if hit:
            rec = hit
            hist = (rec.get("history") or []) + [{"at": now(), "by": "system", "change": f"seen again in scan {run_id[:8]}"}]
            update("fc_records", "id", rec["id"], {"seen_count": rec["seen_count"] + 1, "last_run_id": run_id, "history": hist, **({"status": "NEEDS_REVIEW"} if rec["status"] == "SUPERSEDED" else {}), "updated_at": now()})
            log("record_linked", "pipeline", "ok", f"Patch matched existing record {rec['code']} (not duplicated)", run_id=run_id, record_id=rec["id"])
            linked += 1
            continue
        rid = str(uuid.uuid4())
        with connection() as conn:
            n = conn.execute("SELECT COUNT(*) FROM fc_records").fetchone()[0] + 1
        b = shp.bounds
        insert("fc_records", {"id": rid, "code": f"FC-{n:04d}", "run_id": run_id, "last_run_id": run_id, "seen_count": 1, "status": "NEEDS_REVIEW", "category": None, "severity": severity(p["area_ha"]), "area_ha": p["area_ha"], "est_date": p["est_date"], "date_uncertainty_days": p["date_uncertainty_days"], "issue_date": p["issue_date"], "evidence": p["evidence"], "score": p["score"], "lat": round(pt.y, 6), "lon": round(pt.x, 6), "geometry": mapping(shp), "bbox": list(b), "verification": None, "authority": None, "route": None, "notify": {"history": []}, "notes": "", "history": [{"at": now(), "by": "system", "change": "created from satellite scan"}], "details": {"evidence_mix": p.get("evidence_mix"), "claim_type": "candidate_inference"}, "created_at": now(), "updated_at": now()})
        log("record_created", "pipeline", "ok", f"Record FC-{n:04d}: {p['area_ha']} ha, estimated {p['est_date']} (±{p['date_uncertainty_days']} d), evidence {p['evidence']}", run_id=run_id, record_id=rid)
        verify(rid, actor="system")
        created += 1
    return created, linked


def _overlapping(shp) -> dict[str, Any] | None:
    w, s, e, n = shp.bounds
    with connection() as conn:
        rows = [_dec(decode_row(dict(r))) for r in conn.execute("SELECT * FROM fc_records WHERE lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?", (w - 0.02, e + 0.02, s - 0.02, n + 0.02))]
    best, ratio = None, 0.0
    for r in rows:
        other = to_shape(r["geometry"])
        if not other.intersects(shp):
            continue
        ov = other.intersection(shp).area / max(min(other.area, shp.area), 1e-12)
        if ov > ratio:
            best, ratio = r, ov
    return best if best and ratio >= 0.3 else None


# ------------------------------------------------------------------ verification against records
def verify(record_id: str, actor: str = "system") -> dict[str, Any]:
    rec = _get(record_id)
    shp = to_shape(rec["geometry"])
    with connection() as conn:
        auths = [decode_row(dict(r)) for r in conn.execute("SELECT * FROM authorizations")]
    with_geom = [a for a in auths if a.get("geometry")]
    best, best_rank, considered = None, 99, []
    obs = datetime.fromisoformat(rec["est_date"]).replace(tzinfo=timezone.utc)
    for a in with_geom:
        try:
            if not to_shape(a["geometry"]).intersects(shp):
                continue
            r = geoanalysis.assess_candidate(rec["geometry"], obs, a)
        except Exception:  # noqa: BLE001 - one malformed record must not block the rest
            continue
        cls = _MAP.get(r["state"])
        if cls:
            d = a.get("dates") or {}
            considered.append({"id": a["id"], "project": a.get("project_name"), "number": a.get("project_identifier"), "authority": a.get("authority"), "status": a.get("status"), "valid_from": d.get("valid_from") or d.get("issued"), "valid_to": d.get("valid_to"), "source_url": a.get("source_url"), "entered_by": (a.get("evidence") or {}).get("entered_by_role"), "state": r["state"], "class": cls, "overlap": r["overlap_fraction"], "temporal": r["temporal"]})
            rank = _ORDER.index(cls)
            if rank < best_rank:
                best, best_rank = cls, rank
    weak = (rec["date_uncertainty_days"] or 0) > 45 or rec["area_ha"] < 0.3
    cls = best or ("INSUFFICIENT_EVIDENCE" if weak else "NO_RECORD_FOUND")
    out = {"class": cls, "meaning": VERIFICATION[cls][0], "next_step": VERIFICATION[cls][1], "records_in_system": len(with_geom), "permits": considered[:6], "overlapping_records": considered[:6], "checked_at": now(), "notice": "A candidate from satellite evidence compared with the records loaded in this system. Not a legal finding."}
    update("fc_records", "id", record_id, {"verification": out, "updated_at": now()})
    log("record_verified", "verification", "ok", f"Permit check: {VERIFICATION[cls][0]}", record_id=record_id, actor=actor, details={"overlapping": len(considered)})
    return out


# ------------------------------------------------------------------ queries
def add_permit(record_id: str, data: dict[str, Any], actor: str) -> dict[str, Any]:
    """A permit someone knows applies to this spot, stored with who entered it and where it came from. Its outline is taken to be
    this spot's outline (plus a small margin), because the issuing authority's boundary is not available here; the card says so."""
    rec = _get(record_id)
    if not rec:
        raise WatchError("Record not found")
    outline = mapping(to_shape(rec["geometry"]).buffer(0.0003))
    pid = str(uuid.uuid4())
    insert("authorizations", {"id": pid, "authority": data["authority"], "project_identifier": data.get("permit_number"), "project_name": data.get("project_name"), "status": data["status"], "geometry": outline,
                              "dates": {k: v for k, v in (("valid_from", data.get("valid_from")), ("valid_to", data.get("valid_to"))) if v}, "source_url": data.get("source_url"),
                              "evidence": {"entered_by_role": actor, "coverage": "Outline assumed to cover this spot, as entered by a person; the authority's own boundary was not available.", "note": data.get("note")}, "created_at": now()})
    audit(actor, "permit_added", "authorization", pid, {"record": rec["code"], "number": data.get("permit_number")})
    log("permit_added", "verification", "ok", f"Permit {data.get('permit_number') or data.get('project_name') or ''} added by {actor}".strip(), record_id=record_id, actor=actor)
    return verify(record_id, actor)


def remove_permit(record_id: str, permit_id: str, actor: str) -> dict[str, Any]:
    p = get_one("authorizations", "id", permit_id)
    if not p or not (p.get("evidence") or {}).get("entered_by_role"):
        raise WatchError("Only a permit that was entered by hand can be removed here")
    with connection() as conn:
        conn.execute("DELETE FROM authorizations WHERE id = ?", (permit_id,))
    audit(actor, "permit_removed", "authorization", permit_id, {})
    log("permit_removed", "verification", "ok", "A permit entered by hand was removed", record_id=record_id, actor=actor)
    return verify(record_id, actor)


def delete_record(record_id: str, actor: str) -> None:
    rec = _get(record_id)
    if not rec:
        raise WatchError("Record not found")
    with connection() as conn:
        conn.execute("DELETE FROM fc_events WHERE record_id = ?", (record_id,))
        conn.execute("DELETE FROM fc_records WHERE id = ?", (record_id,))
    audit(actor, "forest_record_deleted", "fc_record", record_id, {"code": rec["code"]})


def public(r: dict[str, Any], full: bool = False) -> dict[str, Any]:
    est = date.fromisoformat(r["est_date"])
    age = (datetime.now(timezone.utc).date() - est).days
    geom = to_shape(r["geometry"]).simplify(0.00003, preserve_topology=True)
    out = {"id": r["id"], "code": r["code"], "status": r["status"], "category": r["category"], "category_label": CATEGORIES.get(r["category"]), "severity": r["severity"], "area_ha": r["area_ha"], "est_date": r["est_date"], "age_days": age, "date_uncertainty_days": r["date_uncertainty_days"], "evidence": r["evidence"], "score": r["score"], "lat": r["lat"], "lon": r["lon"], "geometry": mapping(geom), "seen_count": r["seen_count"], "verification": r["verification"], "notes": r["notes"], "created_at": r["created_at"], "has_authority": bool(r["authority"]), "reported": r["status"] == "REPORTED"}
    if full:
        out.update(authority=r["authority"], route=r["route"], notify=r["notify"], history=r["history"], details=r["details"], run_id=r["run_id"])
    return out


def query(start: date | None = None, end: date | None = None, bbox: list[float] | None = None, status: str | None = None, min_ha: float = 0.0, include_false: bool = False, limit: int = 1000, within=None) -> list[dict[str, Any]]:
    q, a = "SELECT * FROM fc_records WHERE area_ha >= ?", [min_ha]
    if start:
        q += " AND est_date >= ?"; a.append(start.isoformat())
    if end:
        q += " AND est_date <= ?"; a.append(end.isoformat())
    if bbox:
        q += " AND lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?"; a += [bbox[0], bbox[2], bbox[1], bbox[3]]
    if status:
        q += " AND status = ?"; a.append(status)
    elif not include_false:
        q += " AND status NOT IN ('FALSE_POSITIVE', 'SUPERSEDED')"
    with connection() as conn:
        rows = [_dec(decode_row(dict(r))) for r in conn.execute(q + " ORDER BY est_date DESC, area_ha DESC LIMIT ?", (*a, limit))]
    if within is not None:
        from shapely.geometry import Point
        rows = [r for r in rows if within.buffer(1e-4).contains(Point(r["lon"], r["lat"]))]
    return [public(r) for r in rows]


def coverage(start: date | None = None) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = [decode_row(dict(r)) for r in conn.execute("SELECT id, bbox, window_start, window_end, status, result, finished_at FROM fc_runs WHERE status = 'COMPLETE' ORDER BY created_at DESC LIMIT 200")]
    return [{"run_id": r["id"], "bbox": r["bbox"], "window_start": r["window_start"], "window_end": r["window_end"], "finished_at": r["finished_at"], "loss_ha": (r["result"] or {}).get("summary", {}).get("loss_ha"), "patches": (r["result"] or {}).get("summary", {}).get("patches")} for r in rows if not start or r["window_end"] >= start.isoformat()]


def review(record_id: str, actor: str, status: str | None = None, category: str | None = None, notes: str | None = None) -> dict[str, Any]:
    rec = _get(record_id)
    if not rec:
        raise WatchError("Record not found")
    changes, vals = [], {}
    if status and status != rec["status"]:
        if status not in SETTABLE:
            raise WatchError("Status 'REPORTED' is set only by sending a notification; choose one of: " + ", ".join(SETTABLE))
        vals["status"] = status; changes.append(f"status {rec['status']} → {status}")
    if category is not None and category != rec["category"]:
        if category and category not in CATEGORIES:
            raise WatchError("Unknown category")
        vals["category"] = category or None; changes.append(f"category → {CATEGORIES.get(category, 'none')}")
    if notes is not None and notes != rec["notes"]:
        vals["notes"] = notes[:2000]; changes.append("notes edited")
    if not vals:
        return public(rec, True)
    hist = (rec["history"] or []) + [{"at": now(), "by": actor, "change": "; ".join(changes)}]
    update("fc_records", "id", record_id, {**vals, "history": hist, "updated_at": now()})
    audit(actor, "forest_record_reviewed", "fc_record", record_id, vals)
    if "status" in vals:   # only a decision is a milestone; editing notes or the cause is not logged as one
        log("record_reviewed", "review", "ok", f"Marked “{ {'CONFIRMED': 'Real loss', 'FIELD_CHECK': 'Needs a visit', 'FALSE_POSITIVE': 'Not a loss', 'RESOLVED': 'Resolved', 'NEEDS_REVIEW': 'To review'}[vals['status']] }” by {actor}", record_id=record_id, actor=actor)
    else:
        log("record_edited", "review", "ok", f"{rec['code']}: " + "; ".join(changes), record_id=record_id, actor=actor)
    return public(_get(record_id), True)


# ------------------------------------------------------------------ authority + route
def _store_office(name: str, lat: float, lon: float, source: str, ext: str, phone: str | None) -> bool:
    with connection() as conn:
        if conn.execute("SELECT 1 FROM stations WHERE source = ? AND external_ref = ?", (source, ext)).fetchone():
            return False
        for r in conn.execute("SELECT location FROM stations WHERE kind = 'forest_office'"):
            if haversine_km([lon, lat], decode_row({"location": r[0]})["location"]["coordinates"]) < 0.15:
                return False
    insert("stations", {"id": str(uuid.uuid4()), "name": name[:160], "jurisdiction": "unknown", "location": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]}, "coverage": None, "contacts": {"phone": phone} if phone else {}, "availability_note": None, "created_at": now(), "kind": "forest_office", "source": source, "verified": 0, "external_ref": ext})
    return True


def _office_near(lat: float, lon: float, km: float) -> bool:
    with connection() as conn:
        for r in conn.execute("SELECT location FROM stations WHERE kind = 'forest_office'"):
            if haversine_km([lon, lat], decode_row({"location": r[0]})["location"]["coordinates"]) <= km:
                return True
    return False


def discover_offices(lat: float, lon: float, radius_km: int = 80, record_id: str | None = None) -> dict[str, Any]:
    report, added = {}, 0
    if settings.serp_api:
        try:
            n = seen = 0
            for q in ("forest range office", "forest department office"):
                r = httpx.get("https://serpapi.com/search.json", params={"engine": "google_maps", "q": q, "ll": f"@{lat},{lon},10z", "type": "search", "api_key": settings.serp_api}, timeout=60).json()
                for x in r.get("local_results") or []:
                    gps = x.get("gps_coordinates")
                    if gps and "forest" in ((x.get("title") or "") + " " + (x.get("type") or "")).lower():
                        seen += 1
                        n += int(_store_office(x["title"], gps["latitude"], gps["longitude"], "serpapi", x.get("place_id") or f"{gps['latitude']},{gps['longitude']}", x.get("phone")))
            added += n; report["serpapi"] = {"status": "ok", "found": seen, "new": n}
        except Exception as exc:  # noqa: BLE001
            report["serpapi"] = {"status": "failed", "error": f"{type(exc).__name__}"}
    else:
        report["serpapi"] = {"status": "skipped", "reason": "SERP_API not configured"}
    if not _office_near(lat, lon, 40):   # OpenStreetMap is slow: ask it only when Google Maps found nothing close
        try:
            els = fetch(f'[out:json][timeout:25];(nwr["office"="forestry"](around:{radius_km * 1000},{lat},{lon});nwr["amenity"="ranger_station"](around:{radius_km * 1000},{lat},{lon});nwr["name"~"Forest (Range|Office|Division)|Range Office|Van Vibhag",i](around:{radius_km * 1000},{lat},{lon}););out center tags 40;')
            n = 0
            for e in els:
                la, lo = e.get("lat") or e.get("center", {}).get("lat"), e.get("lon") or e.get("center", {}).get("lon")
                t = e.get("tags", {})
                if la is not None:
                    n += int(_store_office(t.get("name") or "Forest office (unnamed)", la, lo, "osm", f"{e['type']}/{e['id']}", t.get("phone") or t.get("contact:phone")))
            added += n; report["osm"] = {"status": "ok", "found": len(els), "new": n}
        except OsmUnavailable as exc:
            report["osm"] = {"status": "failed", "error": str(exc)}
    ok = any(v.get("status") == "ok" for v in report.values())
    log("offices_discovered", "authority", "ok" if ok else "failed", f"Forest-office lookup: {added} new" + ("" if ok else " (all sources failed)"), record_id=record_id, details=report)
    return {"added": added, "sources": report}


def _best_time(r: dict[str, Any]) -> float:
    return r["adjusted_duration_min"] or r["duration_min"]


def assign_authority(record_id: str, router, actor: str, station_id: str | None = None) -> dict[str, Any]:
    """Pick who should go: the nearest forest office by road, with the nearest police stations alongside (and the other way round when
    no forest office is near). Everything is compared by road time; the others are listed so the person can switch."""
    from app.services import responders
    from app.services.firestations import _segments_small, _walk_url

    rec = _get(record_id)
    lat, lon = rec["lat"], rec["lon"]
    if not _office_near(lat, lon, 60):   # look up offices only when none is known near the site
        discover_offices(lat, lon, record_id=record_id)
    if not responders.near("police_station", lat, lon, 25):
        rep = responders.discover_police(lat, lon)
        log("police_discovered", "authority", "ok" if any(v.get("status") == "ok" for v in rep["sources"].values()) else "failed", f"Police-station lookup: {rep['added']} new", record_id=record_id)
    if not responders.near("fire_station", lat, lon, 25):
        from app.services import firestations
        try:
            firestations.discover(lat, lon, 40, None, False)
        except Exception as exc:  # noqa: BLE001 - fire stations are extra; never block the main lookup
            log("fire_station_lookup_failed", "authority", "failed", f"Fire-station lookup failed: {type(exc).__name__}", record_id=record_id)
    cands = responders.nearest("forest_office", lat, lon, 2, 200) + responders.nearest("police_station", lat, lon, 2, 60) + responders.nearest("fire_station", lat, lon, 1, 60)
    if station_id:   # a person chose a specific one: make sure it is among those compared
        from app.db.database import get_one as _g
        row = _g("stations", "id", station_id)
        if row and row["location"].get("type") == "Point" and all(r["id"] != station_id for _, r in cands):
            cands.append((haversine_km([lon, lat], row["location"]["coordinates"]), row))
    if not cands:
        res = {"status": "NO_AUTHORITY_FOUND", "reason": "No forest office or police station was found nearby in OpenStreetMap or Google Maps. Add one manually or notify the configured response channel only."}
        update("fc_records", "id", record_id, {"authority": res, "updated_at": now()})
        log("authority_not_found", "authority", "failed", res["reason"], record_id=record_id, actor=actor, error=res["reason"])
        return res
    log("authority_candidates", "routing", "ok", f"Evaluating {len(cands)} responder(s) by road network", record_id=record_id, details={"candidates": [{"name": r["name"], "kind": r.get("kind"), "straight_line_km": round(d, 1)} for d, r in cands]})
    if not router.available:
        offices = [(d, r) for d, r in cands if r.get("kind") == "forest_office"]
        d, st = (offices or cands)[0]
        res = {"status": "ROUTING_UNAVAILABLE", "office": _office(st, d), "others": [_office(r, dd) for dd, r in cands if r["id"] != st["id"]][:3], "reason": "No routing provider configured; the nearest are shown by straight line, with no road route or time."}
        update("fc_records", "id", record_id, {"authority": res, "updated_at": now()})
        log("route_failed", "routing", "failed", res["reason"], record_id=record_id, error=res["reason"])
        return res
    hazards = navigation.active_hazards()
    done: list[dict[str, Any]] = []
    for d, st in cands:
        rt = router.route(st["location"]["coordinates"], [lon, lat], True)
        if rt["status"] != "ok":
            log("route_candidate_failed", "routing", "failed", f"No route from {st['name']}: {rt.get('reason') or rt['status']}", record_id=record_id, error=str(rt.get("reason"))[:200])
            continue
        ev = navigation.evaluate_routes([rt["primary"], *rt.get("alternatives", [])], hazards)
        if ev["recommended_index"] is not None:
            done.append({"st": st, "d": d, "route": ev["routes"][ev["recommended_index"]], "provider": rt["provider"]})
    if not done:
        res = {"status": "FAILED", "office": _office(cands[0][1], cands[0][0]), "reason": "No route could be calculated from the nearest responders"}
        update("fc_records", "id", record_id, {"authority": res, "updated_at": now()})
        log("route_failed", "routing", "failed", res["reason"], record_id=record_id, error=res["reason"])
        return res
    forced = next((x for x in done if x["st"]["id"] == station_id), None)
    forest = sorted([x for x in done if x["st"].get("kind") == "forest_office"], key=lambda x: _best_time(x["route"]))
    best = forced or (forest[0] if forest and _best_time(forest[0]["route"]) <= 120 else min([x for x in done if x["st"].get("kind") != "fire_station"] or done, key=lambda x: _best_time(x["route"])))
    st, r = best["st"], best["route"]
    geom = r.get("geometry")
    if geom and len(geom["coordinates"]) > 400:
        step = len(geom["coordinates"]) / 400
        geom = {"type": "LineString", "coordinates": [geom["coordinates"][int(i * step)] for i in range(400)] + [geom["coordinates"][-1]]}
    route = {"status": "OK", "provider": best["provider"], "distance_km": r["distance_km"], "duration_min": r["duration_min"], "adjusted_duration_min": r["adjusted_duration_min"], "geometry": geom, "segments": _segments_small(r.get("segments")), "drive_km": r.get("drive_km"), "walk_km": r.get("walk_km"), "drive_min": r.get("drive_min"), "walk_min": r.get("walk_min"),
             "walk_url": _walk_url(r.get("segments"), lat, lon), "reasons": r["reasons"], "navigation_url": f"https://www.google.com/maps/dir/?api=1&origin={st['location']['coordinates'][1]},{st['location']['coordinates'][0]}&destination={lat},{lon}&travelmode=driving", "calculated_at": now(),
             "caveats": ["Road-network estimate, not live traffic.", "Forest tracks near the site may be missing from road data; the final approach may be on foot.", "The responder is unverified public mapping data: its jurisdiction and availability are not confirmed."]}
    def resp(x: dict[str, Any]) -> dict[str, Any]:   # a responder with its own road/walk route, so any of them can be shown on the map
        rr, g = x["route"], x["route"].get("geometry")
        if g and len(g["coordinates"]) > 200:
            step = len(g["coordinates"]) / 200
            g = {"type": "LineString", "coordinates": [g["coordinates"][int(i * step)] for i in range(200)] + [g["coordinates"][-1]]}
        c = x["st"]["location"]["coordinates"]
        return {**_office(x["st"], x["d"]), "distance_km": rr["distance_km"], "duration_min": rr["duration_min"], "drive_km": rr.get("drive_km"), "walk_km": rr.get("walk_km"), "drive_min": rr.get("drive_min"), "walk_min": rr.get("walk_min"), "geometry": g, "segments": _segments_small(rr.get("segments")),
                "walk_url": _walk_url(rr.get("segments"), lat, lon), "navigation_url": f"https://www.google.com/maps/dir/?api=1&origin={c[1]},{c[0]}&destination={lat},{lon}&travelmode=driving"}

    rest = sorted((y for y in done if y is not best), key=lambda y: _best_time(y["route"]))
    pol = next((x for x in rest if x["st"].get("kind") == "police_station"), None)
    police = resp(pol) if pol else ({"same_as_main": True} if st.get("kind") == "police_station" else None)
    others = [resp(x) for x in rest if x is not pol][:4]
    why = "chosen by you" if forced else "nearest forest office by road" if st.get("kind") == "forest_office" else "no forest office is close, so the nearest police station by road"
    auth = {"status": "OK", "v": 2, "office": _office(st, best["d"]), "police": police, "others": others, "selected": why, "assigned_at": now(), "assigned_by": actor}
    update("fc_records", "id", record_id, {"authority": auth, "route": route, "updated_at": now()})
    audit(actor, "forest_authority_assigned", "fc_record", record_id, {"office": st["name"], "kind": st.get("kind")})
    log("route_calculated", "routing", "ok", f"Route ready: {route['distance_km']} km, about {round(route['duration_min'])} min{' (' + str(route['walk_km']) + ' km of it on foot)' if route.get('walk_km') else ''}", record_id=record_id, actor=actor)
    log("authority_assigned", "authority", "ok", f"Assigned to {st['name']} ({LABELS.get(st.get('kind'), 'responder')}, details unverified)", record_id=record_id, actor=actor)
    return {**auth, "route": route}


LABELS = {"forest_office": "forest office", "police_station": "police station", "fire_station": "fire station"}


def _office(st: dict[str, Any], d: float) -> dict[str, Any]:
    c = st["location"]["coordinates"]
    k = st.get("kind") or "forest_office"
    return {"id": st["id"], "name": st["name"], "kind": k, "kind_label": LABELS.get(k, k).capitalize(), "icon": {"forest_office": "🌲", "police_station": "👮", "fire_station": "🚒"}.get(k, "📍"), "lat": c[1], "lon": c[0], "source": st.get("source"), "verified": bool(st.get("verified")), "straight_line_km": round(d, 1), "phone": ("…" + str(st["contacts"]["phone"])[-3:]) if (st.get("contacts") or {}).get("phone") else None, "data_note": "Unverified public mapping data"}


# ------------------------------------------------------------------ notifications
def _contact(o: dict[str, Any]) -> str | None:
    """Full number of a responder from public map data (the stored record), for the people who must call them."""
    row = get_one("stations", "id", o.get("id")) if o.get("id") else None
    return ((row or {}).get("contacts") or {}).get("phone")


def compose(rec: dict[str, Any], html_mode: bool = True) -> str:
    """The Telegram report: what happened, exactly where, how to get there, who is nearest, what to do. Readable on a phone in a few seconds."""
    from app.services import geoinfo

    e = escape if html_mode else (lambda s: s)
    b = (lambda s: f"<b>{escape(s)}</b>") if html_mode else (lambda s: s)
    code = (lambda s: f"<code>{escape(s)}</code>") if html_mode else (lambda s: s)
    link = (lambda label, url: f'<a href="{escape(url)}">{escape(label)}</a>') if html_mode else (lambda label, url: f"{label}: {url}")
    lat, lon = rec["lat"], rec["lon"]
    ver = (rec["verification"] or {}).get("class", "NOT_CHECKED")
    a, r = rec["authority"] or {}, rec["route"] or {}
    pl = geoinfo.place(lat, lon)
    est = date.fromisoformat(rec["est_date"])
    days = (datetime.now(timezone.utc).date() - est).days
    sev = {"HIGH": "🔴", "MEDIUM": "🟠"}.get(rec["severity"], "🟡")
    permit = {"NO_RECORD_FOUND": "❌ No permit in the register covers this spot", "AUTHORIZED_RECORD_MATCH": "✅ A permit in the register covers it (still to be checked on the ground)", "PARTIAL_OR_UNCLEAR_RECORD": "⚠️ A permit exists nearby; it may not cover this clearing", "EXPIRED_OR_REVOKED_RECORD": "⚠️ A permit exists but is expired or revoked"}.get(ver, "❔ Permit not checked")
    L = [f"🚨 {b('FOREST ALERT · ' + rec['code'])}", f"{sev} {b(str(rec['area_ha']) + ' ha of tree cover lost')} · {rec['severity'].title()} size", ""]
    L += [f"📍 {b('WHERE')}"]
    if pl:
        L.append(e(pl["name"]))
    L += [code(f"{lat:.5f}, {lon:.5f}"), link("🗺 Open in Google Maps", f"https://www.google.com/maps?q={lat},{lon}") + "  " + link("🛰 Satellite view", f"https://www.google.com/maps/@{lat},{lon},400m/data=!3m1!1e3"), "",
          f"🕒 {b('WHEN')}", e(f"About {est.strftime('%d %b %Y')} ({'in the last few days' if days <= 3 else str(days) + ' days ago'}, ±{round(rec['date_uncertainty_days'] or 0)} days)") + f" · seen in {rec.get('seen_count') or 1} satellite scan(s)", "",
          f"📄 {b('PERMIT')}", e(permit), ""]
    o = a.get("office")
    if o:
        L.append(f"{o.get('icon', '🏛')} {b('GO TO THE SITE FROM · ' + (o.get('kind_label') or 'Office'))}")
        L.append(e(o["name"]))
        if r.get("status") == "OK":
            L.append(e(f"🚗 {r['distance_km']} km, about {round(r['duration_min'])} min" + (f" · incl. 🚶 {r['walk_km']} km on foot" if r.get("walk_km") else "")))
            L.append(link("🚗 Directions by road", r["navigation_url"]) + (("  " + link("🚶 Walking directions", r["walk_url"])) if r.get("walk_url") else ""))
        ph = _contact(o)
        if ph:
            L.append(e(f"📞 {ph}"))
        L.append("")
    po = a.get("police")
    if po and not po.get("same_as_main"):
        L.append(f"👮 {b('NEAREST POLICE STATION · for an immediate response')}")
        L.append(e(po["name"]))
        L.append(e(f"🚗 {po['distance_km']} km, about {round(po['duration_min'])} min" + (f" · incl. 🚶 {po['walk_km']} km on foot" if po.get("walk_km") else "")))
        L.append(link("🚗 Directions by road", po["navigation_url"]))
        ph = _contact(po)
        if ph:
            L.append(e(f"📞 {ph}"))
        L.append("")
    elif o and o.get("kind") == "police_station":
        L.append(e("No forest office is close, so the nearest police station is the main responder."))
        L.append("")
    L += [f"✅ {b('WHAT TO DO')}", e("1. Go to the location above and check what has happened."), e("2. Take photos and note the time."), e("3. Reply here: real loss, or false alarm."), "",
          e("ℹ️ Found by satellite (Sentinel-2/1). It is a candidate change, not a finding that anything is unlawful. Responder details are from public maps and are unverified.")]
    return "\n".join(L)


def _prior(rec: dict[str, Any], channel: str, mode: str) -> bool:
    return any(h["channel"] == channel and h["mode"] == mode and h["status"] in ("SENT", "SIMULATED", "INITIATED") for h in (rec["notify"] or {}).get("history", []))


def notify(record_id: str, channels: list[str], mode: str, confirm_real: bool, actor: str, resend: bool = False) -> dict[str, Any]:
    rec = _get(record_id)
    if not rec:
        raise WatchError("Record not found")
    if rec["status"] not in NOTIFIABLE:
        log("notify_refused", "review", "skipped", f"{rec['code']} not notified: status is {rec['status']}; a person must confirm it first", record_id=record_id, actor=actor)
        raise WatchError("Confirm the record first: notifications are only sent for records a person has confirmed")
    real = mode == "real"
    if real and not confirm_real:
        raise WatchError("A real send needs explicit confirmation (confirm_real=true)")
    text, plain = compose(rec), compose(rec, False)
    results = []
    for ch in channels:
        if not resend and _prior(rec, ch, "real" if real else "simulated"):
            log("notify_duplicate_suppressed", ch, "skipped", f"{ch} already recorded for {rec['code']}; not sent again", record_id=record_id, actor=actor)
            results.append({"channel": ch, "status": "DUPLICATE_SUPPRESSED"})
            continue
        fn = {"dashboard": _n_dashboard, "telegram": _n_telegram, "ivr": _n_ivr, "email": _n_email}.get(ch)
        if not fn:
            raise WatchError(f"Unknown channel '{ch}'")
        res = fn(rec, text, plain, real, actor)
        res.update(channel=ch, mode="real" if real else "simulated", at=now())
        results.append(res)
    fresh = _get(record_id)
    hist = (fresh["notify"] or {"history": []})["history"] + [x for x in results if x["status"] != "DUPLICATE_SUPPRESSED"]
    vals = {"notify": {"history": hist}, "updated_at": now()}
    if real and any(x["status"] in ("SENT", "INITIATED") for x in results):
        vals["status"] = "REPORTED"
        vals["history"] = (fresh["history"] or []) + [{"at": now(), "by": actor, "change": "status → REPORTED (authority/responder notified)"}]
    update("fc_records", "id", record_id, vals)
    audit(actor, "forest_record_notified", "fc_record", record_id, {"channels": channels, "mode": mode})
    return {"results": results, "record": public(_get(record_id), True)}


def _n_dashboard(rec, text, plain, real, actor):
    log("dashboard_notified", "dashboard", "ok", "Flagged on the dashboard for the responsible team", record_id=rec["id"], actor=actor)
    return {"status": "SENT", "note": "Visible in the VanRakshak dashboard and activity log"}


def _pts(rt: dict[str, Any], who: dict[str, Any], lon: float, lat: float) -> list[list[float]]:
    """The drawable line of a route: its geometry, or the joined segments, or a straight line when the provider gave no path."""
    if (rt.get("geometry") or {}).get("coordinates"):
        return rt["geometry"]["coordinates"]
    pts = [c for sg in rt.get("segments") or [] for c in ((sg.get("geometry") or {}).get("coordinates") or [])]
    return pts or [[who["lon"], who["lat"]], [lon, lat]]


def evidence_images(rec: dict[str, Any]) -> list[tuple[str, bytes, str]]:
    """Two pictures for the responders: the lost area on satellite imagery, and the route(s) from the forest office / police station to it.
    Returns (filename, jpeg, caption); a picture that cannot be made is left out, never replaced by a stock image."""
    import io
    import json as _json
    from urllib.parse import quote

    from PIL import Image
    from app.services import mapimage as mi, tilemap

    lat, lon = rec["lat"], rec["lon"]
    a, r = rec.get("authority") or {}, rec.get("route") or {}
    out: list[tuple[str, bytes, str]] = []
    site = f"pin-l+d9382a({lon},{lat})"
    feat = {"type": "Feature", "properties": {"stroke": "#ff3b30", "stroke-width": 3, "fill": "#ff3b30", "fill-opacity": 0.35}, "geometry": {"type": "Polygon", "coordinates": [ring]}} if (ring := (mi._rings(rec.get("geometry"), 40) or [None])[0]) else None
    ov = ([f"geojson({quote(_json.dumps({'type': 'FeatureCollection', 'features': [feat]}, separators=(',', ':')), safe='')})"] if feat else []) + [site]
    head = [(f"VanRakshak  {rec['code']}  {rec['area_ha']} ha of tree cover lost", (255, 255, 255)), (f"{lat:.5f}, {lon:.5f} · about {rec['est_date']} · red = area lost", (255, 210, 120))]
    try:
        png = mi._mapbox(ov)
        base, credit = (Image.open(io.BytesIO(png)), "Satellite basemap: Mapbox, Maxar") if png else (tilemap.render([(lon - 0.004, lat - 0.003), (lon + 0.004, lat + 0.003)] + [tuple(c) for c in (ring or [])], [ring] if ring else [], [], [(lon, lat, "#d9382a")]), "Satellite basemap: Esri, Maxar")
        if base is not None:
            out.append((f"{rec['code']}-site.jpg", mi._banner(base, head, credit + " · candidate change, verify on the ground"), f"📍 {rec['code']} · the area where trees were lost (red). Coordinates {lat:.5f}, {lon:.5f}"))
    except Exception as exc:  # noqa: BLE001
        log("telegram_image_failed", "telegram", "failed", f"Site picture not made: {type(exc).__name__}", record_id=rec["id"])
    o, po = a.get("office"), a.get("police")
    legs = []
    if o and r.get("status") == "OK":
        legs.append((o, r, "14573c", "park" if o.get("kind") != "police_station" else "police"))
    if po and po.get("distance_km") is not None:
        legs.append((po, po, "7b1fa2", "police"))
    if legs:
        ov, lines2 = [], []
        for who, rt, col, icon in legs:
            ov.append(f"path-5+{col}-0.9({quote(mi._poly5(mi._thin(_pts(rt, who, lon, lat), 150)), safe='')})")
        for who, rt, col, icon in legs:
            ov.append(f"pin-l-{icon}+{col}({who['lon']},{who['lat']})")
            lines2.append((f"{who.get('kind_label', 'Responder')} · {who['name'][:34]}: {rt['distance_km']} km, ~{round(rt['duration_min'])} min", (190, 235, 205) if col == "14573c" else (225, 190, 245)))
        ov.append(site)
        try:
            png = mi._mapbox(ov)
            base = Image.open(io.BytesIO(png)) if png else tilemap.render([(lon, lat)] + [(who["lon"], who["lat"]) for who, *_ in legs], [], [(([who["lon"], who["lat"]], [lon, lat]) if False else (_pts(rt, who, lon, lat), "#" + col)) for who, rt, col, _i in legs], [(who["lon"], who["lat"], "#" + col) for who, rt, col, _i in legs] + [(lon, lat, "#d9382a")])
            if base is not None:
                cap = "🚗 Routes to the site: " + " · ".join(f"{who.get('kind_label', 'Responder')} {rt['distance_km']} km / {round(rt['duration_min'])} min" for who, rt, _, _ in legs)
                out.append((f"{rec['code']}-routes.jpg", mi._banner(base, [(f"Routes to {rec['code']}", (255, 255, 255))] + lines2, "Typical road time, not live traffic · responder details unverified"), cap))
        except Exception as exc:  # noqa: BLE001
            log("telegram_image_failed", "telegram", "failed", f"Route picture not made: {type(exc).__name__}", record_id=rec["id"])
    return out


def _n_telegram(rec, text, plain, real, actor):
    if not real:
        log("telegram_simulated", "telegram", "info", f"SIMULATED Telegram message composed for {rec['code']} (nothing was sent)", record_id=rec["id"], actor=actor)
        return {"status": "SIMULATED", "preview": plain}
    cfg = firecomms.configured()
    if not (cfg["token"] and cfg["chat_id"]):
        log("telegram_not_configured", "telegram", "failed", "Telegram is not configured (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)", record_id=rec["id"], error="unconfigured")
        return {"status": "NOT_CONFIGURED", "error": "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing"}
    err = None
    try:
        pics = evidence_images(rec)
    except Exception as exc:  # noqa: BLE001 - pictures are a bonus; the message must still go
        pics = []
        log("telegram_image_failed", "telegram", "failed", f"Pictures not made: {type(exc).__name__}", record_id=rec["id"])
    for attempt in range(1, 4):
        try:
            res = firecomms._call("sendMessage", {"chat_id": settings.telegram_chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"})
            for name, jpg, cap in pics:
                try:
                    firecomms._call("sendPhoto", {"chat_id": settings.telegram_chat_id, "caption": cap[:1000]}, {"photo": (name, jpg, "image/jpeg")})
                except firecomms.TelegramError as exc:
                    log("telegram_image_failed", "telegram", "failed", f"A picture was not accepted by Telegram: {exc}", record_id=rec["id"], error=str(exc)[:200])
            try:
                firecomms._call("sendLocation", {"chat_id": settings.telegram_chat_id, "latitude": rec["lat"], "longitude": rec["lon"]})
            except firecomms.TelegramError:
                pass
            log("telegram_sent", "telegram", "ok", f"Sent to Telegram (accepted by Telegram; not proof anyone has read it)", record_id=rec["id"], actor=actor)
            return {"status": "SENT", "message_id": res["message_id"], "attempts": attempt}
        except firecomms.TelegramError as exc:
            err = str(exc)
            if attempt < 3:
                log("telegram_retry", "telegram", "retried", f"Telegram failed ({err}); retry {attempt} of 2", record_id=rec["id"], error=err)
                time.sleep(2 * attempt)
    log("telegram_failed", "telegram", "failed", f"Telegram failed after 3 attempts: {err}", record_id=rec["id"], error=err)
    return {"status": "FAILED", "error": err, "attempts": 3}


def spoken(rec: dict[str, Any]) -> str:
    """The call script for a tree-loss record, written to be heard: what, where, how much, who is nearest, what to do."""
    from app.services import geoinfo

    lat, lon = rec["lat"], rec["lon"]
    pl = geoinfo.place(lat, lon)
    a, r = rec.get("authority") or {}, rec.get("route") or {}
    est = date.fromisoformat(rec["est_date"])
    days = (datetime.now(timezone.utc).date() - est).days
    when = "in the last few days" if days <= 3 else f"around {est.strftime('%d %B')}, about {days} days ago"
    parts = ["Hello. This is a VanRakshak forest alert. Please listen carefully.", f"Satellite images show about {rec['area_ha']:.1f} hectares of tree cover lost {('near ' + pl['name']) if pl else 'in a forest area'}, {when}."]
    v = (rec.get("verification") or {}).get("class")
    if v == "NO_RECORD_FOUND":
        parts.append("No permit covering this place was found in the system, so it needs to be checked.")
    elif v == "AUTHORIZED_RECORD_MATCH":
        parts.append("A matching permit was found, but it still needs to be checked.")
    elif v in ("PARTIAL_OR_UNCLEAR_RECORD", "EXPIRED_OR_REVOKED_RECORD"):
        parts.append("A permit exists nearby, but it may not cover this clearing.")
    if a.get("office") and r.get("status") == "OK":
        o = a["office"]
        police = next((x for x in a.get("others", []) if x.get("kind") == "police_station"), None)
        parts.append(f"The responsible {LABELS.get(o.get('kind'), 'office')} is {o['name']}, {round(r['distance_km'])} kilometres {geoinfo.direction_from(lat, lon, o['lat'], o['lon'])} of the site, {geoinfo.say_journey(r)}.")
    if a.get("office") and a["office"].get("kind") != "police_station":
        police = a.get("police") if a.get("police") and not a["police"].get("same_as_main") else None
        if police:
            parts.append(f"The nearest police station is {police['name']}, about {geoinfo.say_journey(police)}.")
    parts.append(f"The location is {abs(lat):.3f} degrees {'north' if lat >= 0 else 'south'}, {abs(lon):.3f} degrees {'east' if lon >= 0 else 'west'}. The record code is {rec['code'].replace('-', ' ')}.")
    parts.append("This comes from satellite images and must be checked on the ground. The map and route are in the VanRakshak app. Please verify and respond.")
    return " ".join(parts)


def _n_ivr(rec, text, plain, real, actor):
    script = spoken(rec)
    if not real:
        log("ivr_simulated", "ivr", "info", f"SIMULATED call for {rec['code']} (no person was called)", record_id=rec["id"], actor=actor)
        return {"status": "SIMULATED", "preview": script}
    from app.services import fireivr
    st = fireivr.status()
    if not st["real_ready"]:
        msg = "Phone calls are not set up: missing " + ", ".join(st["missing"] or ["a valid provider"])
        log("ivr_not_configured", "ivr", "failed", msg, record_id=rec["id"], error="unconfigured")
        return {"status": "NOT_CONFIGURED", "error": msg}
    ctx = {"reference": rec["code"], "latitude": rec["lat"], "longitude": rec["lon"], "area_hectares": rec["area_ha"], "kind": "tree cover loss"}
    res = fireivr.dial(script, ctx, sleep=lambda s_: (log("ivr_retry", "ivr", "retried", "The call request failed; trying again", record_id=rec["id"]), time.sleep(s_))[1])
    if res["ok"]:
        log("ivr_initiated", "ivr", "ok", f"Call placed for {rec['code']}", record_id=rec["id"], actor=actor)
        threading.Thread(target=_poll_call, args=(rec["id"], res["call_id"], res["provider"]), daemon=True).start()
        return {"status": "INITIATED", "provider_call_id": res["call_id"], "attempts": len(res["attempts"])}
    log("ivr_failed", "ivr", "failed", f"Call request failed: {res['error']}", record_id=rec["id"], error=res["error"])
    return {"status": "FAILED", "error": res["error"]}


_CALL_EVENT = {"RINGING": ("ivr_ringing", "ok", "Phone is ringing"), "ANSWERED": ("ivr_answered", "ok", "Call answered"), "COMPLETED": ("ivr_completed", "ok", "Call completed"), "BUSY": ("ivr_busy", "failed", "Line busy"), "NO_ANSWER": ("ivr_no_answer", "failed", "Not answered"), "FAILED": ("ivr_failed", "failed", "Call failed"), "CANCELLED": ("ivr_failed", "failed", "Call cancelled")}


def _poll_call(record_id: str, call_id: str | None, provider: str) -> None:
    """Follow a real call with the provider's own status, so the screen shows ringing / answered / completed as they happen."""
    from app.services import fireivr
    if not call_id:
        return
    seen = None
    gap = 3 if provider == "omnidimension" else 6
    for _ in range(int(240 / gap)):   # up to four minutes
        time.sleep(gap)
        d = fireivr.remote_detail(provider, call_id)
        st = d["status"] if d else None
        if st and st != seen and st in _CALL_EVENT:
            seen = st
            et, lvl, msg = _CALL_EVENT[st]
            if st == "COMPLETED" and d.get("duration_s") is not None:
                msg = f"Call completed ({d['duration_s']} s)"
            log(et, "ivr", lvl, msg, record_id=record_id)
            rec = _get(record_id)
            hist = (rec["notify"] or {"history": []})["history"]
            for h in reversed(hist):
                if h["channel"] == "ivr" and h["mode"] == "real":
                    h["status"] = st if st in ("RINGING", "ANSWERED", "COMPLETED") else "FAILED"; break
            update("fc_records", "id", record_id, {"notify": {"history": hist}, "updated_at": now()})
        if st in ("COMPLETED", "BUSY", "NO_ANSWER", "FAILED", "CANCELLED"):
            return


def refresh_calls(record_id: str) -> None:
    """Ask the provider about a call that is still marked in progress (its background follower may have been lost in a restart)."""
    from app.services import fireivr
    rec = _get(record_id)
    hist = (rec["notify"] or {"history": []})["history"] if rec else []
    for h in hist:
        if h["channel"] == "ivr" and h["mode"] == "real" and h["status"] in ("INITIATED", "RINGING", "ANSWERED") and h.get("provider_call_id"):
            d = fireivr.remote_detail(fireivr.settings.ivr_provider, h["provider_call_id"])
            if d and d["status"] in _CALL_EVENT and d["status"] != h["status"]:
                et, lvl, msg = _CALL_EVENT[d["status"]]
                if d["status"] == "COMPLETED" and d.get("duration_s") is not None:
                    msg = f"Call completed ({d['duration_s']} s)"
                log(et, "ivr", lvl, msg, record_id=record_id)
                h["status"] = d["status"] if d["status"] in ("RINGING", "ANSWERED", "COMPLETED") else "FAILED"
                update("fc_records", "id", record_id, {"notify": {"history": hist}, "updated_at": now()})


def _n_email(rec, text, plain, real, actor):
    to = settings.forest_alert_email_to
    if not real:
        log("email_simulated", "email", "info", f"SIMULATED e-mail composed for {rec['code']} (nothing was sent)", record_id=rec["id"], actor=actor)
        return {"status": "SIMULATED", "preview": plain}
    if not (settings.smtp_host and settings.smtp_from and to):
        log("email_not_configured", "email", "failed", "E-mail is not configured (SMTP_HOST, SMTP_FROM, FOREST_ALERT_EMAIL_TO)", record_id=rec["id"], error="unconfigured")
        return {"status": "NOT_CONFIGURED", "error": "SMTP_HOST / SMTP_FROM / FOREST_ALERT_EMAIL_TO missing"}
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = f"VanRakshak forest-change alert {rec['code']}", settings.smtp_from, to
    msg.set_content(plain)
    err = None
    for attempt in range(1, 4):
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as s:
                s.starttls()
                if settings.smtp_user:
                    s.login(settings.smtp_user, settings.smtp_password or "")
                s.send_message(msg)
            log("email_sent", "email", "ok", f"E-mail accepted by the SMTP server for {rec['code']}", record_id=rec["id"], actor=actor)
            return {"status": "SENT", "attempts": attempt}
        except (smtplib.SMTPException, OSError) as exc:
            err = type(exc).__name__
            if attempt < 3:
                log("email_retry", "email", "retried", f"E-mail failed ({err}); retry {attempt} of 2", record_id=rec["id"], error=err)
                time.sleep(2 * attempt)
    log("email_failed", "email", "failed", f"E-mail failed after 3 attempts: {err}", record_id=rec["id"], error=err)
    return {"status": "FAILED", "error": err}


def channel_status() -> dict[str, Any]:
    from app.services import fireivr
    tg = firecomms.configured()
    return {"dashboard": {"ready": True}, "telegram": {"ready": tg["token"] and tg["chat_id"], "missing": [k for k, v in (("TELEGRAM_BOT_TOKEN", tg["token"]), ("TELEGRAM_CHAT_ID", tg["chat_id"])) if not v]}, "ivr": {"ready": fireivr.status()["real_ready"], "provider": settings.ivr_provider, "missing": fireivr.status()["missing"]}, "email": {"ready": bool(settings.smtp_host and settings.smtp_from and settings.forest_alert_email_to), "missing": [k for k, v in (("SMTP_HOST", settings.smtp_host), ("SMTP_FROM", settings.smtp_from), ("FOREST_ALERT_EMAIL_TO", settings.forest_alert_email_to)) if not v]}}
