"""RAKSHAK module routes: environment, navigation/hazards, AR scenes, projections, industrial, vision, evidence, previews."""
import hashlib
import io
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import get_settings
import json as _json  # noqa: F401
from app.core.geometry import to_shape, validate_geojson
from app.core.security import Role, require_roles
from app.db.database import audit, connection, decode_row, get_one, insert, list_rows, now, update
from app.ops_routes import notifier, router_service
from app.services import alert_eval, alerts as alert_engine, datasources, environment, forestscene, industrial, navigation, previews, projection
from app.services.firevision import FireDetector, FireModelUnavailable, TemporalFilter, correlate
from app.services.jobs import JobManager
from app.services.reports import generic_pdf, to_csv
from app.services.routing import nearest_station
from app.services.weather import WeatherUnavailable, rainfall_summary

settings = get_settings()
api = APIRouter(prefix=settings.api_prefix)
jobs = JobManager(settings.job_workers)
detector = FireDetector(settings)
temporal = TemporalFilter()
READ = (Role.ADMIN, Role.REVIEWER, Role.OFFICER, Role.ANALYST, Role.READ_ONLY)
ANALYSIS = (Role.ADMIN, Role.REVIEWER, Role.ANALYST, Role.OFFICER)
FIELD = (Role.ADMIN, Role.OFFICER)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _404(what: str):
    raise HTTPException(status_code=404, detail=f"{what} not found")


def _job(job_id: str):
    return {"job_id": job_id, "status": "queued"}


# ===================================================================== dashboard / data sources
@api.get("/dashboard/summary")
def dashboard_summary(role: Role = Depends(require_roles(*READ))):
    """Counts and totals computed from stored records only."""
    from app.core.geometry import geodesic_area_hectares

    regions = list_rows("regions", limit=1000)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    changes = [c for c in list_rows("changes", limit=2000) if c["disturbance_type"] == "CANDIDATE_FOREST_LOSS"]
    incidents = list_rows("incidents", limit=2000)
    fires = list_rows("wildfire_observations", limit=2000)
    assessments = list_rows("authorization_assessments", limit=2000)
    with connection() as conn:
        notif = [decode_row(dict(r)) for r in conn.execute("SELECT id, channel, status, created_at FROM notifications ORDER BY created_at DESC LIMIT 5")]
    models = list_rows("model_registry", limit=100)
    return {
        "monitored_regions": len(regions), "monitored_area_hectares": round(sum(geodesic_area_hectares(r["geometry"]) for r in regions), 1),
        "candidate_loss_hectares_last_365d": round(sum(c["area_hectares"] for c in changes if c["comparison_date"] >= cutoff), 2), "candidate_loss_hectares_total": round(sum(c["area_hectares"] for c in changes), 2),
        "pending_incidents": len([i for i in incidents if i["status"] in ("DETECTED", "NEEDS_REVIEW", "PROCESSING")]), "open_incidents": len([i for i in incidents if i["status"] not in ("CLOSED", "DISMISSED")]),
        "authorization_checks_needing_review": len([a for a in assessments if a["status"] != "AUTHORIZATION_DOCUMENTED"]),
        "wildfire_alerts_needing_review": len([f for f in fires if f["status"] == "NEEDS_REVIEW"]), "wildfire_records_total": len(fires),
        "environmental_assessments": len(list_rows("environment_assessments", limit=1000)), "industrial_sites": len(list_rows("industrial_sites", limit=1000)),
        "recent_notifications": notif, "forecast_model_usable": any(m["status"] == "USABLE" for m in models),
        "elevated_risk_regions": [], "note": "Elevated-risk regions require a usable spatial risk model; none is registered unless this is true: forecast_model_usable",
    }


@api.get("/data-sources")
def data_sources(force: bool = False, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER, Role.ANALYST, Role.OFFICER))):
    return datasources.check(settings, force=force and role == Role.ADMIN)


@api.get("/reports")
def list_reports(incident_id: str | None = None, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER, Role.OFFICER, Role.ANALYST))):
    rows = list_rows("reports", "WHERE incident_id = ?" if incident_id else "", (incident_id,) if incident_id else (), limit=200)
    titles = {i["id"]: i["title"] for i in list_rows("incidents", limit=1000)}
    return [{"id": r["id"], "incident_id": r["incident_id"], "incident_title": titles.get(r["incident_id"]), "format": r["format"], "created_at": r["created_at"]} for r in rows]


# ===================================================================== satellite previews
def _obs(observation_id: str) -> dict[str, Any]:
    o = get_one("observations", "id", observation_id)
    if not o or o["status"] != "OK" or not Path(o["provenance"].get("file") or "x").is_file():
        _404("Observation file")
    return o


@api.get("/observations/{observation_id}/preview-meta")
def preview_meta(observation_id: str, role: Role = Depends(require_roles(*READ))):
    o = _obs(observation_id)
    _, bounds = previews.render(o["provenance"]["file"], o["provenance"]["sensor"], max_px=64)
    p = o["provenance"]
    return {"observation_id": observation_id, "sensor": p["sensor"], "bounds": bounds, "acquired_from": o["acquisition_start"], "acquired_to": o["acquisition_end"], "scenes": o["source_scene_ids"], "valid_pixel_fraction": o["quality"].get("valid_pixel_fraction"), "processing": p.get("processing"), "warnings": p.get("warnings"), "legend": "Sentinel-2: NBR, green = vegetated, brown = bare/dry, grey = no clear observation. Sentinel-1: VH backscatter (dB), grey scale. Forest mask: green = tree cover.", "image_url": f"{settings.api_prefix}/observations/{observation_id}/preview.png"}


