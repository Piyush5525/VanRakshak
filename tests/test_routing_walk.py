from types import SimpleNamespace

import httpx

from app.services import geoinfo
from app.services.routing import Router, haversine_km

OFFICE = [76.0, 11.60]
SITE = [76.02, 11.62]            # a place in the forest
ROAD_END = [76.0185, 11.6185]    # where the road stops, a few hundred metres short of it


def settings(**kw):
    base = dict(routing_provider="unconfigured", mapbox_api="pk.test", serp_api=None, osrm_url=None)
    return SimpleNamespace(**{**base, **kw})


def response(url, body):
    return httpx.Response(200, json=body, request=httpx.Request("GET", str(url)))


class Fake:
    """A stand-in for Mapbox: a driving route that stops at the road end, and a configurable walking answer."""

    def __init__(self, walking="degenerate", driving_ok=True):
        self.walking, self.driving_ok, self.calls = walking, driving_ok, []

    def get(self, url, params=None, **kw):
        self.calls.append(str(url))
        if "/driving/" in str(url):
            if not self.driving_ok:
                return response(url, {"code": "NoRoute", "message": "No route found"})
            line = {"type": "LineString", "coordinates": [OFFICE, [76.01, 11.61], ROAD_END]}
            return response(url, {"code": "Ok", "routes": [{"distance": 3200, "duration": 600, "geometry": line, "legs": [{"steps": []}]}], "waypoints": [{"distance": 3}, {"distance": 250}]})
        if self.walking == "degenerate":   # a walking router snaps the site onto the same road: zero distance
            return response(url, {"code": "Ok", "routes": [{"distance": 0, "duration": 0, "geometry": {"type": "LineString", "coordinates": [ROAD_END, ROAD_END]}, "legs": [{"steps": []}]}]})
        if self.walking == "path":          # a real mapped footpath that reaches the site
            mid = [(ROAD_END[0] + SITE[0]) / 2 + 0.0004, (ROAD_END[1] + SITE[1]) / 2]
            km = (haversine_km(ROAD_END, mid) + haversine_km(mid, SITE))
            return response(url, {"code": "Ok", "routes": [{"distance": km * 1000, "duration": km / 4 * 3600, "geometry": {"type": "LineString", "coordinates": [ROAD_END, mid, SITE]}, "legs": [{"steps": [{"maneuver": {"instruction": "Follow the forest track", "location": mid}, "distance": km * 1000}]}]}]})
        return response(url, {"code": "NoRoute"})


def test_a_site_off_the_road_gets_a_drive_leg_and_a_walking_leg():
    r = Router(settings(), client=Fake("degenerate"))
    assert r.providers == ["mapbox"]                      # "unconfigured" with a key present means: use what is available
    out = r.route(OFFICE, SITE, alternatives=False)
    p = out["primary"]
    assert [s["mode"] for s in p["segments"]] == ["drive", "walk"]
    walk = p["segments"][1]
    assert walk["estimated"] is True and "no mapped path" in walk["source"] and 0.2 < walk["distance_km"] < 0.5   # a degenerate walking answer is not believed
    assert p["drive_km"] == 3.2 and p["walk_km"] == walk["distance_km"] and p["distance_km"] == round(3.2 + walk["distance_km"], 2)
    assert p["duration_min"] == round(10 + walk["duration_min"], 1) and p["geometry"]["coordinates"][-1] == SITE


def test_a_mapped_footpath_is_used_when_it_really_reaches_the_site():
    out = Router(settings(), client=Fake("path")).route(OFFICE, SITE, alternatives=False)
    walk = out["primary"]["segments"][1]
    assert walk["estimated"] is False and walk["source"] == "mapped walking path" and walk["steps"][0]["instruction"] == "Follow the forest track"


def test_no_road_at_all_falls_back_to_a_walking_route_when_the_place_is_near():
    out = Router(settings(), client=Fake("degenerate", driving_ok=False)).route(OFFICE, SITE, alternatives=False)
    assert out["status"] == "ok" and out["provider"] == "walking" and out["primary"]["drive_km"] == 0.0 and out["primary"]["segments"][0]["mode"] == "walk"
    assert "walking route" in out["note"]
    far = Router(settings(), client=Fake("degenerate", driving_ok=False)).route(OFFICE, [78.0, 13.0], alternatives=False)
    assert far["status"] == "error"                       # too far to walk: say so instead of inventing a route


def test_unconfigured_without_keys_stays_unavailable_and_journeys_are_worded_for_listening():
    assert not Router(settings(mapbox_api=None)).available
    assert geoinfo.say_journey({"drive_min": 35, "walk_km": 0.42}) == "about 35 minutes by road, then about 420 metres on foot"
    assert geoinfo.say_journey({"drive_min": 35, "walk_km": 1.6}) == "about 35 minutes by road, then about 1.6 kilometres on foot"
    assert geoinfo.say_journey({"duration_min": 35}) == "about 35 minutes by road"
    assert geoinfo.say_journey({"drive_km": 0.0, "walk_min": 20, "walk_km": 0, "duration_min": 20}) == "about 20 minutes on foot"
