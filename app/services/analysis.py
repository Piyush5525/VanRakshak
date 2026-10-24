import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.core.config import get_settings
from app.db.database import audit, get_one, insert, list_rows, now
from app.services.acquisition import acquire_pair
from app.services.osm import OsmUnavailable, context as osm_context
from app.services.forecasting import run_forecast
from app.services.changedetect import detect_from_rasters
from app.services.satellite import AcquisitionConfig, ProviderError
from app.services.geoanalysis import assess_candidate, fragmentation


def _raster_path(name: str) -> str:
    root = Path(get_settings().raster_dir).resolve()
    path = (root / name).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=422, detail="Raster must be an existing file inside RASTER_DIR")
    return str(path)


def detect_change(request, actor: str) -> dict[str, Any]:
    region = get_one("regions", "id", request.region_id)
    if not region:
        raise HTTPException(status_code=404, detail="Monitoring region not found")
    change_id = str(uuid.uuid4())
    provenance: dict[str, Any] = {"provider": request.provider or "unconfigured", "model_version": request.model_version or "untrained", "threshold": request.threshold, "claim_type": "candidate_inference"}
    status, geometry, area, score, disturbance = "UNAVAILABLE", region["geometry"], 0.0, None, "INSUFFICIENT_EVIDENCE"
    paths: dict[str, str | None] | None = None
    if request.provider == "planetary_computer":
        cfg = AcquisitionConfig(window_days=request.window_days, max_cloud_cover=request.max_cloud_cover)
        try:
            obs = acquire_pair(region, request.baseline_date, request.comparison_date, cfg, actor)
        except (ProviderError, ValueError) as exc:
            provenance["data_quality"] = {"status": "unavailable", "reason": str(exc)}
            obs = None
        if obs:
            provenance["observations"] = {k: {"id": v["id"], "status": v["status"], "valid_pixel_fraction": v["quality"]["valid_pixel_fraction"], "acquired": [v["acquisition_start"], v["acquisition_end"]], "scenes": v["source_scene_ids"], "cache_hit": v["provenance"].get("cache_hit"), "warnings": v["provenance"]["warnings"]} for k, v in obs.items()}
            gap = abs((request.baseline_date.timetuple().tm_yday - request.comparison_date.timetuple().tm_yday + 183) % 365 - 183)
            provenance["seasonal_offset_days"] = gap
            if gap > 45:
                provenance["seasonal_warning"] = "Baseline and comparison are in different seasons; phenology can mimic change"
            if obs["before_s2"]["status"] == "OK" and obs["after_s2"]["status"] == "OK":
                s1_ok = obs["before_s1"]["status"] == "OK" and obs["after_s1"]["status"] == "OK"
                paths = {"before": obs["before_s2"]["provenance"]["file"], "after": obs["after_s2"]["provenance"]["file"], "before_s1": obs["before_s1"]["provenance"]["file"] if s1_ok else None, "after_s1": obs["after_s1"]["provenance"]["file"] if s1_ok else None, "forest": obs["forest"]["provenance"]["file"] if obs["forest"]["status"] == "OK" else None}
            else:
                provenance["data_quality"] = {"status": "unavailable", "reason": "Sentinel-2 composite missing for at least one date; no change inferred from radar alone"}
    elif request.before_raster and request.after_raster:
        paths = {"before": _raster_path(request.before_raster), "after": _raster_path(request.after_raster), "before_s1": None, "after_s1": None, "forest": None}
        provenance["input_rasters"] = [request.before_raster, request.after_raster]
    else:
        provenance["data_quality"] = {"status": "unavailable", "reason": "Set provider=planetary_computer or supply raster files"}
    if paths:
        result = detect_from_rasters(paths["before"], paths["after"], before_s1=paths["before_s1"], after_s1=paths["after_s1"], forest_mask_path=paths["forest"])
        polygons = result["polygons"]
        status = result["status"]
        provenance.update({"method": result["method"], "config": result["config"], "radar": result["radar"], "baseline_forest": result.get("baseline_forest"), "data_quality": {"valid_pixel_fraction": result["valid_pixel_fraction"], "cloud_fraction": result["cloud_fraction"], "reason": result.get("reason")}, "polygon_scores": [{k: v for k, v in p.items() if k != "geometry"} for p in polygons]})
        if polygons:
            geometry = {"type": "MultiPolygon", "coordinates": [p["geometry"]["coordinates"] for p in polygons]}
            area = round(sum(p["area_hectares"] for p in polygons), 4)
            score = round(sum(p["mean_score"] * p["area_hectares"] for p in polygons) / area, 4)
            disturbance = "CANDIDATE_FOREST_LOSS"
    record = {"id": change_id, "region_id": request.region_id, "geometry": geometry, "area_hectares": area, "baseline_date": request.baseline_date.isoformat(), "comparison_date": request.comparison_date.isoformat(), "disturbance_type": disturbance, "score": score, "status": status, "provenance": provenance, "created_at": now()}
    insert("changes", record)
    audit(actor, "change_detection_requested", "change", change_id, {"status": status, "area_hectares": area})
    record["candidate_incident_id"] = _maybe_open_incident(record, provenance, actor)
    return record


