import uuid
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import get_settings
from app.core.middleware import RequestContextMiddleware, configure_logging
from app.core.security import Role, current_role, require_roles
from app.db.database import audit, get_one, init_db, insert, list_rows, now, update
from app.domain.state_machine import transition
from app.schemas import (
    AssignmentRequest,
    AuthorizationCreate,
    BuildingEnergyRequest,
    ChangeDetectionRequest,
    DispatchRequest,
    FieldVerificationRequest,
    ForecastRequest,
    IncidentCreate,
    MatchAssessmentRequest,
    ObservationCreate,
    RegionCreate,
    ReviewRequest,
    RouteRequest,
    ScenarioRequest,
    SimulationRequest,
    WildfireIngestRequest,
)
from app.services.analysis import detect_change, forecast, impact_assessment, match_authorizations, simulate
from app.services.acquisition import get_composite
from app.services.forecasting import train as train_model
from app.core.geometry import to_shape
from app.services.energy import predict_observed, scenario as energy_scenario
from app.services.ar import build_scene as build_ar_scene
from app.services.leads import LeadsUnavailable, search as search_leads
from app.services.jobs import JobManager
from app.services.wildfire import FirmsUnavailable, fetch_firms, store_detections
from app.services.satellite import AcquisitionConfig
from app.fire_routes import api as fire_api
from app.forest_routes import api as forest_api
from app.live_routes import api as live_api
from app.rakshak_routes import api as rakshak_api
from app.ops_routes import api as ops_api, notifier, router_service
from app.services.notify import real_sends_enabled
from app.services.providers import FIRMSProvider, SatelliteProvider
from app.services.reports import build_report


settings = get_settings()
settings.validate_runtime()
configure_logging(settings.log_level)
init_db()
try:
    from app.services import firecomms as _fc

    _fc.start_polling()  # opt-in only (TELEGRAM_POLLING=true)
except Exception:  # noqa: BLE001 - never block startup
    pass
jobs = JobManager(settings.job_workers)
satellite = SatelliteProvider(settings)
firms = FIRMSProvider(settings)


WEBHOOKS = ("/notifications/telegram/callback", "/notifications/voice/status", "/notifications/voice/gather", "/fire/telegram/webhook", "/fire/ivr/twilio/status")


def _guard(request: Request):
    """Every /api call needs a valid identity; provider webhooks authenticate themselves (secret / signature)."""
    path = request.url.path
    if not path.startswith(settings.api_prefix) or path.endswith(WEBHOOKS):
        return None
    return current_role(request, request.headers.get("x-role"), request.headers.get("authorization"))


app = FastAPI(dependencies=[Depends(_guard)], title="RAKSHAK", version="0.1.0", description="Evidence-driven forest intelligence and response backend")
app.include_router(ops_api)
app.include_router(rakshak_api)
app.include_router(live_api)
app.include_router(fire_api)
app.include_router(forest_api)
app.add_middleware(RequestContextMiddleware, per_minute=settings.rate_limit_per_minute)


@app.get("/health")
def health():
    return {"status": "ok", "service": settings.app_name, "environment": settings.environment}


@app.get("/ready")
def ready():
    return {"status": "ready", "database": "sqlite_initialized", "optional_integrations": "safe_mode"}


@app.get(settings.api_prefix + "/system/capabilities")
def capabilities():
    return {"features": {"regions": True, "evidence_records": True, "change_detection": True, "forecasting": True, "simulation": True, "wildfire_records": True, "authorization_matching": True, "incident_workflow": True, "reports": True, "satellite_acquisition": satellite.status().usable, "real_satellite_inference": False, "real_forecast_model": any(m["status"] == "USABLE" for m in list_rows("model_registry", limit=200)), "real_fire_ingestion": firms.status().usable, "real_routing": router_service.available, "real_notifications": real_sends_enabled(settings)}, "safe_mode": settings.environment != "production"}


@app.get(settings.api_prefix + "/system/providers")
def providers():
    return [
        {"name": "planetary_computer", "configured": True, "usable": True, "reason": "Public STAC catalogue: Sentinel-2 L2A, Sentinel-1 RTC, ESA WorldCover (no credentials; network required)"},
        firms.status().__dict__,
        {"name": "telegram", "configured": bool(settings.telegram_bot_token), "usable": bool(settings.telegram_bot_token) and real_sends_enabled(settings), "reason": "Real sends need ENVIRONMENT=production and ALLOW_REAL_NOTIFICATIONS=true"},
        {"name": "voice", "configured": settings.voice_provider == "twilio" and bool(settings.twilio_account_sid), "usable": settings.voice_provider == "twilio" and real_sends_enabled(settings), "reason": "Twilio credentials plus the production send gate"},
        router_service.status(),
    ]