@api.get("/observations/{observation_id}/preview.png")
def preview_png(observation_id: str, role: Role = Depends(require_roles(*READ))):
    o = _obs(observation_id)
    sensor = o["provenance"]["sensor"]
    png, _ = previews.render(o["provenance"]["file"], "forest" if sensor == "esa-worldcover" else sensor)
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@api.get("/regions/{region_id}/observations-ok")
def region_observations(region_id: str, role: Role = Depends(require_roles(*READ))):
    rows = list_rows("observations", "WHERE region_id = ? AND status = 'OK'", (region_id,), limit=200)
    return [{"id": o["id"], "sensor": o["provenance"].get("sensor"), "acquired_from": o["acquisition_start"], "acquired_to": o["acquisition_end"], "valid_pixel_fraction": o["quality"].get("valid_pixel_fraction"), "scenes": len(o["source_scene_ids"]), "processed_at": o["provenance"].get("processed_at")} for o in rows]


# ===================================================================== environment
class EnvRequest(Model):
    region_id: str
    change_id: str | None = None


@api.post("/environment/assessments")
def create_environment_assessment(payload: EnvRequest, role: Role = Depends(require_roles(*ANALYSIS))):
    if not get_one("regions", "id", payload.region_id):
        _404("Monitoring region")
    return _job(jobs.submit("environment_assessment", lambda: environment.assess(payload.region_id, payload.change_id, role.value)))


@api.get("/environment/assessments")
def list_environment_assessments(region_id: str | None = None, role: Role = Depends(require_roles(*READ))):
    rows = list_rows("environment_assessments", "WHERE region_id = ?" if region_id else "", (region_id,) if region_id else (), limit=100)
    return [{"id": r["id"], "region_id": r["region_id"], "status": r["status"], "created_at": r["created_at"], "region_name": r["result"].get("region_name")} for r in rows]


@api.get("/environment/assessments/{assessment_id}")
def get_environment_assessment(assessment_id: str, role: Role = Depends(require_roles(*READ))):
    r = get_one("environment_assessments", "id", assessment_id)
    return r["result"] if r else _404("Assessment")


def _env_blocks(a: dict[str, Any]) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = [("Study area", {"region": a["region_name"], "area_hectares": a["region_area_hectares"], "generated_at": a["generated_at"]})]
    labels = {"observed": "measured", "computed": "computed", "screening": "screening"}
    for name, sec in a["sections"].items():
        cls = sec.get("evidence_class", "")
        flat = {k: v for k, v in sec.items() if k not in ("evidence_class", "attribution") and not isinstance(v, (dict, list))}
        nested = {k: v for k, v in sec.items() if isinstance(v, (dict, list)) and k not in ("calibration",)}
        out.append((f"{name.replace('_', ' ').title()}  [{cls}]", {**flat, **{k: json.dumps(v)[:500] for k, v in nested.items()}}))
    out.append(("Recommended follow-up", a["recommended_follow_up"]))
    out.append(("Method and limitations", a["method_notes"] + ["Candidate loss is a rule-based detection from Sentinel-1/Sentinel-2 and is unverified.", "Sections tagged not_available were not estimated."]))
    return out


