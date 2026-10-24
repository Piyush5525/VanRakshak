"""Forest-change watch API: scan an area, see tree cover and loss over time, review records, find the responsible office, notify."""
import json
from shapely.geometry import mapping
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import get_settings
from app.core.security import Role, require_roles
from app.db.database import audit, connection, decode_row, get_one, insert, now
from app.ops_routes import router_service
from app.services import forestwatch as fw
from app.services.jobs import JobManager

settings = get_settings()
api = APIRouter(prefix=settings.api_prefix + "/forest-change")
jobs = JobManager(settings.job_workers)
READ = (Role.ADMIN, Role.REVIEWER, Role.OFFICER, Role.ANALYST, Role.READ_ONLY)
SCAN = (Role.ADMIN, Role.REVIEWER, Role.ANALYST, Role.OFFICER)
REVIEW = (Role.ADMIN, Role.REVIEWER, Role.OFFICER)
SEND = (Role.ADMIN, Role.REVIEWER, Role.OFFICER)


class M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScanIn(M):
    bbox: list[float] = Field(min_length=4, max_length=4, description="[west, south, east, north]")
    window_days: int | None = Field(default=30, ge=3, le=180, description="what the user is looking at; the scan always reaches back at least 90 days")
    start: date | None = None
    end: date | None = None
    use_radar: bool = True
    polygon: dict | None = Field(default=None, description="optional GeoJSON Polygon drawn by the user; results are clipped to it")


class ReviewIn(M):
    status: str | None = None
    category: str | None = None
    notes: str | None = Field(default=None, max_length=2000)


class AuthorityIn(M):
    station_id: str | None = None


class NotifyIn(M):
    channels: list[Literal["dashboard", "telegram", "ivr", "email"]] = Field(default=["dashboard", "telegram"], min_length=1)
    mode: Literal["simulated", "real"] = "simulated"
    confirm_real: bool = False
    resend: bool = False


def _wf(fn, *a, **k):
    try:
        return fn(*a, **k)
    except fw.WatchError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


def _rec(rid: str) -> dict[str, Any]:
    r = fw._get(rid)
    if not r:
        raise HTTPException(status_code=404, detail="Record not found")
    return r


@api.get("/status")
def status(role: Role = Depends(require_roles(*READ))):
    with connection() as conn:
        n_auth = conn.execute("SELECT COUNT(*) FROM authorizations WHERE geometry IS NOT NULL").fetchone()[0]
        n_rec = conn.execute("SELECT COUNT(*) FROM fc_records").fetchone()[0]
    return {"routing_ready": router_service.available, "channels": fw.channel_status(), "permit_records_loaded": n_auth, "records": n_rec, "max_area_km2": fw.MAX_AREA_KM2, "sharp_area_km2": fw.HI_RES_KM2, "history_days": fw.HISTORY_DAYS, "categories": fw.CATEGORIES, "verification_classes": {k: {"meaning": v[0], "next_step": v[1]} for k, v in fw.VERIFICATION.items()}}


@api.post("/runs")
def start_scan(payload: ScanIn, role: Role = Depends(require_roles(*SCAN))):
    bbox, start, end = _wf(fw.validate, payload.bbox, payload.window_days, payload.start, payload.end, payload.polygon)
    view_days = (end - start).days
    scan_start = min(start, end - timedelta(days=fw.HISTORY_DAYS))
    rid = str(uuid.uuid4())
    insert("fc_runs", {"id": rid, "status": "QUEUED", "progress": 0.0, "bbox": bbox, "window_start": scan_start.isoformat(), "window_end": end.isoformat(), "window_days": (end - scan_start).days, "params": {"view_days": view_days, "use_radar": payload.use_radar, "polygon": payload.polygon}, "result": {"steps": []}, "created_by": role.value, "created_at": now()})
    fw.log("scan_queued", "pipeline", "ok", f"Scan queued: {fw.bbox_area_km2(bbox):.1f} km², {scan_start} to {end}", run_id=rid, actor=role.value)
    job = jobs.submit("forest_scan", lambda: fw.scan(rid, bbox, scan_start, end, payload.use_radar, role.value, payload.polygon))
    return {"run_id": rid, "job_id": job, "status": "queued", "scan_start": scan_start.isoformat(), "scan_end": end.isoformat(), "area_km2": round(fw.polygon_area_km2(payload.polygon) if payload.polygon else fw.bbox_area_km2(bbox), 1), "resolution_m": fw.resolution_for(fw.polygon_area_km2(payload.polygon) if payload.polygon else fw.bbox_area_km2(bbox))}