@app.get(settings.api_prefix + "/system/models")
def models():
    registry = list_rows("model_registry", limit=200)
    risk = [{"name": m["name"], "version": m["version"], "horizon_days": m["horizon_days"], "configured": True, "usable": m["status"] == "USABLE", "status": m["status"], "checksum": m["checksum"], "reason": "Evaluated on a held-out later period" if m["status"] == "USABLE" else "Did not beat the baseline or failed calibration"} for m in registry]
    if not any(m["usable"] for m in risk):
        risk.append({"name": "forest-risk", "version": "none", "configured": False, "usable": False, "reason": "No horizon-specific model has been trained and evaluated on stored detections"})
    return [{"name": "change-detection", "version": "dNBR-baseline-v1 + Sentinel-1 VH support", "configured": True, "usable": True, "kind": "rule-based candidate generator, not a trained model", "reason": "Needs Sentinel-2 composites; radar is supporting evidence"}, *risk, {"name": "building-energy", "version": "seasonal-profile-v1", "configured": True, "usable": True, "reason": "Needs >=12 months of metered history; scenarios need user-stated intensity. No benchmark table is invented."}]


@app.get(settings.api_prefix + "/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post(settings.api_prefix + "/regions")
def create_region(payload: RegionCreate, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER))):
    region_id = str(uuid.uuid4())
    record = {"id": region_id, "name": payload.name, "jurisdiction": payload.jurisdiction, "geometry": payload.geometry, "created_at": now()}
    insert("regions", record)
    audit(role.value, "region_created", "region", region_id, {"name": payload.name})
    return record


@app.get(settings.api_prefix + "/regions")
def list_regions(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    return list_rows("regions", limit=limit, offset=offset)


@app.get(settings.api_prefix + "/regions/{region_id}")
def get_region(region_id: str):
    record = get_one("regions", "id", region_id)
    if not record:
        raise HTTPException(status_code=404, detail="Monitoring region not found")
    return record


@app.patch(settings.api_prefix + "/regions/{region_id}")
def patch_region(region_id: str, payload: dict, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER))):
    region = get_region(region_id)
    allowed = {key: payload[key] for key in ("name", "jurisdiction", "geometry") if key in payload}
    if not allowed:
        raise HTTPException(status_code=422, detail="No editable fields supplied")
    update("regions", "id", region_id, allowed)
    audit(role.value, "region_updated", "region", region_id, allowed)
    return get_region(region_id)


@app.post(settings.api_prefix + "/regions/{region_id}/observations")
def create_observation(region_id: str, payload: ObservationCreate, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER))):
    region = get_region(region_id)
    if payload.provider != "planetary_computer":
        raise HTTPException(status_code=422, detail="Supported providers: planetary_computer (Sentinel-1 RTC and Sentinel-2 L2A)")
    sensor = payload.processing_config.get("sensor", "sentinel-2")
    if sensor not in ("sentinel-1", "sentinel-2"):
        raise HTTPException(status_code=422, detail="processing_config.sensor must be sentinel-1 or sentinel-2")
    if not (payload.acquisition_start and payload.acquisition_end) or payload.acquisition_start > payload.acquisition_end:
        raise HTTPException(status_code=422, detail="acquisition_start and acquisition_end are required, start <= end")
    span = (payload.acquisition_end - payload.acquisition_start).days
    if span > 180:
        raise HTTPException(status_code=422, detail="Window longer than 180 days is not allowed")
    centre = payload.acquisition_start + (payload.acquisition_end - payload.acquisition_start) / 2
    cfg = AcquisitionConfig(window_days=max(5, span // 2 + 1), max_cloud_cover=float(payload.processing_config.get("max_cloud_cover", 60)))

    def task():
        return get_composite(region, sensor, centre.date(), cfg, actor=role.value)

    return {"job_id": jobs.submit("satellite_observation", task), "status": "queued"}


@app.get(settings.api_prefix + "/regions/{region_id}/observations")
def list_observations(region_id: str):
    get_region(region_id)
    return list_rows("observations", "WHERE region_id = ?", (region_id,))


@app.post(settings.api_prefix + "/forest/change-detection")
def request_change_detection(payload: ChangeDetectionRequest, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER))):
    return {"run_id": jobs.submit("change_detection", lambda: detect_change(payload, role.value)), "status": "queued", "claim_type": "candidate_inference"}