@api.get("/environment/assessments/{assessment_id}/report.pdf")
def environment_pdf(assessment_id: str, role: Role = Depends(require_roles(*READ))):
    a = get_environment_assessment(assessment_id, role)
    audit(role.value, "environment_report_exported", "environment_assessment", assessment_id, {"format": "pdf"})
    return Response(generic_pdf("RAKSHAK environmental impact report", f"Region: {a['region_name']}", _env_blocks(a)), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="rakshak-environment-{assessment_id[:8]}.pdf"'})


@api.get("/environment/assessments/{assessment_id}/report.csv")
def environment_csv(assessment_id: str, role: Role = Depends(require_roles(*READ))):
    a = get_environment_assessment(assessment_id, role)
    audit(role.value, "environment_report_exported", "environment_assessment", assessment_id, {"format": "csv"})
    rows = []
    for name, sec in a["sections"].items():
        for k, v in sec.items():
            rows.append({"section": name, "evidence_class": sec.get("evidence_class"), "field": k, "value": json.dumps(v) if isinstance(v, (dict, list)) else v})
    return Response(to_csv(rows, ["section", "evidence_class", "field", "value"]), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="rakshak-environment-{assessment_id[:8]}.csv"'})


# ===================================================================== projections
class ProjectionRequest(Model):
    region_id: str
    scenario_multipliers: dict[str, float] = Field(default_factory=dict)

    @field_validator("scenario_multipliers")
    @classmethod
    def sane(cls, v):
        if any(not (0 <= x <= 5) for x in v.values()):
            raise ValueError("multipliers must be between 0 and 5")
        return v


class HistoryRequest(Model):
    region_id: str
    dates: list[datetime] = Field(min_length=2, max_length=7)


@api.post("/forest/projections")
def create_projection(payload: ProjectionRequest, role: Role = Depends(require_roles(*ANALYSIS))):
    try:
        return projection.project(payload.region_id, payload.scenario_multipliers, role.value)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@api.get("/forest/projections")
def list_projections(region_id: str | None = None, role: Role = Depends(require_roles(*READ))):
    return [r["result"] for r in list_rows("projections", "WHERE region_id = ?" if region_id else "", (region_id,) if region_id else (), limit=50)]


@api.get("/forest/projections/{projection_id}/report.pdf")
def projection_pdf(projection_id: str, role: Role = Depends(require_roles(*READ))):
    r = get_one("projections", "id", projection_id)
    if not r:
        _404("Projection")
    p = r["result"]
    blocks: list[tuple[str, Any]] = [("Status", {"status": p["status"], "claim_type": p["claim_type"], "reason": p.get("reason")})]
    if p["status"] == "OK":
        blocks += [("Historical intervals used", [f"{i['from']} to {i['to']}: {i['loss_ha']} ha over {i['days']} days" for i in p["intervals_used"]])]
        for name, sc in p["projections"].items():
            blocks.append((f"{name.replace('_', ' ').title()} ({sc['kind']}, multiplier {sc['multiplier']})", {f"{h} days": f"{v['expected_additional_loss_ha']} ha" + (f" (range {v['range_ha'][0]}-{v['range_ha'][1]})" if v["range_ha"] else "") for h, v in sc["by_horizon_days"].items()}))
        blocks += [("Backtest", p["backtest"]), ("Assumptions", p["assumptions"]), ("Warnings and limitations", p["warnings"] + p["limitations"])]
    return Response(generic_pdf("RAKSHAK forest projection report", "Projection from historical change rate - not a validated forecast", blocks), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="rakshak-projection-{projection_id[:8]}.pdf"'})


@api.post("/forest/history/build")
def build_history(payload: HistoryRequest, role: Role = Depends(require_roles(Role.ADMIN, Role.ANALYST, Role.REVIEWER))):
    if not get_one("regions", "id", payload.region_id):
        _404("Monitoring region")
    dates = [d if d.tzinfo else d.replace(tzinfo=timezone.utc) for d in payload.dates]
    return _job(jobs.submit("history_build", lambda: projection.build_history(payload.region_id, dates, role.value)))


# ===================================================================== AR scenes
@api.get("/ar/forest-scenes/{region_id}")
def forest_scene(region_id: str, role: Role = Depends(require_roles(*READ))):
    if not get_one("regions", "id", region_id):
        _404("Monitoring region")
    with connection() as conn:
        stamp = conn.execute("SELECT (SELECT COUNT(*) FROM changes WHERE region_id=?), (SELECT COUNT(*) FROM simulations WHERE region_id=?), (SELECT COUNT(*) FROM forecasts WHERE region_id=?), (SELECT COUNT(*) FROM projections WHERE region_id=?)", (region_id,) * 4).fetchone()
    cache = Path(settings.raster_dir) / f"scene-{region_id}-{'-'.join(map(str, stamp))}.json"
    if cache.is_file():
        return Response(cache.read_text(encoding="utf-8"), media_type="application/json")
    try:
        scene = forestscene.build(region_id)
    except Exception as exc:  # noqa: BLE001 - provider failures are reported, not hidden
        raise HTTPException(status_code=503, detail=f"Scene unavailable: {exc}")
    body = json.dumps(scene)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(body, encoding="utf-8")
    return Response(body, media_type="application/json")


# ===================================================================== hazards and navigation
class HazardCreate(Model):
    kind: Literal["closure", "flood", "waterlogging", "washout", "restricted_access", "other"]
    geometry: dict[str, Any]
    severity: Literal["info", "warning", "critical"] = "warning"
    source: str = Field(min_length=2, max_length=200, description="Who or what reported this (e.g. 'Range officer phone report')")
    expires_in_hours: float | None = Field(default=48, gt=0, le=24 * 90)
    note: str | None = Field(default=None, max_length=1000)
    verified: bool = False

    @field_validator("geometry")
    @classmethod
    def geom(cls, v):
        if v.get("type") not in ("Point", "LineString", "Polygon", "MultiPolygon"):
            raise ValueError("Unsupported hazard geometry")
        return v


@api.post("/navigation/hazards")
def create_hazard(payload: HazardCreate, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER, Role.OFFICER))):
    validate_geojson(payload.geometry) if payload.geometry["type"] != "LineString" else None
    rec = {"id": str(uuid.uuid4()), "kind": payload.kind, "geometry": payload.geometry, "severity": payload.severity, "source": payload.source, "reported_by": role.value, "reported_at": now(), "expires_at": (datetime.now(timezone.utc) + timedelta(hours=payload.expires_in_hours)).isoformat() if payload.expires_in_hours else None, "verified": 1 if (payload.verified and role in (Role.ADMIN, Role.REVIEWER)) else 0, "note": payload.note, "created_at": now()}
    insert("hazards", rec)
    audit(role.value, "hazard_reported", "hazard", rec["id"], {"kind": payload.kind, "verified": bool(rec["verified"])})
    return {**rec, "verified": bool(rec["verified"])}


@api.get("/navigation/hazards")
def list_hazards(role: Role = Depends(require_roles(*READ))):
    return [{**h, "verified": bool(h["verified"])} for h in navigation.active_hazards()]


@api.post("/navigation/hazards/{hazard_id}/verify")
def verify_hazard(hazard_id: str, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER))):
    if not get_one("hazards", "id", hazard_id):
        _404("Hazard")
    update("hazards", "id", hazard_id, {"verified": 1})
    audit(role.value, "hazard_verified", "hazard", hazard_id, {})
    return {"id": hazard_id, "verified": True}


@api.post("/navigation/hazards/{hazard_id}/resolve")
def resolve_hazard(hazard_id: str, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER, Role.OFFICER))):
    if not get_one("hazards", "id", hazard_id):
        _404("Hazard")
    update("hazards", "id", hazard_id, {"expires_at": now()})
    audit(role.value, "hazard_resolved", "hazard", hazard_id, {})
    return {"id": hazard_id, "resolved": True}