def _run_out(r: dict[str, Any], timeline: bool = False) -> dict[str, Any]:
    res = r.get("result") or {}
    out = {"id": r["id"], "status": r["status"], "progress": r["progress"], "bbox": r["bbox"], "window_start": r["window_start"], "window_end": r["window_end"], "created_at": r["created_at"], "finished_at": r["finished_at"], "area_km2": round(fw.polygon_area_km2(mapping(fw.run_area(r))), 1), "polygon": (r.get("params") or {}).get("polygon"), "steps": res.get("steps", [])[-12:], "summary": res.get("summary"), "error": res.get("error")}
    if timeline:
        out["timeline"] = res.get("timeline", [])
    return out


@api.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, role: Role = Depends(require_roles(*SCAN))):
    """Stop a check that is queued or running. Work already cached is kept; nothing is added to the map."""
    r = get_one("fc_runs", "id", run_id)
    if not r:
        raise HTTPException(status_code=404, detail="Scan not found")
    if r["status"] not in ("QUEUED", "RUNNING"):
        return {"status": r["status"], "cancelled": False}
    from app.db.database import update
    update("fc_runs", "id", run_id, {"status": "CANCELLED", "result": {"steps": [], "error": "Cancelled by " + role.value}, "finished_at": now()})
    fw.log("scan_cancelled", "pipeline", "ok", "Check cancelled", run_id=run_id, actor=role.value)
    return {"status": "CANCELLED", "cancelled": True}


@api.get("/runs")
def list_runs(limit: int = Query(20, ge=1, le=100), role: Role = Depends(require_roles(*READ))):
    with connection() as conn:
        rows = [decode_row(dict(r)) for r in conn.execute("SELECT * FROM fc_runs ORDER BY created_at DESC LIMIT ?", (limit,))]
    return [_run_out(r) for r in rows]


@api.get("/runs/{run_id}")
def get_run(run_id: str, timeline: bool = False, role: Role = Depends(require_roles(*READ))):
    r = get_one("fc_runs", "id", run_id)
    if not r:
        raise HTTPException(status_code=404, detail="Scan not found")
    return _run_out(r, timeline)


@api.get("/runs/{run_id}/grid")
def run_grid(run_id: str, cell_m: float | None = Query(default=None, ge=100, le=5000), role: Role = Depends(require_roles(*READ))):
    """The scanned area cut into labelled cells (A1, A2, B1…) with tree cover and loss for 7 / 30 / 90 days in each."""
    return _wf(fw.grid, run_id, cell_m)


@api.get("/timeline")
def timeline(limit: int = Query(12, ge=1, le=60), role: Role = Depends(require_roles(*READ))):
    """Tree cover and loss snapshots (90 / 30 / 7 days ago, today) of every finished scan, newest first."""
    with connection() as conn:
        rows = [decode_row(dict(r)) for r in conn.execute("SELECT * FROM fc_runs WHERE status = 'COMPLETE' ORDER BY created_at DESC LIMIT ?", (limit,))]
    seen, out = set(), []
    for r in rows:   # a repeated scan of the same area replaces the older picture of it
        key = tuple(r["bbox"])
        if key in seen:
            continue
        seen.add(key)
        out.append({"run_id": r["id"], "bbox": r["bbox"], "window_end": r["window_end"], "summary": (r["result"] or {}).get("summary"), "snapshots": (r["result"] or {}).get("timeline", [])})
    return out