@app.get(settings.api_prefix + "/forest/change-detection/{run_id}")
def get_change_run(run_id: str):
    job = jobs.get(run_id)
    if not job:
        raise HTTPException(status_code=404, detail="Change detection run not found")
    return job


@app.get(settings.api_prefix + "/forest/changes")
def list_changes(region_id: str | None = None, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    if region_id:
        return list_rows("changes", "WHERE region_id = ?", (region_id,), limit=limit, offset=offset)
    return list_rows("changes", limit=limit, offset=offset)


@app.get(settings.api_prefix + "/forest/changes/{change_id}")
def get_change(change_id: str):
    change = get_one("changes", "id", change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change record not found")
    return change


@app.get(settings.api_prefix + "/forest/changes/{change_id}/evidence")
def change_evidence(change_id: str):
    change = get_change(change_id)
    return {"change_id": change_id, "observed": [], "inferred": [change], "forecast": [], "simulation": [], "limitations": change["provenance"].get("data_quality")}


@app.post(settings.api_prefix + "/forest/forecasts")
def request_forecast(payload: ForecastRequest, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER))):
    return forecast(payload, role.value)


@app.get(settings.api_prefix + "/forest/forecasts/{forecast_id}")
def get_forecast(forecast_id: str):
    record = get_one("forecasts", "id", forecast_id)
    if not record:
        raise HTTPException(status_code=404, detail="Forecast not found")
    return record


@app.get(settings.api_prefix + "/forest/forecasts/{forecast_id}/layers")
def forecast_layers(forecast_id: str):
    record = get_forecast(forecast_id)
    rows = list_rows("forecast_layers", "WHERE forecast_id = ?", (forecast_id,), limit=1)
    return {"forecast_id": forecast_id, "status": record["status"], "claim_type": "forecast_estimate", "is_observation": False, "cell_size_degrees": 0.01, "layers": rows[0]["cells"] if rows else [], "reason": record["provenance"].get("reason"), "model_id": record["provenance"].get("model_id"), "evaluation_id": record["provenance"].get("evaluation_id")}


@app.post(settings.api_prefix + "/forest/forecasts/train")
def train_forecast_model(payload: dict, role: Role = Depends(require_roles(Role.ADMIN))):
    horizon = int(payload.get("horizon_days", 0))
    if horizon not in (7, 30, 90):
        raise HTTPException(status_code=422, detail="horizon_days must be 7, 30 or 90")
    region_id = payload.get("region_id")
    job = jobs.submit("forecast_training", lambda: train_model(region_id, horizon, role.value))
    audit(role.value, "forecast_training_requested", "model", None, {"horizon_days": horizon, "region_id": region_id})
    return {"job_id": job, "status": "queued"}


@app.post(settings.api_prefix + "/forest/simulations")
def create_simulation(payload: SimulationRequest, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER))):
    return simulate(payload, role.value)


@app.get(settings.api_prefix + "/forest/simulations/{simulation_id}")
def get_simulation(simulation_id: str):
    record = get_one("simulations", "id", simulation_id)
    if not record:
        raise HTTPException(status_code=404, detail="Simulation not found")
    return record


@app.get(settings.api_prefix + "/forest/history")
def forest_history(region_id: str):
    return history_frames(region_id)