class RoutePlan(Model):
    incident_id: str
    origin: dict[str, Any] | None = None
    alternatives: bool = True

    @field_validator("origin")
    @classmethod
    def pt(cls, v):
        if v is not None:
            if v.get("type") != "Point":
                raise ValueError("origin must be a GeoJSON Point")
            validate_geojson(v)
        return v


def _destination(incident: dict[str, Any]) -> list[float]:
    g = incident["geometry"]
    return g["coordinates"] if g["type"] == "Point" else list(to_shape(g).representative_point().coords[0])


def _plan(incident: dict[str, Any], origin: list[float], origin_source: str, alternatives: bool) -> dict[str, Any]:
    dest = _destination(incident)
    r = router_service.route(origin, dest, alternatives)
    if r["status"] != "ok":
        return {"status": r["status"], "reason": r.get("reason"), "details": r.get("details"), "origin": {"type": "Point", "coordinates": origin}, "origin_source": origin_source, "destination": {"type": "Point", "coordinates": dest}}
    flags = []
    try:
        rain = rainfall_summary(dest[1], dest[0])
        if rain.get("heavy_rain_in_window"):
            flags.append(f"Heavy rainfall (>= 50 mm/day) in the recent or forecast window near the destination; last 7 days {rain['last_7_days_mm']} mm. Check for flooding and waterlogging.")
        elif rain.get("anomaly_ratio") and rain["anomaly_ratio"] > 1.5:
            flags.append("Rainfall over the last 30 days is well above normal for this period; low-lying and unpaved sections may be waterlogged.")
    except WeatherUnavailable:
        flags.append("Rainfall data unavailable; weather hazards not assessed.")
    ev = navigation.evaluate_routes([r["primary"], *r.get("alternatives", [])], navigation.active_hazards(), flags)
    return {"status": "ok", "provider": r["provider"], "origin": {"type": "Point", "coordinates": origin}, "origin_source": origin_source, "destination": {"type": "Point", "coordinates": dest}, "evaluation": ev, "caveat": r.get("caveat"), "fallback_note": r.get("fallback_note"), "warning": r.get("warning"), "planned_at": now()}


def _origin(payload_origin: dict[str, Any] | None, dest: list[float]) -> tuple[list[float], str]:
    if payload_origin:
        return payload_origin["coordinates"], "supplied (device or manual)"
    st = nearest_station(list_rows("stations", limit=1000), dest)
    if not st:
        raise HTTPException(status_code=409, detail="No origin supplied and no stations are registered")
    return st[0]["location"]["coordinates"], f"nearest registered station by straight line: {st[0]['name']} ({st[1]:.1f} km); availability not known"


@api.post("/navigation/routes")
def plan_route(payload: RoutePlan, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER, Role.OFFICER, Role.ANALYST))):
    inc = get_one("incidents", "id", payload.incident_id) or _404("Incident")
    origin, src = _origin(payload.origin, _destination(inc))
    return _plan(inc, origin, src, payload.alternatives)


class SessionStart(Model):
    incident_id: str
    origin: dict[str, Any]
    consent_location_use: bool
    route_index: int | None = None


@api.post("/navigation/sessions")
def start_session(payload: SessionStart, role: Role = Depends(require_roles(*FIELD))):
    if not payload.consent_location_use:
        raise HTTPException(status_code=422, detail="Location use requires the responder's informed consent")
    inc = get_one("incidents", "id", payload.incident_id) or _404("Incident")
    if inc["status"] not in ("APPROVED_FOR_INSPECTION", "ASSIGNED", "DISPATCHED", "ACKNOWLEDGED", "FIELD_VERIFICATION_PENDING"):
        raise HTTPException(status_code=409, detail=f"Incident is {inc['status']}; navigation is available only for reviewed incidents")
    plan = _plan(inc, payload.origin["coordinates"], "device or manual position", True)
    if plan["status"] != "ok":
        raise HTTPException(status_code=502, detail=f"No route available: {plan.get('reason')}")
    idx = payload.route_index if payload.route_index is not None else plan["evaluation"]["recommended_index"]
    if idx is None:
        raise HTTPException(status_code=409, detail=plan["evaluation"]["message"])
    chosen = plan["evaluation"]["routes"][idx]
    if chosen["status"] == "blocked":
        raise HTTPException(status_code=409, detail="Selected route is blocked by a recorded hazard")
    if not chosen.get("geometry"):
        raise HTTPException(status_code=409, detail="Selected route has no geometry; pick a route from a provider with geometry")
    sid = str(uuid.uuid4())
    result = {"plan": {k: v for k, v in plan.items() if k != "evaluation"}, "route": chosen, "evaluation": {k: v for k, v in plan["evaluation"].items() if k != "routes"}, "last_position": None, "reroutes": 0, "max_off_route_m": 0}
    insert("nav_sessions", {"id": sid, "incident_id": payload.incident_id, "status": "ACTIVE", "started_at": now(), "ended_at": None, "consent": 1, "result": result, "created_at": now()})
    audit(role.value, "navigation_started", "incident", payload.incident_id, {"session_id": sid})
    return {"session_id": sid, "route": chosen, "evaluation": plan["evaluation"], "destination": plan["destination"], "privacy": "Only your latest position and summary figures are kept; no track is stored."}


class PositionUpdate(Model):
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    accuracy_m: float | None = Field(default=None, ge=0, le=100000)
    heading_deg: float | None = Field(default=None, ge=0, le=360)


def _session(sid: str) -> dict[str, Any]:
    s = get_one("nav_sessions", "id", sid)
    return s if s else _404("Navigation session")