@api.get("/records")
def list_records(days: int = Query(90, ge=1, le=365), run_id: str | None = None, bbox: str | None = None, status: str | None = None, min_ha: float = 0.0, include_false: bool = False, role: Role = Depends(require_roles(*READ))):
    box = [float(x) for x in bbox.split(",")] if bbox else None
    start = datetime.now(timezone.utc).date() - timedelta(days=days)
    within = None
    if run_id:
        run = get_one("fc_runs", "id", run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Scan not found")
        within = fw.run_area(run)
    return fw.query(start=start, bbox=box, status=status, min_ha=min_ha, include_false=include_false, within=within)


@api.get("/records/{rid}")
def get_record(rid: str, role: Role = Depends(require_roles(*READ))):
    _rec(rid)
    try:
        fw.refresh_calls(rid)
    except Exception:  # noqa: BLE001
        pass
    return fw.public(_rec(rid), True)


@api.get("/records/{rid}/report.pdf")
def record_report(rid: str, role: Role = Depends(require_roles(*READ))):
    """One-page-style PDF for a tree-loss spot: summary, map, permit, who responds and how to get there, and what has been done."""
    from app.services import geoinfo, reports

    rec = _rec(rid)
    full = fw.public(rec, True)
    lat, lon = rec["lat"], rec["lon"]
    pl = geoinfo.place(lat, lon)
    a, r, v = rec.get("authority") or {}, rec.get("route") or {}, rec.get("verification") or {}
    o, po = a.get("office"), a.get("police")
    km = lambda x: f"{x['distance_km']} km, about {round(x['duration_min'])} min" + (f" (incl. {x['walk_km']} km on foot)" if x.get("walk_km") else "")  # noqa: E731
    blocks: list[tuple[str, Any]] = [
        ("Where and when", {"place": pl["name"] if pl else None, "coordinates": f"{lat:.5f}, {lon:.5f}", "estimated date": f"{rec['est_date']} (±{round(rec.get('date_uncertainty_days') or 0)} days)", "age": f"{full.get('age_days')} days", "seen in scans": f"{rec.get('seen_count')}", "evidence": rec.get("evidence")}),
        ("Permit check", {"result": v.get("meaning"), "next step": v.get("next_step"), **{f"permit {i + 1}": f"{p.get('number') or 'no number'} · {p.get('authority') or 'authority not given'} · {p.get('status') or ''} · {p.get('valid_from') or '?'} to {p.get('valid_to') or '?'}" for i, p in enumerate(v.get("permits") or [])}}),
    ]
    if o:
        blocks.append((f"Main responder: {o.get('kind_label', 'office')}", {"name": o["name"], "distance": f"{o.get('straight_line_km')} km in a straight line", "by road": km(r) if r.get("status") == "OK" else "no road route found", "directions": r.get("navigation_url"), "walking directions": r.get("walk_url"), "phone": None, "source": f"{o.get('source')} (unverified)"}))
    if po and not po.get("same_as_main"):
        blocks.append(("Nearest police station", {"name": po["name"], "by road": km(po), "directions": po.get("navigation_url")}))
    others = a.get("others") or []
    if others:
        blocks.append(("Also nearby", [f"{x.get('kind_label', '')}: {x['name']} · " + (km(x) if x.get("distance_km") is not None else f"{x.get('straight_line_km')} km straight") for x in others]))
    blocks.append(("Status", {"decision": rec["status"].replace("_", " ").title(), "notes": rec.get("notes")}))
    hist = [f"{e['ts'][:16].replace('T', ' ')}  {e['message']}" for e in fw.events(record_id=rid, limit=60) if e.get("status") != "info"][:40]
    blocks.append(("What has been done", hist or ["Nothing yet."]))
    imgs: list[tuple[bytes, str]] = []
    try:   # the same live pictures the Telegram alert carries: the lost area, and the routes from the responders
        imgs = [(jpg, cap) for _, jpg, cap in fw.evidence_images(rec)]
    except Exception:  # noqa: BLE001 - a picture that cannot be made is left out
        pass
    if not imgs and (m := reports._static_map(lat, lon)):
        imgs = [(m, f"Satellite view of {lat:.5f}, {lon:.5f}")]
    pdf = reports.generic_pdf(f"Tree-loss spot {rec['code']}", (pl["name"] if pl else "Forest area") + " · satellite-detected candidate change", blocks,
                              summary=[("Area lost", f"{rec['area_ha']} ha"), ("Size", rec["severity"].title()), ("Age", f"{full.get('age_days')} d"), ("Status", rec["status"].replace("_", " ").title())],
                              links=[("Open in Google Maps", f"https://www.google.com/maps?q={lat},{lon}")] + ([("Directions: " + o["name"][:28], r["navigation_url"])] if o and r.get("navigation_url") else []) + ([("Directions: " + po["name"][:28], po["navigation_url"])] if po and po.get("navigation_url") else []) + [("Satellite view", f"https://www.google.com/maps/@{lat},{lon},400m/data=!3m1!1e3")], images=imgs)
    audit(role.value, "forest_report_downloaded", "fc_record", rid, {})
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{rec["code"]}-report.pdf"'})


@api.patch("/records/{rid}")
def review_record(rid: str, payload: ReviewIn, role: Role = Depends(require_roles(*REVIEW))):
    _rec(rid)
    return _wf(fw.review, rid, role.value, payload.status, payload.category, payload.notes)


@api.post("/records/{rid}/verify")
def verify_record(rid: str, role: Role = Depends(require_roles(*REVIEW))):
    _rec(rid)
    return fw.verify(rid, role.value)


@api.post("/records/{rid}/authority")
def assign_authority(rid: str, payload: AuthorityIn, role: Role = Depends(require_roles(*REVIEW))):
    _rec(rid)
    return _wf(fw.assign_authority, rid, router_service, role.value, payload.station_id)


@api.post("/records/{rid}/notify")
def notify_record(rid: str, payload: NotifyIn, role: Role = Depends(require_roles(*SEND))):
    _rec(rid)
    return _wf(fw.notify, rid, payload.channels, payload.mode, payload.confirm_real, role.value, payload.resend)


class PermitIn(M):
    authority: str = Field(min_length=2, max_length=160, description="who issued it")
    permit_number: str = Field(min_length=1, max_length=120)
    project_name: str | None = Field(default=None, max_length=200)
    status: Literal["issued", "approved", "granted", "conditional_approval", "pending", "rejected", "revoked", "expired"] = "issued"
    valid_from: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    valid_to: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    source_url: str | None = Field(default=None, max_length=500, pattern=r"^https?://")
    note: str | None = Field(default=None, max_length=500)


@api.post("/records/{rid}/permits")
def add_permit(rid: str, payload: PermitIn, role: Role = Depends(require_roles(*REVIEW))):
    _rec(rid)
    return _wf(fw.add_permit, rid, payload.model_dump(), role.value)


@api.delete("/records/{rid}/permits/{pid}")
def remove_permit(rid: str, pid: str, role: Role = Depends(require_roles(*REVIEW))):
    _rec(rid)
    return _wf(fw.remove_permit, rid, pid, role.value)


@api.delete("/records/{rid}")
def delete_record(rid: str, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER))):
    _rec(rid)
    _wf(fw.delete_record, rid, role.value)
    return {"deleted": rid}


