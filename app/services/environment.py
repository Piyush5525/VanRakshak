"""Environmental impact assessment for a region / detected change.

Every section is tagged:
  measured   - third-party observation/model data fetched now (elevation, rainfall, OSM features)
  computed   - geometry arithmetic on stored detections
  screening  - rule-of-thumb indicator; NOT a validated model of runoff, erosion or flooding
  not_available - data or model absent; explains why. Nothing is estimated from area alone.
"""
import uuid
from typing import Any

from fastapi import HTTPException

from app.core.geometry import geodesic_area_hectares, to_shape
from app.db.database import audit, get_one, insert, list_rows, now
from app.services.geoanalysis import fragmentation
from app.services.osm import OsmUnavailable, context as osm_context
from app.services.satellite import ProviderError
from app.services.terrain import stats_for
from app.services.weather import WeatherUnavailable, rainfall_summary


def _section(kind: str, **data: Any) -> dict[str, Any]:
    return {"evidence_class": kind, **data}


def assess(region_id: str, change_id: str | None, actor: str) -> dict[str, Any]:
    region = get_one("regions", "id", region_id)
    if not region:
        raise HTTPException(status_code=404, detail="Monitoring region not found")
    change = get_one("changes", "id", change_id) if change_id else None
    if change_id and not change:
        raise HTTPException(status_code=404, detail="Change record not found")
    if not change:
        rows = list_rows("changes", "WHERE region_id = ? AND disturbance_type = 'CANDIDATE_FOREST_LOSS'", (region_id,), limit=1)
        change = rows[0] if rows else None

    shape = to_shape(region["geometry"])
    c = shape.centroid
    sections: dict[str, Any] = {}
    warnings: list[str] = []

    # forest change (computed from stored candidate detections)
    if change:
        sections["forest_change"] = _section("computed", change_id=change["id"], baseline_date=change["baseline_date"], comparison_date=change["comparison_date"], candidate_loss_hectares=change["area_hectares"], status=change["status"], note="Candidate loss from a rule-based detector; not verified on the ground", evidence_levels=_count([p.get("evidence_level") for p in change["provenance"].get("polygon_scores", [])]))
        sections["fragmentation"] = _section("computed", **fragmentation(region["geometry"], change["geometry"]))
    else:
        sections["forest_change"] = _section("not_available", reason="No detected change stored for this region; run change detection first")
        sections["fragmentation"] = _section("not_available", reason="Needs a detected change")

    # terrain (measured DEM, screening on slope)
    try:
        terrain_region = stats_for(region["geometry"])
        terrain_loss = stats_for(change["geometry"]) if change and change["area_hectares"] > 0 else None
        sections["terrain"] = _section("measured", region=terrain_region, within_candidate_loss=terrain_loss)
        if terrain_loss and terrain_loss.get("status") == "ok":
            share = terrain_loss["share_steeper_than_screen"]
            sections["erosion_susceptibility"] = _section("screening", share_of_loss_on_steep_slopes=share, slope_threshold_deg=terrain_loss["screen_threshold_deg"], level="elevated" if share >= 0.3 else "moderate" if share >= 0.1 else "low", note="Slope-only screening. Soil type, rainfall intensity, ground cover and land management are not modelled; this is not an erosion or sediment-yield estimate.")
        else:
            sections["erosion_susceptibility"] = _section("not_available", reason="Needs candidate loss polygons and DEM")
    except (ProviderError, Exception) as exc:  # noqa: BLE001
        warnings.append(f"Terrain unavailable: {type(exc).__name__}")
        sections["terrain"] = _section("not_available", reason=f"DEM fetch failed: {exc}")
        sections["erosion_susceptibility"] = _section("not_available", reason="DEM unavailable")

    # rainfall (measured/model)
    try:
        rain = rainfall_summary(c.y, c.x)
        sections["rainfall"] = _section("measured", **rain)
        flags = []
        if rain.get("anomaly_ratio") and rain["anomaly_ratio"] > 1.5:
            flags.append("Last-30-day rainfall exceeds 1.5x the 5-year mean for this window")
        if rain.get("heavy_rain_in_window"):
            flags.append("Daily rainfall at or above 50 mm in the recent/forecast window")
        sections["water_runoff_screening"] = _section("screening", flags=flags, note="Combines rainfall anomalies with cleared slopes as a prompt for field checks; no hydrological model is run. Flood exposure and waterlogging need dedicated data (not ingested).")
    except WeatherUnavailable as exc:
        sections["rainfall"] = _section("not_available", reason=str(exc))
        sections["water_runoff_screening"] = _section("not_available", reason="Rainfall unavailable")

    # OSM water/protected areas/settlements (third-party mapping, incomplete)
    try:
        osm = osm_context(change["geometry"] if change and change["area_hectares"] > 0 else region["geometry"])
        sections["nearby_features_osm"] = _section("measured", **osm)
    except OsmUnavailable as exc:
        sections["nearby_features_osm"] = _section("not_available", reason=str(exc))
    except Exception as exc:  # noqa: BLE001
        sections["nearby_features_osm"] = _section("not_available", reason=type(exc).__name__)

    sections["biodiversity"] = _section("not_available", reason="No species-range, habitat or corridor dataset is ingested. Only OSM protected-area membership (above, when available) is reported; no species presence is inferred.")
    sections["carbon"] = _section("not_available", reason="No justified biomass/carbon-density model is configured. Carbon is deliberately not derived from area alone.")
    sections["watershed"] = _section("not_available", reason="No watershed boundary dataset ingested; nearest mapped waterway (OSM) is the only water context.")

    result = {
        "id": str(uuid.uuid4()), "region_id": region_id, "region_name": region["name"], "change_id": change["id"] if change else None,
        "region_area_hectares": round(geodesic_area_hectares(shape), 1), "sections": sections, "warnings": warnings,
        "recommended_follow_up": _follow_up(sections),
        "method_notes": ["Observed or fetched data, computed geometry, screening indicators and unavailable items are labelled separately", "Candidate loss is a model output and not confirmed deforestation"],
        "generated_at": now(), "created_at": now(),
    }
    insert("environment_assessments", {"id": result["id"], "region_id": region_id, "change_id": result["change_id"], "status": "COMPLETE" if not warnings else "COMPLETE_WITH_GAPS", "result": result, "created_at": result["created_at"]})
    audit(actor, "environment_assessment_created", "environment_assessment", result["id"], {"region_id": region_id, "gaps": len(warnings)})
    return result


def _count(values: list) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return out


def _follow_up(sections: dict[str, Any]) -> list[str]:
    out = []
    if sections["forest_change"]["evidence_class"] == "computed":
        out.append("Field-verify the largest candidate patches, starting with those where Sentinel-1 and Sentinel-2 agree")
    if sections.get("erosion_susceptibility", {}).get("level") in ("elevated", "moderate"):
        out.append("Inspect cleared slopes near drainage lines for rills, slumping or sediment delivery")
    if sections.get("water_runoff_screening", {}).get("flags"):
        out.append("Check access and drainage conditions after the recent heavy rainfall")
    nearby = sections.get("nearby_features_osm", {})
    if nearby.get("nearest_waterway") and nearby["nearest_waterway"]["distance_m"] < 200:
        out.append("Assess buffer integrity along the nearby waterway")
    out.append("Commission a biodiversity and carbon assessment with appropriate datasets before drawing ecological or climate conclusions")
    return out