@api.post("/navigation/sessions/{session_id}/position")
def update_position(session_id: str, payload: PositionUpdate, role: Role = Depends(require_roles(*FIELD))):
    s = _session(session_id)
    if s["status"] != "ACTIVE":
        raise HTTPException(status_code=409, detail="Session is not active")
    res = s["result"]
    geom = res["route"]["geometry"]
    prog = navigation.progress_on_route(geom, payload.lon, payload.lat)
    dest = res["plan"]["destination"]["coordinates"]
    bearing = navigation.bearing_deg(payload.lon, payload.lat, dest[0], dest[1])
    step = navigation.next_step(res["route"].get("steps"), geom, payload.lon, payload.lat)
    warnings = []
    if payload.accuracy_m is not None and payload.accuracy_m > 30:
        warnings.append(f"GPS accuracy is poor ({round(payload.accuracy_m)} m); guidance may be unreliable")
    haz = [h for h in navigation.active_hazards() if h["verified"]]
    ahead = navigation.evaluate_routes([{**{k: res["route"][k] for k in ("distance_km", "duration_min")}, "geometry": geom}], haz)["routes"][0]
    if ahead["status"] == "blocked":
        warnings.append("A newly recorded verified hazard now blocks this route. Recalculate before continuing.")
    res["last_position"] = {"lon": payload.lon, "lat": payload.lat, "at": now(), "accuracy_m": payload.accuracy_m}
    res["max_off_route_m"] = max(res["max_off_route_m"], prog["off_route_m"])
    update("nav_sessions", "id", session_id, {"result": res})
    arrived = prog["distance_remaining_m"] < 30 and prog["on_route"]
    return {**prog, "bearing_to_destination_deg": round(bearing, 1), "next_step": step, "warnings": warnings, "recalculate_suggested": (not prog["on_route"]) or ahead["status"] == "blocked", "arrived": arrived, "route_status": ahead["status"], "reasons": ahead["reasons"]}


@api.post("/navigation/sessions/{session_id}/reroute")
def reroute(session_id: str, payload: PositionUpdate, role: Role = Depends(require_roles(*FIELD))):
    s = _session(session_id)
    if s["status"] != "ACTIVE":
        raise HTTPException(status_code=409, detail="Session is not active")
    inc = get_one("incidents", "id", s["incident_id"])
    plan = _plan(inc, [payload.lon, payload.lat], "device position (reroute)", True)
    if plan["status"] != "ok":
        raise HTTPException(status_code=502, detail=f"Reroute failed: {plan.get('reason')}")
    idx = plan["evaluation"]["recommended_index"]
    if idx is None:
        return {"rerouted": False, "message": plan["evaluation"]["message"], "evaluation": plan["evaluation"]}
    res = s["result"]
    res["route"], res["reroutes"] = plan["evaluation"]["routes"][idx], res["reroutes"] + 1
    update("nav_sessions", "id", session_id, {"result": res})
    audit(role.value, "navigation_rerouted", "incident", s["incident_id"], {"session_id": session_id})
    return {"rerouted": True, "route": res["route"], "evaluation": plan["evaluation"]}


class SessionEnd(Model):
    outcome: Literal["arrived", "aborted"]
    notes: str | None = Field(default=None, max_length=1000)


@api.post("/navigation/sessions/{session_id}/end")
def end_session(session_id: str, payload: SessionEnd, role: Role = Depends(require_roles(*FIELD))):
    s = _session(session_id)
    res = s["result"]
    res["last_position"] = None  # drop the last known position once navigation ends
    summary = {"outcome": payload.outcome, "reroutes": res["reroutes"], "max_off_route_m": res["max_off_route_m"], "route_distance_km": res["route"]["distance_km"], "route_status_at_end": res["route"]["status"], "notes": payload.notes}
    res["summary"] = summary
    update("nav_sessions", "id", session_id, {"status": "ENDED", "ended_at": now(), "result": res})
    audit(role.value, "navigation_ended", "incident", s["incident_id"], {"session_id": session_id, **summary})
    return {"session_id": session_id, "summary": summary}


@api.get("/navigation/sessions/{session_id}")
def get_session(session_id: str, role: Role = Depends(require_roles(*FIELD))):
    s = _session(session_id)
    return {k: s[k] for k in ("id", "incident_id", "status", "started_at", "ended_at")} | {"summary": s["result"].get("summary"), "reroutes": s["result"]["reroutes"]}


# ===================================================================== industrial
class SiteCreate(Model):
    name: str = Field(min_length=2, max_length=200)
    geometry: dict[str, Any]
    kind: Literal["factory", "mining", "quarry", "construction", "road", "power", "other", "unclassified"] = "unclassified"
    source: Literal["permit_record", "field_report", "user_drawn"] = "user_drawn"
    permit_ref: str | None = Field(default=None, max_length=200)
    region_id: str | None = None

    @field_validator("geometry")
    @classmethod
    def geom(cls, v):
        if v.get("type") not in ("Polygon", "MultiPolygon", "Point"):
            raise ValueError("Site geometry must be Point, Polygon or MultiPolygon")
        return validate_geojson(v)


@api.post("/industrial/sites")
def create_site(payload: SiteCreate, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER, Role.ANALYST))):
    return industrial.register(payload.name, payload.geometry, payload.kind, payload.source, payload.permit_ref, payload.region_id, role.value)


@api.post("/industrial/sites/from-change/{change_id}")
def site_from_change(change_id: str, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER, Role.ANALYST))):
    return industrial.promote_from_change(change_id, role.value)