@app.get(settings.api_prefix + "/forest/history/{region_id}/frames")
def history_frames(region_id: str, start: str | None = None, end: str | None = None):
    """Frames carry real observation dates; nothing is interpolated between them."""
    get_region(region_id)
    frames = []
    for o in list_rows("observations", "WHERE region_id = ? AND status = 'OK'", (region_id,), limit=500):
        if not o["acquisition_start"]:
            continue
        frames.append({"type": "observation", "sensor": o["provenance"].get("sensor"), "observed_from": o["acquisition_start"], "observed_to": o["acquisition_end"], "processed_at": o["provenance"].get("processed_at"), "observation_id": o["id"], "scenes": o["source_scene_ids"], "valid_pixel_fraction": o["quality"].get("valid_pixel_fraction"), "warnings": o["provenance"].get("warnings", []), "claim_type": "observation"})
    for c in list_rows("changes", "WHERE region_id = ? AND disturbance_type = 'CANDIDATE_FOREST_LOSS'", (region_id,), limit=500):
        frames.append({"type": "detected_change", "baseline_date": c["baseline_date"], "observed_from": c["comparison_date"], "change_id": c["id"], "area_hectares": c["area_hectares"], "claim_type": "candidate_inference"})
    if start:
        frames = [f for f in frames if f["observed_from"] >= start]
    if end:
        frames = [f for f in frames if f["observed_from"] <= end]
    frames.sort(key=lambda f: f["observed_from"])
    gaps = [{"from": a["observed_from"], "to": b["observed_from"], "days": (datetime.fromisoformat(b["observed_from"]) - datetime.fromisoformat(a["observed_from"])).days} for a, b in zip(frames, frames[1:]) if (datetime.fromisoformat(b["observed_from"]) - datetime.fromisoformat(a["observed_from"])).days > 45]
    return {"region_id": region_id, "frames": frames, "observation_gaps": gaps, "interpolated": False}


@app.post(settings.api_prefix + "/wildfires/ingest")
def ingest_wildfire(payload: WildfireIngestRequest, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER))):
    wildfire_id = str(uuid.uuid4())
    record = {"id": wildfire_id, "geometry": payload.geometry, "observed_at": payload.observed_at.isoformat(), "observation_type": payload.observation_type, "confidence": payload.confidence, "source": payload.source, "status": "NEEDS_REVIEW", "evidence": payload.evidence, "created_at": now()}
    insert("wildfire_observations", record)
    audit(role.value, "wildfire_ingested", "wildfire", wildfire_id, {"source": payload.source, "observation_type": payload.observation_type})
    return record


@app.post(settings.api_prefix + "/wildfires/ingest-firms")
def ingest_firms(payload: dict, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER))):
    region = get_region(str(payload.get("region_id", "")))
    source, days = str(payload.get("source", "VIIRS_SNPP_NRT")), int(payload.get("days", 2))
    try:
        rows = fetch_firms(settings, to_shape(region["geometry"]).bounds, source, days)
    except FirmsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return store_detections(rows, source, region, role.value)


@app.get(settings.api_prefix + "/wildfires")
def list_wildfires():
    return list_rows("wildfire_observations")


@app.get(settings.api_prefix + "/wildfires/{wildfire_id}")
def get_wildfire(wildfire_id: str):
    record = get_one("wildfire_observations", "id", wildfire_id)
    if not record:
        raise HTTPException(status_code=404, detail="Wildfire observation not found")
    return record


@app.post(settings.api_prefix + "/wildfires/{wildfire_id}/review")
def review_wildfire(wildfire_id: str, payload: ReviewRequest, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER, Role.OFFICER))):
    get_wildfire(wildfire_id)
    status = "CONFIRMED" if payload.decision == "confirm" else "DISMISSED" if payload.decision == "dismiss" else "NEEDS_REVIEW"
    update("wildfire_observations", "id", wildfire_id, {"status": status})
    audit(role.value, "wildfire_reviewed", "wildfire", wildfire_id, {"decision": payload.decision, "notes": payload.notes})
    return get_wildfire(wildfire_id)


@app.post(settings.api_prefix + "/incidents/{incident_id}/impact-assessment")
def create_impact_assessment(incident_id: str, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER, Role.OFFICER))):
    incident = get_incident(incident_id)
    return impact_assessment(incident, role.value)


@app.get(settings.api_prefix + "/incidents/{incident_id}/impact-assessment")
def get_impact_assessment(incident_id: str):
    records = list_rows("impact_assessments", "WHERE incident_id = ?", (incident_id,))
    return records[0] if records else {"incident_id": incident_id, "status": "not_generated", "metrics": {}}


@app.post(settings.api_prefix + "/authorizations/ingest")
def ingest_authorization(payload: AuthorizationCreate, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER))):
    record = {"id": str(uuid.uuid4()), **payload.model_dump(), "created_at": now()}
    insert("authorizations", record)
    audit(role.value, "authorization_ingested", "authorization", record["id"], {"authority": payload.authority, "source_url": payload.source_url})
    return record


