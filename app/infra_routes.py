"""Infrastructure-change API: find new roads, buildings and other construction from the last 365 days of satellite images in a chosen area,
see each one with its type, impact and before/after pictures, mark false detections, and download reports."""
import io
import json
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import get_settings
from app.core.security import Role, require_roles
from app.db.database import audit, connection, decode_row, get_one, insert, now, update
from app.services import infra, infrareport
from app.services.jobs import JobManager

settings = get_settings()
api = APIRouter(prefix=settings.api_prefix + "/infra")
jobs = JobManager(settings.job_workers)
READ = (Role.ADMIN, Role.REVIEWER, Role.OFFICER, Role.ANALYST, Role.READ_ONLY)
SCAN = (Role.ADMIN, Role.REVIEWER, Role.ANALYST, Role.OFFICER)


class M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunIn(M):
    preset: str | None = Field(default=None, description="a ready-made Jaipur area, see /presets")
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4, description="[west, south, east, north]")
    polygon: dict | None = Field(default=None, description="optional GeoJSON Polygon drawn by the user; results are clipped to it")
    name: str | None = Field(default=None, max_length=120)


class ChangeIn(M):
    status: Literal["OPEN", "NOT_A_CHANGE"]
    note: str | None = Field(default=None, max_length=500)


def _run(rid: str) -> dict[str, Any]:
    r = get_one("infra_runs", "id", rid)
    if not r:
        raise HTTPException(status_code=404, detail="Check not found")
    return r


def _change(cid: str) -> dict[str, Any]:
    c = get_one("infra_changes", "id", cid)
    if not c:
        raise HTTPException(status_code=404, detail="Change not found")
    return c


def _run_out(r: dict[str, Any]) -> dict[str, Any]:
    res = r.get("result") or {}
    return {"id": r["id"], "name": r["name"], "status": r["status"], "progress": r["progress"], "bbox": r["bbox"], "polygon": r.get("geometry"), "created_at": r["created_at"], "finished_at": r["finished_at"],
            "steps": res.get("steps", []), "summary": res.get("summary"), "error": res.get("error"), "preset": (r.get("params") or {}).get("preset")}


@api.get("/status")
def status(role: Role = Depends(require_roles(*READ))):
    d = infra.dates()
    return {"history_days": infra.HISTORY_DAYS, "max_area_km2": infra.MAX_AREA_KM2, "sharp_area_km2": infra.HI_RES_KM2, "min_area_km2": infra.MIN_AREA_KM2, "before": str(d["before"]), "after": str(d["after"]),
            "classes": {k: {"label": v["label"], "color": v["color"], "hard": v["hard"]} for k, v in infra.CLASSES.items()}, "presets": {k: {"name": v["name"], "bbox": v["bbox"], "area_km2": round(infra.area_km2(v["bbox"]), 1)} for k, v in infra.PRESETS.items()}}


@api.post("/runs")
def start(payload: RunIn, role: Role = Depends(require_roles(*SCAN))):
    if payload.preset:
        pre = infra.PRESETS.get(payload.preset)
        if not pre:
            raise HTTPException(status_code=422, detail="Unknown ready-made area")
        bbox, name, polygon = pre["bbox"], pre["name"], None
    elif payload.bbox:
        bbox, name, polygon = payload.bbox, payload.name or "Area I chose", payload.polygon
    else:
        raise HTTPException(status_code=422, detail="Choose a ready-made area or draw one")
    try:
        bbox, km2, res = infra.validate(bbox, polygon)
    except infra.InfraError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    rid = str(uuid.uuid4())
    insert("infra_runs", {"id": rid, "name": name, "status": "QUEUED", "progress": 0.0, "bbox": bbox, "geometry": polygon, "params": {"preset": payload.preset, "area_km2": round(km2, 1), "resolution_m": res}, "result": None, "created_by": role.value, "created_at": now(), "finished_at": None})
    audit(role.value, "infra_scan_queued", "infra_run", rid, {"area_km2": round(km2, 1)})
    job = jobs.submit("infra_scan", lambda: infra.run(rid, bbox, polygon, role.value))
    return {"run_id": rid, "job_id": job, "area_km2": round(km2, 1), "resolution_m": res}


@api.get("/runs")
def list_runs(limit: int = Query(30, ge=1, le=100), role: Role = Depends(require_roles(*READ))):
    with connection() as conn:
        rows = [decode_row(dict(r)) for r in conn.execute("SELECT * FROM infra_runs ORDER BY created_at DESC LIMIT ?", (limit,))]
    return [_run_out(r) for r in rows]