@api.get("/industrial/sites")
def list_sites(role: Role = Depends(require_roles(*READ))):
    return list_rows("industrial_sites", limit=500)


@api.post("/industrial/sites/{site_id}/assess")
def assess_site(site_id: str, role: Role = Depends(require_roles(*ANALYSIS))):
    if not get_one("industrial_sites", "id", site_id):
        _404("Site")
    return _job(jobs.submit("industrial_assessment", lambda: industrial.assess(site_id, role.value)))


@api.get("/industrial/sites/{site_id}/report.pdf")
def site_pdf(site_id: str, role: Role = Depends(require_roles(*READ))):
    s = get_one("industrial_sites", "id", site_id) or _404("Site")
    if not s["result"]:
        raise HTTPException(status_code=409, detail="Run the assessment first")
    a = s["result"]
    blocks: list[tuple[str, Any]] = [("Site", {"name": s["name"], "status": s["status"], "classification": a["classification_note"], "area_ha": a["site_area_hectares"], "permit_ref": s["permit_ref"]})]
    for name, sec in a["sections"].items():
        flat = {k: (json.dumps(v)[:500] if isinstance(v, (dict, list)) else v) for k, v in sec.items() if k != "attribution"}
        blocks.append((f"{name.replace('_', ' ').title()}  [{sec.get('evidence_class')}]", flat))
    blocks += [("Recommended follow-up", a["recommended_follow_up"]), ("Limits", ["Proximity and overlap are facts about geometry; they do not show that the site caused any pollution or damage."])]
    return Response(generic_pdf("RAKSHAK site assessment", s["name"], blocks), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="rakshak-site-{site_id[:8]}.pdf"'})


# ===================================================================== wildfire vision / correlation
@api.get("/wildfires/vision/status")
def vision_status(role: Role = Depends(require_roles(*READ))):
    return {**detector.status(), "temporal_filter": {"required": "3 of last 5 frames per camera"}, "limitations": "Candidates only: fog, cloud, dust, glare and industrial smoke cause false positives."}


@api.post("/wildfires/vision/analyze")
async def vision_analyze(file: UploadFile = File(...), camera_id: str = Form(..., min_length=1, max_length=80), lat: float = Form(..., ge=-90, le=90), lon: float = Form(..., ge=-180, le=180), observed_at: str | None = Form(None), bearing_deg: float | None = Form(None), role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER, Role.OFFICER))):
    st = detector.status()
    if not st["usable"]:
        raise HTTPException(status_code=503, detail=f"Fire vision unavailable: {st['reason']}")
    raw = await file.read(settings.max_evidence_mb * 1024 * 1024 + 1)
    if len(raw) > settings.max_evidence_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image too large")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (UnidentifiedImageError, OSError):
        raise HTTPException(status_code=422, detail="File is not a readable image")
    try:
        when = datetime.fromisoformat(observed_at.replace("Z", "+00:00")) if observed_at else datetime.now(timezone.utc)
    except ValueError:
        raise HTTPException(status_code=422, detail="observed_at must be ISO-8601")
    try:
        res = detector.detect(img)
    except FireModelUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    fire = [d for d in res["detections"] if d["class"] in ("fire", "smoke", "flame")]
    filt = temporal.update(camera_id, bool(fire))
    out = {**res, "camera_id": camera_id, "temporal_filter": filt, "candidate_incident_created": False}
    if fire and filt["confirmed_by_filter"]:
        wid = str(uuid.uuid4())
        sha = hashlib.sha256(raw).hexdigest()
        evdir = Path(settings.evidence_dir) / "wildfire"
        evdir.mkdir(parents=True, exist_ok=True)
        (evdir / f"{wid}.jpg").write_bytes(_jpeg(img))
        top = max(fire, key=lambda d: d["confidence"])
        try:
            insert("wildfire_observations", {"id": wid, "geometry": {"type": "Point", "coordinates": [lon, lat]}, "observed_at": when.isoformat(), "observation_type": "camera_detection", "confidence": top["confidence"], "source": f"camera:{camera_id}", "status": "NEEDS_REVIEW", "evidence": {"claim_type": "candidate_inference", "location_kind": "camera position; the fire lies somewhere in the camera's view", "bearing_deg": bearing_deg, "detections": fire, "model": res["model"], "model_sha256": res["model_sha256"], "temporal_filter": filt, "frame_sha256": sha, "frame_file": f"wildfire/{wid}.jpg", "uploaded_by_role": role.value}, "created_at": now()})
            out.update(candidate_incident_created=True, wildfire_id=wid)
        except Exception:  # noqa: BLE001 - dedupe index may reject an identical record
            out["note"] = "Identical record already exists"
    return out


def _jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=85)
    return buf.getvalue()


@api.get("/wildfires/correlations")
def fire_correlations(radius_km: float = Query(2.0, gt=0, le=25), hours: float = Query(24.0, gt=0, le=168), role: Role = Depends(require_roles(*READ))):
    rows = [r for r in list_rows("wildfire_observations", "WHERE status != 'DISMISSED'", limit=2000)]
    return correlate(rows, radius_km, hours)


@api.post("/wildfires/{wildfire_id}/create-incident")
def fire_incident(wildfire_id: str, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER, Role.OFFICER))):
    w = get_one("wildfire_observations", "id", wildfire_id) or _404("Wildfire observation")
    if w["status"] != "CONFIRMED":
        raise HTTPException(status_code=409, detail="Review and confirm the observation before opening an incident")
    iid = str(uuid.uuid4())
    insert("incidents", {"id": iid, "change_id": None, "wildfire_id": wildfire_id, "status": "NEEDS_REVIEW", "title": f"Fire observation ({w['observation_type'].replace('_', ' ')}) from {w['source']}", "geometry": w["geometry"], "evidence": {"claim_type": "candidate_inference", "wildfire_status": w["status"]}, "created_at": now(), "updated_at": now()})
    audit(role.value, "incident_created_from_wildfire", "incident", iid, {"wildfire_id": wildfire_id})
    return {"incident_id": iid}