INCIDENT_MIN_AREA_HA = 0.5  # configured criteria for opening a *review* case
INCIDENT_MIN_AGREEING_PATCHES = 1


def _maybe_open_incident(record: dict[str, Any], provenance: dict[str, Any], actor: str) -> str | None:
    """Open a NEEDS_REVIEW case when area and optical+radar agreement criteria are met. Never dispatches."""
    if record["disturbance_type"] != "CANDIDATE_FOREST_LOSS" or record["area_hectares"] < INCIDENT_MIN_AREA_HA:
        return None
    agreeing = [p for p in provenance.get("polygon_scores", []) if p.get("evidence_level") == "optical_radar_agree"]
    if len(agreeing) < INCIDENT_MIN_AGREEING_PATCHES:
        return None
    incident_id = str(uuid.uuid4())
    insert("incidents", {"id": incident_id, "change_id": record["id"], "wildfire_id": None, "status": "NEEDS_REVIEW", "title": f"Candidate forest loss {record['area_hectares']} ha ({len(agreeing)} radar-supported patches)", "geometry": record["geometry"], "evidence": {"claim_type": "candidate_inference", "auto_created": True, "criteria": {"min_area_ha": INCIDENT_MIN_AREA_HA, "min_optical_radar_agreeing_patches": INCIDENT_MIN_AGREEING_PATCHES}}, "created_at": now(), "updated_at": now()})
    audit(actor, "candidate_incident_created", "incident", incident_id, {"change_id": record["id"], "status": "NEEDS_REVIEW"})
    return incident_id


def forecast(request, actor: str) -> dict[str, Any]:
    region = get_one("regions", "id", request.region_id)
    if not region:
        raise HTTPException(status_code=404, detail="Monitoring region not found")
    origin = request.origin or datetime.now(timezone.utc)
    if origin.tzinfo is None:
        origin = origin.replace(tzinfo=timezone.utc)
    if origin > datetime.now(timezone.utc):
        raise HTTPException(status_code=422, detail="Forecast origin cannot be in the future")
    forecast_id = str(uuid.uuid4())
    result = run_forecast(region, request.horizon_days, origin)
    provenance = {"claim_type": "forecast_estimate", "event_definition": "new loss detection recorded in a 0.01 degree cell within the horizon", "model_version": result.get("model_version", "none"), "model_id": result.get("model_id"), "evaluation_id": result.get("evaluation_id"), "input_freshness_days": result.get("input_freshness_days"), "reason": result.get("reason"), "score_kind": result.get("score_kind"), "limitations": ["Trained only on detections stored in this system", "Risk is not a prediction of polygon growth", "Time-ordered validation; no spatial hold-out unless stated in the evaluation"]}
    cells = result.get("cells", [])
    record = {"id": forecast_id, "region_id": request.region_id, "origin": origin.isoformat(), "horizon_days": request.horizon_days, "status": result["status"], "risk_score": max((c["risk"] for c in cells), default=None), "provenance": provenance, "created_at": now()}
    insert("forecasts", record)
    if cells:
        insert("forecast_layers", {"id": str(uuid.uuid4()), "forecast_id": forecast_id, "cells": cells, "created_at": now()})
    audit(actor, "forecast_requested", "forecast", forecast_id, {"status": result["status"], "horizon_days": request.horizon_days})
    return record


