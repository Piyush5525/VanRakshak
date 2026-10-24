"""Road routing through an OSRM-compatible HTTP service, plus station selection.

Routes come from road-network data only. They say nothing about closures, seasonal washouts,
permits or safety, and the response says so. If no engine is configured the result is `unavailable`.
"""
import math
from typing import Any

import time

import httpx

from app.core.config import Settings

WALK_KMH = 4.0            # ordinary walking pace on a path
ROUGH_KMH = 2.5           # pace used when no mapped path exists and the leg is straight across country
WALK_LEG_MIN_M = 60       # a final approach shorter than this is not worth drawing as a separate leg
CAVEAT = "Road-network estimate only; closures, seasonal access, permits and safety are not verified."


def haversine_km(a: list[float], b: list[float]) -> float:
    p = math.pi / 180
    h = math.sin((b[1] - a[1]) * p / 2) ** 2 + math.cos(a[1] * p) * math.cos(b[1] * p) * math.sin((b[0] - a[0]) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def nearest_station(stations: list[dict[str, Any]], destination: list[float]) -> tuple[dict[str, Any], float] | None:
    best = None
    for s in stations:
        loc = s["location"]
        if loc.get("type") != "Point":
            continue
        d = haversine_km(loc["coordinates"], destination)
        if best is None or d < best[1]:
            best = (s, d)
    return best


_DEAD: dict[str, float] = {}   # a provider whose key was refused is skipped for a while instead of failing every request
FOOT_URL = "https://routing.openstreetmap.de/routed-foot"   # keyless OpenStreetMap walking router (FOSSGIS)
DRIVE_URL = "https://router.project-osrm.org"               # keyless OpenStreetMap driving router (OSRM demo)


def _dead(name: str) -> bool:
    return _DEAD.get(name, 0) > time.time()


class Router:
    """Providers are tried in the order of ROUTING_PROVIDER (comma list), e.g. "mapbox,serpapi"."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=30, headers={"User-Agent": "VanaNetra/0.2"})

    @property
    def providers(self) -> list[str]:
        wanted = [p.strip() for p in self.settings.routing_provider.split(",") if p.strip() and p.strip() != "unconfigured"]
        ready = {"mapbox": bool(self.settings.mapbox_api) and not _dead("mapbox"), "serpapi": bool(self.settings.serp_api), "osrm": bool(self.settings.osrm_url) or getattr(self.settings, "osrm_public", False)}
        if not wanted:   # nothing named (or the placeholder "unconfigured"): use whatever has a key, best first
            wanted = ["mapbox", "osrm", "serpapi"]
        return [p for p in wanted if ready.get(p)]

    @property
    def available(self) -> bool:
        return bool(self.providers)

    def status(self) -> dict[str, Any]:
        return {"name": "routing", "configured": self.available, "usable": self.available, "providers": self.providers, "reason": f"Providers in order: {', '.join(self.providers)}" if self.available else "Set ROUTING_PROVIDER (mapbox, serpapi, osrm) and the matching key/URL"}

    # ------------------------------------------------------------ providers
    def _mapbox(self, o, d, alternatives):
        r = self.client.get(f"https://api.mapbox.com/directions/v5/mapbox/driving/{o[0]},{o[1]};{d[0]},{d[1]}", params={"alternatives": str(alternatives).lower(), "geometries": "geojson", "overview": "full", "steps": "true", "access_token": self.settings.mapbox_api})
        data = r.json()
        if data.get("code") != "Ok":
            if r.status_code in (401, 403):
                _DEAD["mapbox"] = time.time() + 600   # the key is refused: stop asking for ten minutes
            return {"status": "no_route", "reason": data.get("message") or data.get("code")}
        routes = []
        for x in data["routes"]:
            steps = [{"instruction": st["maneuver"].get("instruction"), "distance_m": round(st["distance"]), "location": st["maneuver"]["location"], "type": st["maneuver"].get("type"), "modifier": st["maneuver"].get("modifier")} for leg in x.get("legs", []) for st in leg.get("steps", [])]
            routes.append({"distance_km": round(x["distance"] / 1000, 2), "duration_min": round(x["duration"] / 60, 1), "geometry": x["geometry"], "steps": steps})
        return {"status": "ok", "primary": routes[0], "alternatives": routes[1:], "waypoint_snap_distance_m": [w.get("distance") for w in data.get("waypoints", [])], "provider": "mapbox"}

    def _osrm(self, o, d, alternatives):
        r = self.client.get(f"{(self.settings.osrm_url or DRIVE_URL).rstrip('/')}/route/v1/driving/{o[0]},{o[1]};{d[0]},{d[1]}", params={"alternatives": "true" if alternatives else "false", "overview": "full", "geometries": "geojson"})
        data = r.json()
        if data.get("code") != "Ok":
            return {"status": "no_route", "reason": data.get("message") or data.get("code")}
        routes = [{"distance_km": round(x["distance"] / 1000, 2), "duration_min": round(x["duration"] / 60, 1), "geometry": x["geometry"]} for x in data["routes"]]
        return {"status": "ok", "primary": routes[0], "alternatives": routes[1:], "waypoint_snap_distance_m": [w.get("distance") for w in data.get("waypoints", [])], "provider": "osrm"}

    def _serpapi(self, o, d, alternatives):
        r = self.client.get("https://serpapi.com/search.json", params={"engine": "google_maps_directions", "start_coords": f"{o[1]},{o[0]}", "end_coords": f"{d[1]},{d[0]}", "travel_mode": "0", "api_key": self.settings.serp_api})
        data = r.json()
        if "error" in data:
            return {"status": "no_route", "reason": str(data["error"])[:200]}
        driving = [x for x in data.get("directions", []) if x.get("travel_mode") == "Driving"]
        if not driving:
            return {"status": "no_route", "reason": "No driving route returned"}
        routes = [{"distance_km": round(x["distance"] / 1000, 2), "duration_min": round(x["duration"] / 60, 1), "via": x.get("via"), "geometry": None} for x in driving]
        return {"status": "ok", "primary": routes[0], "alternatives": routes[1:], "provider": "serpapi (Google Maps)", "note": "No route geometry from this provider; distance/duration only."}

    # ------------------------------------------------------------ the last stretch on foot
    def _walk_mapbox(self, o: list[float], d: list[float]) -> dict[str, Any] | None:
        """A walking route (paths and tracks, not only roads): Mapbox when its key works, else the OpenStreetMap foot router."""
        use_mapbox = bool(self.settings.mapbox_api) and not _dead("mapbox")
        if not use_mapbox and not getattr(self.settings, "osrm_public", False):
            return None
        url = f"https://api.mapbox.com/directions/v5/mapbox/walking/{o[0]},{o[1]};{d[0]},{d[1]}" if use_mapbox else f"{FOOT_URL}/route/v1/foot/{o[0]},{o[1]};{d[0]},{d[1]}"
        params = {"geometries": "geojson", "overview": "full", "steps": "true"} | ({"access_token": self.settings.mapbox_api} if use_mapbox else {})
        try:
            r = self.client.get(url, params=params)
            data = r.json()
        except (httpx.HTTPError, ValueError):
            return None
        if use_mapbox and r.status_code in (401, 403):
            _DEAD["mapbox"] = time.time() + 600
            return self._walk_mapbox(o, d)
        if data.get("code") != "Ok" or not data.get("routes"):
            return None
        x = data["routes"][0]
        steps = [{"instruction": (st.get("maneuver") or {}).get("instruction"), "distance_m": round(st["distance"]), "location": st["maneuver"]["location"]} for leg in x.get("legs", []) for st in leg.get("steps", []) if st.get("maneuver")]
        return {"mode": "walk", "distance_km": round(x["distance"] / 1000, 2), "duration_min": round(x["duration"] / 60, 1), "geometry": x["geometry"], "steps": steps, "source": "mapped walking path", "estimated": False}

    def _walk_leg(self, start: list[float], dest: list[float]) -> dict[str, Any]:
        """The part of the journey the vehicle cannot do. Uses a mapped footpath when there is one; otherwise a straight line across
        country, clearly marked as an estimate (slower pace, 30% longer than the straight distance)."""
        straight = haversine_km(start, dest)
        mapped = self._walk_mapbox(start, dest)
        # a mapped walk is only believable if it actually reaches the place and is not shorter than the straight line
        if mapped and straight * 0.9 <= mapped["distance_km"] <= max(straight * 3.0, 0.3) and haversine_km(mapped["geometry"]["coordinates"][-1], dest) * 1000 <= 80:
            return mapped
        km = round(straight * 1.3, 2)
        return {"mode": "walk", "distance_km": km, "duration_min": round(km / ROUGH_KMH * 60, 1), "geometry": {"type": "LineString", "coordinates": [start, dest]}, "steps": [], "source": "straight line across country (no mapped path)", "estimated": True}

    def _add_last_mile(self, out: dict[str, Any], destination: list[float]) -> None:
        """Split every road route into 'drive to the nearest road' + 'walk the rest', so the screen can show both."""
        for route in [out["primary"], *out.get("alternatives", [])]:
            geom = route.get("geometry")
            drive = {"mode": "drive", "distance_km": route["distance_km"], "duration_min": route["duration_min"], "geometry": geom, "source": out.get("provider")}
            segs = [drive]
            if geom and haversine_km(geom["coordinates"][-1], destination) * 1000 > WALK_LEG_MIN_M:
                leg = self._walk_leg(geom["coordinates"][-1], destination)
                if leg["distance_km"] * 1000 >= WALK_LEG_MIN_M:
                    segs.append(leg)
            route["segments"] = segs
            route["drive_km"], route["walk_km"] = drive["distance_km"], round(sum(s["distance_km"] for s in segs[1:]), 2)
            route["drive_min"], route["walk_min"] = drive["duration_min"], round(sum(s["duration_min"] for s in segs[1:]), 1)
            if len(segs) > 1:
                route["distance_km"] = round(drive["distance_km"] + route["walk_km"], 2)
                route["duration_min"] = round(drive["duration_min"] + route["walk_min"], 1)
                route["geometry"] = {"type": "LineString", "coordinates": geom["coordinates"] + segs[1]["geometry"]["coordinates"][1:]}
                route["steps"] = (route.get("steps") or []) + [{"instruction": "Leave the vehicle at the end of the road and continue on foot", "distance_m": 0, "location": geom["coordinates"][-1]}] + (segs[1].get("steps") or [])

    def _walk_only(self, origin: list[float], destination: list[float]) -> dict[str, Any] | None:
        """No road route at all: if the place is close enough to walk, give a walking route instead of nothing."""
        if haversine_km(origin, destination) > 12:
            return None
        w = self._walk_leg(origin, destination)
        route = {"distance_km": w["distance_km"], "duration_min": w["duration_min"], "geometry": w["geometry"], "steps": w.get("steps") or [], "segments": [w], "drive_km": 0.0, "walk_km": w["distance_km"], "drive_min": 0.0, "walk_min": w["duration_min"]}
        return {"status": "ok", "primary": route, "alternatives": [], "provider": "walking", "note": "No road route was found; this is a walking route.", "caveat": "Walking estimate only; terrain, wildlife, weather and permits are not verified."}

    def route(self, origin: list[float], destination: list[float], alternatives: bool = True) -> dict[str, Any]:
        if not self.available:
            return {"status": "unavailable", "reason": "No routing provider configured", "alternatives": []}
        errors = []
        for name in self.providers:
            try:
                out = getattr(self, f"_{name}")(origin, destination, alternatives)
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(f"{name}: {type(exc).__name__}")
                continue
            if out["status"] == "ok":
                out["caveat"] = CAVEAT
                if out["primary"].get("geometry"):
                    self._add_last_mile(out, destination)
                else:
                    for rt in [out["primary"], *out.get("alternatives", [])]:
                        rt["segments"] = [{"mode": "drive", "distance_km": rt["distance_km"], "duration_min": rt["duration_min"], "geometry": None, "source": out.get("provider")}]
                        rt["drive_km"], rt["walk_km"], rt["drive_min"], rt["walk_min"] = rt["distance_km"], 0.0, rt["duration_min"], 0.0
                if errors:
                    out["fallback_note"] = "Earlier providers failed: " + "; ".join(errors)
                snaps = [x for x in out.get("waypoint_snap_distance_m", []) if x]
                if any(x > 500 for x in snaps):
                    out["warning"] = "A waypoint is more than 500 m from the nearest road; the final approach is off-network (on foot or unmapped track)."
                return out
            errors.append(f"{name}: {out.get('reason')}")
        walk = self._walk_only(origin, destination)
        if walk:
            walk["fallback_note"] = "Road routing failed: " + "; ".join(errors)
            return walk
        return {"status": "error", "reason": "All routing providers failed", "details": errors, "alternatives": []}

    def patrol(self, origin: list[float], stops: list[list[float]]) -> dict[str, Any]:
        """Greedy nearest-neighbour visiting order, then per-leg routing (providers have no common trip API)."""
        if not self.available:
            return {"status": "unavailable", "reason": "No routing provider configured"}
        remaining, current, order, legs = list(range(len(stops))), origin, [], []
        while remaining:
            nxt = min(remaining, key=lambda i: haversine_km(current, stops[i]))
            leg = self.route(current, stops[nxt], alternatives=False)
            if leg["status"] != "ok":
                return {"status": "error", "reason": f"Leg to stop {nxt} failed", "details": leg}
            order.append(nxt); legs.append({"to_stop": nxt, **leg["primary"], "provider": leg["provider"]}); current = stops[nxt]; remaining.remove(nxt)
        return {"status": "ok", "visit_order": order, "legs": legs, "distance_km": round(sum(l["distance_km"] for l in legs), 2), "duration_min": round(sum(l["duration_min"] for l in legs), 1), "method": "greedy nearest-neighbour ordering; not an optimal tour", "caveat": CAVEAT}