# ===================================================================== evidence files and case view
ALLOWED = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".pdf": "application/pdf", ".txt": "text/plain", ".csv": "text/csv", ".json": "application/json", ".geojson": "application/geo+json"}


@api.post("/incidents/{incident_id}/evidence")
async def upload_evidence(incident_id: str, file: UploadFile = File(...), role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER, Role.OFFICER, Role.ANALYST))):
    if not get_one("incidents", "id", incident_id):
        _404("Incident")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED:
        raise HTTPException(status_code=415, detail=f"Allowed types: {', '.join(sorted(ALLOWED))}")
    limit = settings.max_evidence_mb * 1024 * 1024
    raw = await file.read(limit + 1)
    if len(raw) > limit:
        raise HTTPException(status_code=413, detail=f"File exceeds {settings.max_evidence_mb} MB")
    if ext in (".png", ".jpg", ".jpeg"):
        try:
            Image.open(io.BytesIO(raw)).verify()
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=422, detail="Not a valid image")
    if ext == ".pdf" and not raw.startswith(b"%PDF"):
        raise HTTPException(status_code=422, detail="Not a valid PDF")
    eid = str(uuid.uuid4())
    d = Path(settings.evidence_dir) / incident_id
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{eid}{ext}"  # server-generated name; user filename is metadata only
    path.write_bytes(raw)
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(file.filename or "file").name)[:120]
    rec = {"id": eid, "incident_id": incident_id, "filename": name, "content_type": ALLOWED[ext], "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "path": str(path), "uploaded_by": role.value, "created_at": now()}
    insert("evidence_files", rec)
    audit(role.value, "evidence_uploaded", "incident", incident_id, {"evidence_id": eid, "sha256": rec["sha256"], "filename": name})
    return {k: v for k, v in rec.items() if k != "path"}


@api.get("/incidents/{incident_id}/evidence")
def list_evidence(incident_id: str, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER, Role.OFFICER, Role.ANALYST))):
    return [{k: v for k, v in e.items() if k != "path"} for e in list_rows("evidence_files", "WHERE incident_id = ?", (incident_id,), limit=200)]


@api.get("/evidence/{evidence_id}/download")
def download_evidence(evidence_id: str, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER, Role.OFFICER, Role.ANALYST))):
    e = get_one("evidence_files", "id", evidence_id)
    if not e or not Path(e["path"]).is_file():
        _404("Evidence")
    audit(role.value, "evidence_accessed", "incident", e["incident_id"], {"evidence_id": evidence_id})
    return FileResponse(e["path"], media_type=e["content_type"], filename=e["filename"], headers={"X-Content-Type-Options": "nosniff"})


@api.get("/incidents/{incident_id}/case")
def case_detail(incident_id: str, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER, Role.OFFICER, Role.ANALYST))):
    inc = get_one("incidents", "id", incident_id) or _404("Incident")
    change = get_one("changes", "id", inc["change_id"]) if inc.get("change_id") else None
    obs = []
    if change:
        for key, meta in (change["provenance"].get("observations") or {}).items():
            o = get_one("observations", "id", meta["id"])
            if o and o["status"] == "OK":
                obs.append({"role": key, "observation_id": o["id"], "sensor": o["provenance"].get("sensor"), "acquired_from": o["acquisition_start"], "acquired_to": o["acquisition_end"], "valid_pixel_fraction": o["quality"].get("valid_pixel_fraction"), "scenes": o["source_scene_ids"]})
    with connection() as conn:
        events = [decode_row(dict(r)) for r in conn.execute("SELECT * FROM audit_events WHERE resource_type='incident' AND resource_id = ? ORDER BY id", (incident_id,))]
        notes = [decode_row(dict(r)) for r in conn.execute("SELECT id, channel, status, created_at FROM notifications WHERE payload LIKE ? ORDER BY created_at", (f'%{incident_id}%',))]
    assessments = list_rows("authorization_assessments", "WHERE change_id = ?", (change["id"],), limit=20) if change else []
    return {"incident": inc, "change": ({k: change[k] for k in ("id", "area_hectares", "score", "baseline_date", "comparison_date", "status", "disturbance_type", "geometry")} | {"provenance": change["provenance"]}) if change else None, "observations": obs, "authorization_assessments": assessments, "evidence_files": [{k: v for k, v in e.items() if k != "path"} for e in list_rows("evidence_files", "WHERE incident_id = ?", (incident_id,), limit=100)], "audit_trail": events, "notifications": notes, "status_note": "No matching authorisation found means verification is required, not that the activity is illegal." if any(a["status"] == "NO_RECORD_FOUND" for a in assessments) else None}


# ===================================================================== monthly alert service (CL-01 phase 2)
class AlertRunRequest(Model):
    region_id: str
    year: int = Field(ge=2017, le=2100)
    n_months: int = Field(default=12, ge=1, le=12)
    ref_months: int = Field(default=12, ge=2, le=12, description="Months before January used as the baseline (12 = full prior year, needed for the seasonal baseline)")
    seasonal: bool = Field(default=True, description="Compare each scene with the same time of year in the baseline period")
    use_radar: bool = True
    held_out: bool = Field(default=False, description="Declare that this region was not used to tune thresholds")


