"""Industrial / construction site registry and screening assessment.

Satellite imagery alone cannot reliably say a factory exists. Sites enter as (a) registered by a user
from a permit or field record, or (b) "potential land-use change" promoted from a detection.
Indicators are proximity and overlap facts; none asserts pollution or damage.
"""
import uuid
from typing import Any

import httpx
from fastapi import HTTPException
from shapely.ops import unary_union

from app.core.geometry import geodesic_area_hectares, to_shape
from app.db.database import audit, get_one, insert, list_rows, now
from app.services.geoanalysis import _equal_area
from app.services.osm import OsmUnavailable, context as osm_context
from app.services.terrain import stats_for

POTENTIAL_LABEL = "Potential construction or land-use change - verification required"
BUFFERS_M = (0, 500, 2000)


def register(name: str, geometry: dict[str, Any], kind: str, source: str, permit_ref: str | None, region_id: str | None, actor: str) -> dict[str, Any]:
    status = "POTENTIAL_LAND_USE_CHANGE_VERIFICATION_REQUIRED" if source == "detected_change" else "REGISTERED"
    rec = {"id": str(uuid.uuid4()), "name": name, "geometry": geometry, "kind": kind, "source": source, "permit_ref": permit_ref, "status": status, "region_id": region_id, "result": None, "created_at": now()}
    insert("industrial_sites", rec)
    audit(actor, "industrial_site_registered", "industrial_site", rec["id"], {"source": source, "kind": kind})
    return rec


def promote_from_change(change_id: str, actor: str) -> dict[str, Any]:
    change = get_one("changes", "id", change_id)
    if not change or change["disturbance_type"] != "CANDIDATE_FOREST_LOSS":
        raise HTTPException(status_code=404, detail="Candidate forest-loss change not found")
    return register(POTENTIAL_LABEL, change["geometry"], "unclassified", "detected_change", None, change["region_id"], actor)


def _ambient_air(lat: float, lon: float) -> dict[str, Any]:
    try:
        r = httpx.get("https://air-quality-api.open-meteo.com/v1/air-quality", params={"latitude": lat, "longitude": lon, "hourly": "pm2_5", "past_days": 7, "forecast_days": 1, "timezone": "UTC"}, timeout=25)
        r.raise_for_status()
        vals = [v for v in r.json()["hourly"]["pm2_5"] if v is not None]
        if not vals:
            return {"evidence_class": "not_available", "reason": "No PM2.5 values returned"}
        return {"evidence_class": "measured", "kind": "ambient modelled PM2.5 (CAMS via Open-Meteo), NOT site emissions", "mean_ug_m3_7d": round(sum(vals) / len(vals), 1), "max_ug_m3_7d": round(max(vals), 1), "attribution": "Air quality data by Open-Meteo.com / Copernicus CAMS"}
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        return {"evidence_class": "not_available", "reason": f"Air-quality request failed: {type(exc).__name__}"}


