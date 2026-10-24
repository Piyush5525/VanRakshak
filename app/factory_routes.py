"""Factory impact check: place a factory (by hand or from a PDF) and see what it does to the forest and its surroundings."""
import hashlib
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import get_settings
from app.core.security import Role, require_roles
from app.db.database import audit, connection, decode_row, get_one, insert, now, update
from app.services import boundary, factorydoc, factoryimpact
from app.services.jobs import JobManager
from app.services.reports import generic_pdf

settings = get_settings()
api = APIRouter(prefix=settings.api_prefix + "/factory")
jobs = JobManager(settings.job_workers)
READ = (Role.ADMIN, Role.REVIEWER, Role.OFFICER, Role.ANALYST, Role.READ_ONLY)
WRITE = (Role.ADMIN, Role.REVIEWER, Role.OFFICER, Role.ANALYST)


class M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Emission(M):
    pollutant: str = Field(min_length=1, max_length=20)
    value: float
    unit: str = Field(min_length=1, max_length=20)
    source: str = Field(min_length=2, max_length=200)


class CheckIn(M):
    name: str = Field(min_length=1, max_length=120)
    kind: str = "other"
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    footprint_ha: float | None = Field(default=None, gt=0, le=2000)
    stack_height_m: float | None = Field(default=None, ge=0, le=500)
    water_kld: float | None = Field(default=None, ge=0, le=1e7)
    emissions: list[Emission] = []
    clearing_days: int | None = Field(default=None, ge=1, le=365)
    operation_start_day: int | None = Field(default=None, ge=0, le=365)
    source: str = Field(default="form", pattern="^(form|pdf)$")
    document: dict[str, Any] | None = None


@api.get("/options")
def options(role: Role = Depends(require_roles(*READ))):
    return {"kinds": factoryimpact.kinds(), "horizons": list(factoryimpact.HORIZONS), "max_pdf_mb": factorydoc.MAX_BYTES // 1024 // 1024}


@api.post("/extract")
async def extract(file: UploadFile = File(...), role: Role = Depends(require_roles(*WRITE))):
    """Read a PDF and return the facts found in it, each with the page and sentence it came from. Only a fingerprint is kept."""
    data = await file.read(factorydoc.MAX_BYTES + 1)
    try:
        pages, warnings = factorydoc.read_pdf(data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    out = factorydoc.extract(pages)
    audit(role.value, "factory_pdf_read", "factory_document", None, {"filename": (file.filename or "")[:80], "pages": out["pages_read"], "fields": sorted(out["fields"])})
    return {**out, "filename": file.filename, "sha256": hashlib.sha256(data).hexdigest(), "warnings": warnings}


def _run(cid: str) -> dict[str, Any]:
    row = get_one("factory_checks", "id", cid)
    try:
        res = factoryimpact.assess(row["inputs"])
    except Exception as exc:  # noqa: BLE001
        update("factory_checks", "id", cid, {"status": "FAILED", "result": {"status": "failed", "reason": str(exc)[:300]}})
        raise
    update("factory_checks", "id", cid, {"status": "COMPLETE" if res.get("status") == "ok" else "FAILED", "result": res})
    return {"id": cid, "status": res.get("status")}


@api.post("/checks")
def create(payload: CheckIn, role: Role = Depends(require_roles(*WRITE))):
    if not boundary.contains(payload.lat, payload.lon):
        raise HTTPException(status_code=422, detail="That point is outside India; this prototype checks places in India only")
    cid = str(uuid.uuid4())
    insert("factory_checks", {"id": cid, "name": payload.name, "kind": payload.kind if payload.kind in factoryimpact.PROFILES else "other", "lat": payload.lat, "lon": payload.lon, "source": payload.source, "document": payload.document, "inputs": payload.model_dump(), "result": None, "status": "RUNNING", "created_by": role.value, "created_at": now()})
    audit(role.value, "factory_check_started", "factory_check", cid, {"kind": payload.kind, "source": payload.source})
    return {"id": cid, "job_id": jobs.submit("factory_check", lambda: _run(cid))}


def _out(r: dict[str, Any], full: bool = False) -> dict[str, Any]:
    d = {"id": r["id"], "name": r["name"], "kind": r["kind"], "kind_label": factoryimpact.PROFILES.get(r["kind"], {}).get("label"), "lat": r["lat"], "lon": r["lon"], "source": r["source"], "status": r["status"], "created_at": r["created_at"]}
    if full:
        d.update(inputs=r["inputs"], result=r["result"], document=r["document"])
    else:
        d["levels"] = {e["area"]: e["level"] for e in ((r["result"] or {}).get("effects", []))}
    return d


@api.get("/checks")
def listing(role: Role = Depends(require_roles(*READ))):
    with connection() as conn:
        rows = [decode_row(dict(x)) for x in conn.execute("SELECT * FROM factory_checks ORDER BY created_at DESC LIMIT 50")]
    return [_out(r) for r in rows]


@api.get("/checks/{cid}")
def detail(cid: str, role: Role = Depends(require_roles(*READ))):
    r = get_one("factory_checks", "id", cid)
    if not r:
        raise HTTPException(status_code=404, detail="Check not found")
    return _out(r, True)


@api.delete("/checks/{cid}")
def remove(cid: str, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER))):
    if not get_one("factory_checks", "id", cid):
        raise HTTPException(status_code=404, detail="Check not found")
    with connection() as conn:
        conn.execute("DELETE FROM factory_checks WHERE id = ?", (cid,))
    audit(role.value, "factory_check_deleted", "factory_check", cid, {})
    return {"deleted": cid}