@app.get(settings.api_prefix + "/authorizations/search")
def search_authorizations(q: str | None = None, status: str | None = None):
    clauses, args = [], []
    if q:
        clauses.append("(project_name LIKE ? OR project_identifier LIKE ? OR authority LIKE ?)")
        args.extend([f"%{q}%"] * 3)
    if status:
        clauses.append("status = ?")
        args.append(status)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    return list_rows("authorizations", where, tuple(args))


@app.post(settings.api_prefix + "/authorizations/match-assessment")
def authorization_match(payload: MatchAssessmentRequest, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER))):
    return match_authorizations(payload, role.value)


@app.get(settings.api_prefix + "/authorizations/match-assessment/{assessment_id}")
def get_authorization_assessment(assessment_id: str):
    record = get_one("authorization_assessments", "id", assessment_id)
    if not record:
        raise HTTPException(status_code=404, detail="Authorization assessment not found")
    return record


@app.get(settings.api_prefix + "/forest/simulations/{simulation_id}/ar")
def simulation_ar_scene(simulation_id: str, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER, Role.OFFICER, Role.READ_ONLY))):
    sim = get_simulation(simulation_id)
    region = get_region(sim["region_id"])
    try:
        return build_ar_scene(sim, region)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc))


@app.post(settings.api_prefix + "/authorizations/web-leads")
def authorization_web_leads(payload: dict, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER))):
    query = str(payload.get("query", "")).strip()
    if len(query) < 3 or len(query) > 200:
        raise HTTPException(status_code=422, detail="query must be 3-200 characters")
    try:
        out = search_leads(settings, query)
    except LeadsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    audit(role.value, "authorization_web_leads_searched", "authorization", None, {"query": query, "results": len(out["leads"])})
    return out


@app.get(settings.api_prefix + "/system/client-config")
def client_config():
    """Public, browser-safe configuration. Only a public (pk.) Mapbox token is ever returned."""
    token = settings.mapbox_api if (settings.mapbox_api or "").startswith("pk.") else None
    return {"mapbox_public_token": token, "osm_attribution": "(c) OpenStreetMap contributors"}


@app.post(settings.api_prefix + "/buildings/energy-predictions")
def create_energy_prediction(payload: BuildingEnergyRequest, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER))):
    return predict_observed(payload, role.value)


@app.get(settings.api_prefix + "/buildings/energy-predictions/{prediction_id}")
def get_energy_prediction(prediction_id: str):
    record = get_one("energy_predictions", "id", prediction_id)
    if not record:
        raise HTTPException(status_code=404, detail="Energy prediction not found")
    return record


@app.post(settings.api_prefix + "/buildings/scenarios")
def create_building_scenario(payload: ScenarioRequest, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER))):
    return energy_scenario(payload, role.value)


@app.get(settings.api_prefix + "/buildings/scenarios/{scenario_id}")
def get_building_scenario(scenario_id: str):
    record = get_one("energy_predictions", "id", scenario_id)
    if not record:
        raise HTTPException(status_code=404, detail="Building scenario not found")
    return record


def get_incident(incident_id: str):
    incident = get_one("incidents", "id", incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident


@app.get(settings.api_prefix + "/incidents")
def list_incidents():
    return list_rows("incidents")


@app.post(settings.api_prefix + "/incidents")
def create_incident(payload: IncidentCreate, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER))):
    incident_id = str(uuid.uuid4())
    record = {"id": incident_id, "change_id": payload.change_id, "wildfire_id": payload.wildfire_id, "status": "DETECTED", "title": payload.title, "geometry": payload.geometry, "evidence": payload.evidence, "created_at": now(), "updated_at": now()}
    insert("incidents", record)
    audit(role.value, "incident_created", "incident", incident_id, {"status": "DETECTED"})
    return record


@app.get(settings.api_prefix + "/incidents/{incident_id}")
def incident(incident_id: str):
    return get_incident(incident_id)


@app.post(settings.api_prefix + "/incidents/{incident_id}/review")
def review_incident(incident_id: str, payload: ReviewRequest, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER))):
    item = get_incident(incident_id)
    target = {"approve": "APPROVED_FOR_INSPECTION", "dismiss": "DISMISSED", "confirm": "CONFIRMED", "unconfirm": "UNCONFIRMED"}[payload.decision]
    transition(item["status"], target)
    update("incidents", "id", incident_id, {"status": target, "updated_at": now()})
    audit(role.value, "incident_reviewed", "incident", incident_id, {"decision": payload.decision, "notes": payload.notes})
    return get_incident(incident_id)