@api.get("/records/{rid}/call-script")
def call_script(rid: str, role: Role = Depends(require_roles(*READ))):
    """The exact words a phone call would speak, so the operator can read or hear them first."""
    return {"script": fw.spoken(_rec(rid))}


@api.get("/events")
def events(record_id: str | None = None, run_id: str | None = None, since_id: int = 0, limit: int = Query(100, ge=1, le=500), role: Role = Depends(require_roles(*READ))):
    return fw.events(record_id, run_id, since_id, limit)


@api.get("/coverage")
def coverage(role: Role = Depends(require_roles(*READ))):
    return fw.coverage()


@api.get("/export.geojson")
def export(days: int = Query(90, ge=1, le=365), role: Role = Depends(require_roles(*READ))):
    rows = fw.query(start=datetime.now(timezone.utc).date() - timedelta(days=days))
    audit(role.value, "forest_records_exported", "fc_record", None, {"n": len(rows)})
    fc = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": r["geometry"], "properties": {k: v for k, v in r.items() if k != "geometry"}} for r in rows], "notice": "Satellite-derived candidate tree loss; verify before acting. Not legal findings."}
    return Response(json.dumps(fc), media_type="application/geo+json", headers={"Content-Disposition": 'attachment; filename="rakshak-forest-change.geojson"'})


@api.get("/cases")
def case_list(role: Role = Depends(require_roles(*READ))):
    from app.services import casestudies
    return casestudies.listing() if settings.enable_examples else []


@api.get("/cases/{case_id}")
def case_detail(case_id: str, role: Role = Depends(require_roles(*READ))):
    from app.services import casestudies
    c = casestudies.build(case_id) if settings.enable_examples else None
    if not c:
        raise HTTPException(status_code=404, detail="Case not found")
    return c