class EvalRequest(Model):
    phase1_change_id: str | None = None
    top_k: int = Field(default=50, ge=1, le=500)


@api.post("/alerts/runs")
def start_alert_run(payload: AlertRunRequest, role: Role = Depends(require_roles(Role.ADMIN, Role.ANALYST, Role.REVIEWER))):
    if not get_one("regions", "id", payload.region_id):
        _404("Monitoring region")
    rid = str(uuid.uuid4())
    params = alert_engine.AlertParams(seasonal=payload.seasonal)
    job = jobs.submit("alert_run", lambda: alert_engine.run(payload.region_id, payload.year, payload.n_months, payload.ref_months, payload.use_radar, params, role.value, payload.held_out, run_id=rid))
    return {"run_id": rid, "job_id": job, "status": "queued", "params_digest": params.digest()}


@api.get("/alerts/runs")
def list_alert_runs(region_id: str | None = None, role: Role = Depends(require_roles(*READ))):
    rows = list_rows("alert_runs", "WHERE region_id = ?" if region_id else "", (region_id,) if region_id else (), limit=50)
    return [{"id": r["id"], "region_id": r["region_id"], "year": r["year"], "status": r["status"], "progress": r["progress"], "created_at": r["created_at"], "held_out": bool(r["held_out"]), "use_radar": r["params"].get("use_radar"), "summary": (r["result"] or {}).get("summary")} for r in rows]


@api.get("/alerts/runs/{run_id}")
def get_alert_run(run_id: str, role: Role = Depends(require_roles(*READ))):
    r = get_one("alert_runs", "id", run_id) or _404("Alert run")
    return {**r, "held_out": bool(r["held_out"])}


@api.get("/alerts/runs/{run_id}/alerts")
def run_alerts(run_id: str, month: str | None = None, evidence: str | None = None, limit: int = Query(200, ge=1, le=1000), role: Role = Depends(require_roles(*READ))):
    clauses, args = ["run_id = ?"], [run_id]
    if month:
        clauses.append("issue_month = ?"); args.append(month)
    if evidence:
        clauses.append("evidence = ?"); args.append(evidence)
    with connection() as conn:
        rows = [decode_row(dict(r)) for r in conn.execute(f"SELECT * FROM alerts WHERE {' AND '.join(clauses)} ORDER BY rank LIMIT ?", (*args, limit))]
    return [{**r, "monsoon": bool(r["monsoon"])} for r in rows]


@api.get("/alerts/runs/{run_id}/top")
def run_top(run_id: str, k: int = Query(50, ge=1, le=500), month: str | None = None, role: Role = Depends(require_roles(*READ))):
    rows = run_alerts(run_id, month=month, limit=k, role=role)
    return {"k": k, "returned": len(rows), "ranking": "score = evidence weight x sqrt(area ha) x date-certainty factor (uncalibrated; not a probability)", "alerts": rows}


@api.get("/alerts/runs/{run_id}/export.geojson")
def export_geojson(run_id: str, role: Role = Depends(require_roles(*READ))):
    rows = run_alerts(run_id, limit=1000, role=role)
    audit(role.value, "alerts_exported", "alert_run", run_id, {"format": "geojson", "n": len(rows)})
    fc = {"type": "FeatureCollection", "name": f"rakshak-alerts-{run_id[:8]}", "features": [{"type": "Feature", "geometry": r["geometry"], "properties": {k: v for k, v in r.items() if k not in ("geometry", "details", "created_at")}} for r in rows], "notice": "Model-generated candidate alerts; verify before acting. Not legal findings."}
    return Response(json.dumps(fc), media_type="application/geo+json", headers={"Content-Disposition": f'attachment; filename="rakshak-alerts-{run_id[:8]}.geojson"'})


@api.get("/alerts/runs/{run_id}/export.csv")
def export_csv(run_id: str, role: Role = Depends(require_roles(*READ))):
    rows = run_alerts(run_id, limit=1000, role=role)
    audit(role.value, "alerts_exported", "alert_run", run_id, {"format": "csv", "n": len(rows)})
    out = [{"rank": r["rank"], "issue_month": r["issue_month"], "estimated_loss_date": r["est_date"], "date_uncertainty_days": r["date_uncertainty_days"], "issue_date": r["issue_date"], "area_ha": r["area_ha"], "evidence": r["evidence"], "monsoon": r["monsoon"], "score": r["score"], "centroid_lon": round(to_shape(r["geometry"]).centroid.x, 6), "centroid_lat": round(to_shape(r["geometry"]).centroid.y, 6)} for r in rows]
    return Response(to_csv(out, list(out[0].keys()) if out else ["rank"]), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="rakshak-alerts-{run_id[:8]}.csv"'})


@api.post("/alerts/runs/{run_id}/evaluate")
def evaluate_alert_run(run_id: str, payload: EvalRequest, role: Role = Depends(require_roles(Role.ADMIN, Role.ANALYST, Role.REVIEWER))):
    r = get_one("alert_runs", "id", run_id) or _404("Alert run")
    if r["status"] != "COMPLETE":
        raise HTTPException(status_code=409, detail="The alert run has not completed")
    return _job(jobs.submit("alert_evaluation", lambda: alert_eval.evaluate_run(run_id, payload.phase1_change_id, role.value, payload.top_k)))


@api.get("/alerts/runs/{run_id}/evaluations")
def run_evaluations(run_id: str, role: Role = Depends(require_roles(*READ))):
    return [e["result"] for e in list_rows("alert_evaluations", "WHERE run_id = ?", (run_id,), limit=20)]
