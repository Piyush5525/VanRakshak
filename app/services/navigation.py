"""Hazard-aware route evaluation and navigation-session tracking.

Routes come from road-network providers and know nothing about closures, floods or access rules.
This module overlays *recorded* hazards (with source, time, verification) and explains every penalty.
It never labels a route "safe": the best it can say is "no recorded hazard on this route".
"""
import math
import uuid
from datetime import datetime, timezone
from typing import Any

from pyproj import Transformer
from shapely.geometry import LineString, Point, shape
from shapely.ops import transform as shp_transform

from app.db.database import list_rows, now

HAZARD_BUFFER_M = 40.0
STALE_HOURS = 24
UNVERIFIED_PENALTY_MIN = 20.0
OFF_ROUTE_M = 50.0
BLOCKING_KINDS = {"closure", "flood", "washout", "restricted_access"}


def _aeqd(lon: float, lat: float):
    t = Transformer.from_crs("EPSG:4326", f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84", always_xy=True)
    return t.transform


def active_hazards(at: datetime | None = None) -> list[dict[str, Any]]:
    at = at or datetime.now(timezone.utc)
    out = []
    for h in list_rows("hazards", limit=2000):
        if h["expires_at"] and datetime.fromisoformat(h["expires_at"]) < at:
            continue
        age_h = (at - datetime.fromisoformat(h["reported_at"])).total_seconds() / 3600
        out.append({**h, "age_hours": round(age_h, 1), "stale": age_h > STALE_HOURS})
    return out


def evaluate_routes(routes: list[dict[str, Any]], hazards: list[dict[str, Any]], weather_flags: list[str] | None = None) -> dict[str, Any]:
    """Rank routes; each gets status, penalty and plain-language reasons."""
    evaluated = []
    for i, r in enumerate(routes):
        reasons, penalty, blocked = [], 0.0, False
        geom = r.get("geometry")
        if not geom:
            reasons.append({"code": "no_geometry", "text": "This provider returned no route geometry, so hazards cannot be checked along it."})
            penalty += UNVERIFIED_PENALTY_MIN
        else:
            line = LineString(geom["coordinates"])
            to_m = _aeqd(*line.centroid.coords[0])
            line_m = shp_transform(to_m, line)
            for h in hazards:
                hz = shp_transform(to_m, shape(h["geometry"]))
                if line_m.distance(hz) > HAZARD_BUFFER_M:
                    continue
                when = f"reported {h['reported_at'][:16].replace('T', ' ')} UTC ({h['age_hours']} h ago) by {h['source']}"
                state = "verified" if h["verified"] else "unverified"
                stale = " This report may be out of date." if h["stale"] else ""
                if h["kind"] in BLOCKING_KINDS and h["verified"] and not h["stale"]:
                    blocked = True
                    reasons.append({"code": h["kind"], "hazard_id": h["id"], "text": f"Blocked: {state} {h['kind'].replace('_', ' ')} within {int(HAZARD_BUFFER_M)} m of this route, {when}.{stale}"})
                else:
                    penalty += UNVERIFIED_PENALTY_MIN
                    reasons.append({"code": h["kind"], "hazard_id": h["id"], "text": f"Penalised (+{int(UNVERIFIED_PENALTY_MIN)} min): {state} {h['kind'].replace('_', ' ')} near this route, {when}.{stale} Verify before use."})
        if weather_flags:
            reasons.extend({"code": "weather_advisory", "text": f"Advisory (not a verified hazard): {f}"} for f in weather_flags)
        evaluated.append({"index": i, "distance_km": r["distance_km"], "duration_min": r["duration_min"], "adjusted_duration_min": None if blocked else round(r["duration_min"] + penalty, 1), "status": "blocked" if blocked else "penalised" if penalty else "no_recorded_hazard", "reasons": reasons, "geometry": geom, "steps": r.get("steps"), "via": r.get("via")})
    usable = sorted([e for e in evaluated if e["status"] != "blocked"], key=lambda e: e["adjusted_duration_min"])
    recommended = usable[0]["index"] if usable else None
    for e in evaluated:
        if e["index"] == recommended:
            e["recommendation"] = "recommended" if e["status"] == "no_recorded_hazard" else "recommended_with_caution"
        elif e["status"] == "blocked":
            e["recommendation"] = "not_recommended_blocked"
        else:
            e["recommendation"] = "alternative"
            e["reasons"].append({"code": "slower", "text": f"Slower or more penalised than the recommended route by {round(e['adjusted_duration_min'] - usable[0]['adjusted_duration_min'], 1)} min."}) if usable and e["adjusted_duration_min"] is not None else None
    return {"recommended_index": recommended, "routes": evaluated, "message": None if recommended is not None else "No verified suitable route: all candidate routes are blocked by recorded hazards. Do not proceed without manual verification.", "disclaimer": "'No recorded hazard' means no hazard report exists in this system, not that the route is safe or open. Forest tracks may be missing from the routing data.", "hazards_considered": len(hazards)}


def progress_on_route(geometry: dict[str, Any], lon: float, lat: float) -> dict[str, Any]:
    line = LineString(geometry["coordinates"])
    to_m = _aeqd(lon, lat)
    line_m = shp_transform(to_m, line)
    me = Point(0, 0)
    off = me.distance(line_m)
    along = line_m.project(me)
    total = line_m.length
    return {"off_route_m": round(off, 1), "on_route": off <= OFF_ROUTE_M, "distance_remaining_m": round(max(total - along, 0), 1), "progress": round(min(max(along / total, 0), 1), 3) if total else 1.0, "route_length_m": round(total, 1)}


def bearing_deg(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def next_step(steps: list[dict[str, Any]] | None, geometry: dict[str, Any], lon: float, lat: float) -> dict[str, Any] | None:
    """First maneuver located ahead of the current position along the route."""
    if not steps:
        return None
    to_m = _aeqd(lon, lat)
    line_m = shp_transform(to_m, LineString(geometry["coordinates"]))
    here = line_m.project(Point(0, 0))
    ahead = []
    for st in steps:
        pt = Point(*to_m(*st["location"]))
        along = line_m.project(pt)
        if along > here + 5 and st.get("type") != "depart":
            ahead.append((along, pt, st))
    if not ahead:
        return None
    along, pt, st = min(ahead, key=lambda a: a[0])
    return {"instruction": st.get("instruction"), "distance_m": round(along - here), "bearing_deg": round(bearing_deg(lon, lat, *st["location"]), 1), "type": st.get("type"), "modifier": st.get("modifier")}