@api.get("/checks/{cid}/report.pdf")
def report(cid: str, role: Role = Depends(require_roles(*READ))):
    r = get_one("factory_checks", "id", cid)
    res = (r or {}).get("result") or {}
    if not r or res.get("status") != "ok":
        raise HTTPException(status_code=404, detail="No finished check to report on")
    i, s = res["inputs"], res["site"]
    blocks: list[tuple[str, Any]] = [
        ("Factory", {"name": i["name"], "type": i["kind_label"], "location": f"{s['lat']:.5f}, {s['lon']:.5f}", "site area (ha)": f"{i['footprint_ha']}" + (" (assumed for this type)" if i["footprint_assumed"] else ""), "stack height (m)": i.get("stack_height_m") or "not given", "water use (KLD)": i.get("water_kld") or "not given", "details from": "PDF document" if r["source"] == "pdf" else "entered by hand"}),
        ("What is there today", {"forest inside the site (ha)": s["forest_in_footprint_ha"], "forest within 300 m (edge zone, ha)": s["edge_zone_forest_ha"], "forest within 3 km (ha)": s["forest_within_3km_ha"], "tree cover within 500 m / 1 km / 3 km (%)": " / ".join(str(s["tree_share_pct"].get(str(k), s["tree_share_pct"].get(k))) for k in (500, 1000, 3000))}),
        ("Effects on the surroundings", {f"{e['area']} ({e['level']})": f"{e['title']}. {e['detail']}" for e in res["effects"]}),
        ("Scenario: 7 / 30 / 90 days after work starts", {f"day {h['day']}": f"{h['phase']}: {h['cleared_ha']} ha cleared, {h['edge_affected_ha']} ha edge-affected, {h['carbon_t_co2']:,} t CO2, {h['smoke_forest_ha']} ha of forest in the smoke zone, water pathway {h['water_pathway']}" for h in res["projection"]["horizons"]}),
        ("Scenario assumptions", res["projection"]["assumptions"]["note"]),
        ("Data sources", res["sources"]),
        ("Limits", res["notice"]),
    ]
    audit(role.value, "factory_report_downloaded", "factory_check", cid, {})
    from app.services.reports import _static_map

    pdf = generic_pdf(f"Factory impact screening: {i['name']}", f"{i['kind_label']} · screening estimate from open data", blocks,
                      summary=[("Forest on site", f"{s['forest_in_footprint_ha']} ha"), ("Within 300 m", f"{s['edge_zone_forest_ha']} ha"), ("Within 3 km", f"{s['forest_within_3km_ha']} ha" if 'forest_within_3km_ha' in s else "–")],
                      links=[("Open in Google Maps", f"https://www.google.com/maps?q={s['lat']},{s['lon']}")], image=_static_map(s["lat"], s["lon"], 14), caption=f"Satellite view of the site, {s['lat']:.5f}, {s['lon']:.5f}")
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="factory-check-{cid[:8]}.pdf"'})