def simulate(request, actor: str) -> dict[str, Any]:
    region = get_one("regions", "id", request.region_id)
    if not region:
        raise HTTPException(status_code=404, detail="Monitoring region not found")
    if request.mode == "model_based":
        raise HTTPException(status_code=409, detail="Model-based simulation unavailable: no trained and evaluated forecast model is configured")
    metrics = fragmentation(region["geometry"], request.geometry)
    simulation = {
        "id": str(uuid.uuid4()),
        "region_id": request.region_id,
        "status": "SIMULATED",
        "claim_type": "hypothetical_assumption",
        "is_observation": False,
        "is_forecast": False,
        "horizon_days": request.horizon_days,
        "mode": request.mode,
        "initial_forest_state": "region_polygon_assumed_forest",
        "assumptions": request.assumptions,
        "geometry": request.geometry,
        "assumed_additional_area_hectares": metrics["loss_within_forest_hectares"],
        "fragmentation": metrics,
        "carbon": {"status": "not_calculated", "reason": "No justified biomass model configured"},
        "uncertainty": "not quantified; user-defined scenario",
        "provenance": {"method": "user_defined_geometry", "generated_at": now()},
        "created_at": now(),
    }
    insert("simulations", {"id": simulation["id"], "region_id": simulation["region_id"], "status": simulation["status"], "claim_type": simulation["claim_type"], "horizon_days": simulation["horizon_days"], "mode": simulation["mode"], "geometry": simulation["geometry"], "assumptions": simulation["assumptions"], "result": simulation, "created_at": simulation["created_at"]})
    audit(actor, "simulation_created", "simulation", simulation["id"], simulation["provenance"])
    return simulation


def match_authorizations(request, actor: str) -> dict[str, Any]:
    change = get_one("changes", "id", request.change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change record not found")
    if request.candidate_authorization_ids:
        candidates = [get_one("authorizations", "id", item) for item in request.candidate_authorization_ids]
        candidates = [item for item in candidates if item]
    else:
        candidates = list_rows("authorizations", limit=1000)
    observed = datetime.fromisoformat(change["comparison_date"])
    assessed = [assess_candidate(change["geometry"], observed, c) for c in candidates if c.get("geometry")]
    assessed = [a for a in assessed if a["overlap_fraction"]]
    rank = ["MATCH_REQUIRES_REVIEW", "POSSIBLE_SCOPE_OR_CONDITION_MISMATCH", "RECORD_EXPIRED_OR_REVOKED_CANDIDATE", "POTENTIAL_MATCH", "AUTHORIZATION_STATUS_UNCLEAR"]
    status = next((r for r in rank if any(a["state"] == r for a in assessed)), "NO_RECORD_FOUND")
    rationale = {
        "candidate_count": len(candidates),
        "overlapping_candidates": assessed,
        "records_searched": "local ingested records only; official-portal coverage not established",
        "legal_conclusion": "none",
        "warning": "Absence of a matching online record is not a finding of illegality",
    }
    assessment = {"id": str(uuid.uuid4()), "change_id": request.change_id, "status": status, "candidate_ids": [a["authorization_id"] for a in assessed], "rationale": rationale, "created_at": now()}
    insert("authorization_assessments", assessment)
    audit(actor, "authorization_match_assessed", "authorization_assessment", assessment["id"], {"status": status})
    return assessment


def impact_assessment(incident: dict[str, Any], actor: str) -> dict[str, Any]:
    area, frag = 0.0, "not_calculated_without_change_and_region"
    if incident.get("change_id"):
        change = get_one("changes", "id", incident["change_id"])
        if change:
            area = float(change["area_hectares"])
            region = get_one("regions", "id", change["region_id"]) if change.get("region_id") else None
            if region and area > 0:
                frag = fragmentation(region["geometry"], change["geometry"])
    osm_ctx: Any = {"status": "not_requested", "reason": "No incident geometry"}
    try:
        osm_ctx = osm_context(incident["geometry"])
    except OsmUnavailable as exc:
        osm_ctx = {"status": "unavailable", "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 - context is best-effort and must not fail the assessment
        osm_ctx = {"status": "error", "reason": type(exc).__name__}
    assessment = {"id": str(uuid.uuid4()), "incident_id": incident["id"], "status": "SCREENING_ONLY", "claim_type": "inferred_consequence", "metrics": {"detected_loss_area_hectares": area, "fragmentation": frag, "access_and_context_osm": osm_ctx, "runoff": "not_calculated_without_elevation_rainfall_and_soil", "carbon": "not_calculated_without_biomass_model"}, "assumptions": ["No ecological consequence is asserted from area alone"], "created_at": now()}
    insert("impact_assessments", {"id": assessment["id"], "incident_id": assessment["incident_id"], "status": assessment["status"], "claim_type": assessment["claim_type"], "result": assessment, "created_at": assessment["created_at"]})
    audit(actor, "impact_assessment_created", "incident", incident["id"], {"status": assessment["status"]})
    return assessment
