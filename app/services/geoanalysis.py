"""Spatial metrics (fragmentation) and authorization-record matching.

Matching states are workflow states for human review, never legal verdicts.
"""
from datetime import datetime, timezone
from typing import Any

from pyproj import Transformer
from shapely.ops import transform as shp_transform

from app.core.geometry import geodesic_area_hectares, to_shape


def _equal_area(geom):
    c = geom.centroid
    t = Transformer.from_crs("EPSG:4326", f"+proj=laea +lat_0={c.y} +lon_0={c.x} +datum=WGS84", always_xy=True)
    return shp_transform(t.transform, geom)


def fragmentation(forest_geojson: dict[str, Any], loss_geojson: dict[str, Any]) -> dict[str, Any]:
    forest, loss = to_shape(forest_geojson), to_shape(loss_geojson)
    remaining = forest.difference(loss)
    f_m, r_m = _equal_area(forest), _equal_area(remaining)
    after = [p for p in getattr(r_m, "geoms", [r_m]) if not p.is_empty]
    areas_after = sorted((p.area / 1e4 for p in after), reverse=True)
    return {
        "initial_forest_hectares": round(geodesic_area_hectares(forest), 3),
        "loss_within_forest_hectares": round(geodesic_area_hectares(forest.intersection(loss)), 3),
        "loss_outside_forest_hectares": round(geodesic_area_hectares(loss.difference(forest)), 3),
        "remaining_forest_hectares": round(geodesic_area_hectares(remaining), 3),
        "patch_count_before": len(getattr(f_m, "geoms", [f_m])),
        "patch_count_after": len(after),
        "largest_patch_after_hectares": round(areas_after[0], 3) if areas_after else 0.0,
        "edge_length_before_m": round(f_m.length, 1),
        "edge_length_after_m": round(r_m.length, 1),
        "edge_created_m": round(max(r_m.length - f_m.length, 0.0), 1),
        "method": "shapely set difference in a local Lambert azimuthal equal-area projection",
        "assumptions": ["The region polygon is treated as forest extent; replace with a verified forest mask when available"],
    }


ISSUED = {"issued", "approved", "granted", "conditional_approval"}
NOT_VALID = {"rejected", "cancelled", "revoked", "expired"}
PRE_DECISION = {"application", "pending", "recommended", "under_review"}


def _parse(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def assess_candidate(change_geom: dict[str, Any], observed_at: datetime, auth: dict[str, Any]) -> dict[str, Any]:
    status = str(auth.get("status", "")).lower()
    obs = observed_at if observed_at.tzinfo else observed_at.replace(tzinfo=timezone.utc)

    overlap = None
    if auth.get("geometry"):
        c, a = to_shape(change_geom), to_shape(auth["geometry"])
        area = geodesic_area_hectares(c)
        overlap = round(geodesic_area_hectares(c.intersection(a)) / area, 4) if area > 0 else 0.0

    dates = auth.get("dates") or {}
    start, end = _parse(dates.get("valid_from") or dates.get("issued")), _parse(dates.get("valid_to"))
    temporal = "unknown"
    if start or end:
        temporal = "within_validity" if (not start or start <= obs) and (not end or obs <= end) else "outside_validity"

    if overlap is None:
        state = "AUTHORIZATION_STATUS_UNCLEAR"
    elif overlap == 0:
        state = "INSUFFICIENT_EVIDENCE"
    elif status in NOT_VALID or temporal == "outside_validity":
        state = "RECORD_EXPIRED_OR_REVOKED_CANDIDATE"
    elif status in PRE_DECISION or status not in ISSUED:
        state = "AUTHORIZATION_STATUS_UNCLEAR"
    elif overlap < 0.95:
        state = "POSSIBLE_SCOPE_OR_CONDITION_MISMATCH"
    elif temporal == "within_validity":
        state = "MATCH_REQUIRES_REVIEW"
    else:
        state = "POTENTIAL_MATCH"
    return {"authorization_id": auth["id"], "state": state, "overlap_fraction": overlap, "temporal": temporal, "record_status": status}