@app.post(settings.api_prefix + "/incidents/{incident_id}/assign")
def assign_incident(incident_id: str, payload: AssignmentRequest, role: Role = Depends(require_roles(Role.ADMIN, Role.OFFICER))):
    item = get_incident(incident_id)
    transition(item["status"], "ASSIGNED")
    update("incidents", "id", incident_id, {"status": "ASSIGNED", "evidence": {**item["evidence"], "assignee": payload.assignee}, "updated_at": now()})
    audit(role.value, "incident_assigned", "incident", incident_id, {"assignee": payload.assignee})
    return get_incident(incident_id)


@app.post(settings.api_prefix + "/incidents/{incident_id}/field-verification")
def field_verification(incident_id: str, payload: FieldVerificationRequest, role: Role = Depends(require_roles(Role.ADMIN, Role.OFFICER))):
    item = get_incident(incident_id)
    target = "CONFIRMED" if payload.outcome == "confirmed" else "UNCONFIRMED" if payload.outcome == "unconfirmed" else "FIELD_VERIFICATION_PENDING"
    transition(item["status"], target)
    evidence = {**item["evidence"], "field_verification": {"outcome": payload.outcome, "notes": payload.notes, "observed_at": payload.observed_at.isoformat() if payload.observed_at else None}}
    update("incidents", "id", incident_id, {"status": target, "evidence": evidence, "updated_at": now()})
    audit(role.value, "field_verification_recorded", "incident", incident_id, evidence["field_verification"])
    return get_incident(incident_id)


@app.post(settings.api_prefix + "/incidents/{incident_id}/close")
def close_incident(incident_id: str, role: Role = Depends(require_roles(Role.ADMIN, Role.OFFICER))):
    item = get_incident(incident_id)
    transition(item["status"], "CLOSED")
    update("incidents", "id", incident_id, {"status": "CLOSED", "updated_at": now()})
    audit(role.value, "incident_closed", "incident", incident_id, {})
    return get_incident(incident_id)


@app.post(settings.api_prefix + "/incidents/{incident_id}/reports")
def create_report(incident_id: str, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER, Role.OFFICER))):
    return build_report(incident_id, settings, role.value)


@app.get(settings.api_prefix + "/reports/{report_id}")
def get_report(report_id: str, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER, Role.OFFICER))):
    record = get_one("reports", "id", report_id)
    if not record:
        raise HTTPException(status_code=404, detail="Report not found")
    return {k: v for k, v in record.items() if k != "path"} | {"download": f"{settings.api_prefix}/reports/{report_id}/download"}


@app.get(settings.api_prefix + "/reports/{report_id}/download")
def download_report(report_id: str, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER, Role.OFFICER))):
    record = get_one("reports", "id", report_id)
    if not record or not Path(record["path"]).is_file():
        raise HTTPException(status_code=404, detail="Report not found")
    audit(role.value, "report_downloaded", "report", report_id, {})
    return FileResponse(record["path"], media_type="application/pdf" if record["format"] == "pdf" else "application/json", filename=f"vananetra-{report_id}.{record['format']}")


@app.get(settings.api_prefix + "/audit/events")
def audit_events(limit: int = Query(100, ge=1, le=500), role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER))):
    return list_rows("audit_events", limit=limit)


@app.get(settings.api_prefix + "/evaluations")
def evaluations():
    return list_rows("evaluations")


@app.get(settings.api_prefix + "/evaluations/{evaluation_id}")
def evaluation(evaluation_id: str):
    record = get_one("evaluations", "id", evaluation_id)
    if not record:
        raise HTTPException(status_code=404, detail="Evaluation not found")
    return record


frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/assets", StaticFiles(directory=frontend_dir), name="assets")

    @app.get("/", include_in_schema=False)
    def landing():
        return FileResponse(frontend_dir / "landing.html")

    @app.get("/live", include_in_schema=False)
    def live_map():
        return FileResponse(frontend_dir / "live.html")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        from fastapi import Response

        return Response(status_code=204)

    @app.get("/app", include_in_schema=False)
    def console():
        return FileResponse(frontend_dir / "index.html")
