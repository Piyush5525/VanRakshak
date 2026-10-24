"""Road routing through an OSRM-compatible HTTP service, plus station selection.

Routes come from road-network data only. They say nothing about closures, seasonal washouts,
permits or safety, and the response says so. If no engine is configured the result is `unavailable`.
"""
import math
from typing import Any

import httpx

from app.core.config import Settings

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


class Router:
    """Providers are tried in the order of ROUTING_PROVIDER (comma list), e.g. "mapbox,serpapi"."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=30, headers={"User-Agent": "VanaNetra/0.2"})

    @property
    def providers(self) -> list[str]:
        wanted = [p.strip() for p in self.settings.routing_provider.split(",") if p.strip()]
        ready = {"mapbox": bool(self.settings.mapbox_api), "serpapi": bool(self.settings.serp_api), "osrm": bool(self.settings.osrm_url)}
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
            return {"status": "no_route", "reason": data.get("message") or data.get("code")}
        routes = []
        for x in data["routes"]:
            steps = [{"instruction": st["maneuver"].get("instruction"), "distance_m": round(st["distance"]), "location": st["maneuver"]["location"], "type": st["maneuver"].get("type"), "modifier": st["maneuver"].get("modifier")} for leg in x.get("legs", []) for st in leg.get("steps", [])]
            routes.append({"distance_km": round(x["distance"] / 1000, 2), "duration_min": round(x["duration"] / 60, 1), "geometry": x["geometry"], "steps": steps})
        return {"status": "ok", "primary": routes[0], "alternatives": routes[1:], "waypoint_snap_distance_m": [w.get("distance") for w in data.get("waypoints", [])], "provider": "mapbox"}

    def _osrm(self, o, d, alternatives):
        r = self.client.get(f"{self.settings.osrm_url.rstrip('/')}/route/v1/driving/{o[0]},{o[1]};{d[0]},{d[1]}", params={"alternatives": "true" if alternatives else "false", "overview": "full", "geometries": "geojson"})
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
                if errors:
                    out["fallback_note"] = "Earlier providers failed: " + "; ".join(errors)
                snaps = [x for x in out.get("waypoint_snap_distance_m", []) if x]
                if any(x > 500 for x in snaps):
                    out["warning"] = "A waypoint is more than 500 m from the nearest road; the final approach is off-network (on foot or unmapped track)."
                return out
            errors.append(f"{name}: {out.get('reason')}")
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