@api.get("/runs/{rid}")
def get_run(rid: str, role: Role = Depends(require_roles(*READ))):
    return _run_out(_run(rid))


@api.post("/runs/{rid}/cancel")
def cancel(rid: str, role: Role = Depends(require_roles(*SCAN))):
    r = _run(rid)
    if r["status"] not in ("QUEUED", "RUNNING"):
        return {"status": r["status"], "cancelled": False}
    update("infra_runs", "id", rid, {"status": "CANCELLED", "result": {"steps": [], "error": "Cancelled by " + role.value}, "finished_at": now()})
    return {"status": "CANCELLED", "cancelled": True}


@api.delete("/runs/{rid}")
def delete_run(rid: str, role: Role = Depends(require_roles(*SCAN))):
    """Remove a check and everything found in it."""
    _run(rid)
    with connection() as conn:
        conn.execute("DELETE FROM infra_changes WHERE run_id = ?", (rid,))
        conn.execute("DELETE FROM infra_runs WHERE id = ?", (rid,))
    f = infra.DATA / f"{rid}.npz"
    if f.is_file():
        f.unlink()
    audit(role.value, "infra_run_deleted", "infra_run", rid, {})
    return {"deleted": rid}


@api.get("/runs/{rid}/changes")
def list_changes(rid: str, include_hidden: bool = False, role: Role = Depends(require_roles(*READ))):
    _run(rid)
    rows = infra.changes_of(rid)
    if not include_hidden:
        rows = [c for c in rows if c["status"] != "NOT_A_CHANGE"]
    return [infra.public_change(c, full=False) for c in rows]


@api.get("/runs/{rid}/export.geojson")
def export(rid: str, role: Role = Depends(require_roles(*READ))):
    _run(rid)
    feats = [{"type": "Feature", "geometry": c["geometry"], "properties": {"code": c["code"], "type": infra.CLASSES[c["cls"]]["label"], "area_ha": c["area_ha"], "classified_from": c["cls_source"], "name": c["name"], "vegetation_lost_ha": (c["impact"] or {}).get("vegetation_lost_ha")}}
             for c in infra.changes_of(rid) if c["status"] != "NOT_A_CHANGE"]
    audit(role.value, "infra_exported", "infra_run", rid, {"n": len(feats)})
    return Response(json.dumps({"type": "FeatureCollection", "features": feats}), media_type="application/geo+json", headers={"Content-Disposition": f'attachment; filename="infrastructure-change-{rid[:8]}.geojson"'})


@api.get("/changes/{cid}")
def get_change(cid: str, role: Role = Depends(require_roles(*READ))):
    return infra.public_change(_change(cid))


@api.patch("/changes/{cid}")
def review(cid: str, payload: ChangeIn, role: Role = Depends(require_roles(*SCAN))):
    """Mark a detection as not a real change (a field, a shadow) or put it back. Nothing is deleted."""
    c = _change(cid)
    update("infra_changes", "id", cid, {"status": payload.status, "note": payload.note})
    audit(role.value, "infra_change_reviewed", "infra_change", cid, {"status": payload.status})
    return infra.public_change(_change(cid))


@api.get("/changes/{cid}/image/{kind}.jpg")
def image(cid: str, kind: Literal["before", "after", "compare"], role: Role = Depends(require_roles(*READ))):
    c = _change(cid)
    if kind == "compare":
        data = infra.compare_image(c, c["run_id"], get_one("infra_runs", "id", c["run_id"]))
    else:
        ims = infra.change_images(c, c["run_id"])
        data = None
        if ims:
            buf = io.BytesIO()
            ims[kind].save(buf, "JPEG", quality=88)
            data = buf.getvalue()
    if not data:
        raise HTTPException(status_code=404, detail="The pictures for this check are no longer stored")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


@api.get("/changes/{cid}/report.pdf")
def change_report(cid: str, role: Role = Depends(require_roles(*READ))):
    c = _change(cid)
    run = get_one("infra_runs", "id", c["run_id"])
    pdf = infrareport.change_pdf(c, run)
    audit(role.value, "infra_change_report", "infra_change", cid, {})
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{c["code"]}-report.pdf"'})


@api.get("/runs/{rid}/report.pdf")
def run_report(rid: str, role: Role = Depends(require_roles(*READ))):
    run = _run(rid)
    pdf = infrareport.run_pdf(run, infra.changes_of(rid))
    audit(role.value, "infra_run_report", "infra_run", rid, {})
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="infrastructure-change-{rid[:8]}.pdf"'})