def assess(site_id: str, actor: str) -> dict[str, Any]:
    site = get_one("industrial_sites", "id", site_id)
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")
    shape = to_shape(site["geometry"])
    c = shape.centroid
    site_m = _equal_area(shape)
    out: dict[str, Any] = {"site_id": site_id, "site_status": site["status"], "classification_note": POTENTIAL_LABEL if site["source"] == "detected_change" else "Registered by a user; not independently verified from imagery", "site_area_hectares": round(geodesic_area_hectares(shape), 2), "sections": {}}
    s = out["sections"]

    losses = [to_shape(r["geometry"]) for r in list_rows("changes", "WHERE disturbance_type = 'CANDIDATE_FOREST_LOSS'", limit=500)]
    loss_rows = []
    if losses:
        union = unary_union(losses)
        for b in BUFFERS_M:
            area_geom = shape if b == 0 else _buffer_wgs(shape, b)
            loss_rows.append({"zone": "inside site" if b == 0 else f"within {b} m", "candidate_loss_hectares": round(geodesic_area_hectares(area_geom.intersection(union)), 3)})
        s["forest_loss"] = {"evidence_class": "computed", "rows": loss_rows, "note": "From stored candidate detections, which are unverified."}
    else:
        s["forest_loss"] = {"evidence_class": "not_available", "reason": "No stored detections"}

    regions = list_rows("regions", limit=500)
    nearest = None
    for rg in regions:
        d = site_m.distance(_equal_area_like(shape, to_shape(rg["geometry"]).boundary))
        if nearest is None or d < nearest[0]:
            nearest = (d, rg["name"])
    s["distance_to_monitored_forest_boundary"] = {"evidence_class": "computed", "metres": round(nearest[0]), "region": nearest[1]} if nearest else {"evidence_class": "not_available", "reason": "No monitored regions"}

    try:
        ctx = osm_context(site["geometry"])
        s["nearby_features_osm"] = {"evidence_class": "measured", **ctx}
    except OsmUnavailable as exc:
        s["nearby_features_osm"] = {"evidence_class": "not_available", "reason": str(exc)}
    try:
        t = stats_for(site["geometry"])
        s["terrain"] = {"evidence_class": "measured", **t}
        s["erosion_sedimentation_screening"] = {"evidence_class": "screening", "share_steeper_than_screen": t.get("share_steeper_than_screen"), "note": "Slope only; not a sediment model"} if t.get("status") == "ok" else {"evidence_class": "not_available", "reason": "No terrain cells"}
    except Exception as exc:  # noqa: BLE001
        s["terrain"] = {"evidence_class": "not_available", "reason": str(exc)}
    s["ambient_air_quality"] = _ambient_air(c.y, c.x)
    s["water_pollution_exposure"] = {"evidence_class": "not_available", "reason": "No discharge, effluent or watershed dataset ingested; only nearest mapped waterway (OSM) is reported"}
    s["carbon"] = {"evidence_class": "not_available", "reason": "No biomass model configured"}
    s["noise_light"] = {"evidence_class": "not_available", "reason": "No supporting data"}

    permits = []
    for a in list_rows("authorizations", limit=1000):
        if a.get("geometry") and to_shape(a["geometry"]).intersects(shape):
            permits.append({"authorization_id": a["id"], "authority": a["authority"], "project": a["project_name"], "status": a["status"], "source_url": a["source_url"]})
    s["permit_and_clearance_records"] = {"evidence_class": "computed", "intersecting_records": permits, "coverage": "local ingested records only", "warning": "No record found is not a finding of illegality; an intersecting record is not proof of authorisation"}
    out["recommended_follow_up"] = ["Verify site existence and extent with high-resolution imagery or a field visit", "Obtain official clearance records from the relevant authority", "Request emission/discharge data from the operator or regulator before assessing pollution"]
    out["generated_at"] = now()
    from app.db.database import update

    update("industrial_sites", "id", site_id, {"result": out})
    audit(actor, "industrial_site_assessed", "industrial_site", site_id, {})
    return out


def _buffer_wgs(shape, metres: float):
    from pyproj import Transformer
    from shapely.ops import transform

    c = shape.centroid
    p = f"+proj=aeqd +lat_0={c.y} +lon_0={c.x} +datum=WGS84"
    fwd, inv = Transformer.from_crs("EPSG:4326", p, always_xy=True), Transformer.from_crs(p, "EPSG:4326", always_xy=True)
    return transform(inv.transform, transform(fwd.transform, shape).buffer(metres))


def _equal_area_like(reference, geom):
    from pyproj import Transformer
    from shapely.ops import transform

    c = reference.centroid
    t = Transformer.from_crs("EPSG:4326", f"+proj=laea +lat_0={c.y} +lon_0={c.x} +datum=WGS84", always_xy=True)
    return transform(t.transform, geom)
