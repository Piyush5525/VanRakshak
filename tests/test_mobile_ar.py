"""Phone links for Ranger AR Navigation: signed, expiring, scoped to one incident, and no credentials inside."""
import base64
import json

import pytest
from fastapi.testclient import TestClient

from app import rakshak_routes as rr
from app.db.database import get_one
from app.main import app
from app.services import mobilelink
from app.services.weather import WeatherUnavailable

client = TestClient(app)
ADMIN = {"X-Role": "admin"}
POLY = {"type": "Polygon", "coordinates": [[[81.0, 21.7], [81.004, 21.7], [81.004, 21.704], [81.0, 21.704], [81.0, 21.7]]]}
POINT = {"type": "Point", "coordinates": [81.02, 21.72]}


def incident(geom, approve=True):
    i = client.post("/api/v1/incidents", headers=ADMIN, json={"title": "Test cutting", "geometry": geom, "evidence": {"kind": "cutting"}}).json()
    if approve:
        assert client.post(f"/api/v1/incidents/{i['id']}/review", headers=ADMIN, json={"decision": "approve", "notes": ""}).status_code == 200
    return i


def link(i, **kw):
    return client.post("/api/v1/navigation/mobile-links", headers=ADMIN, json={"incident_id": i["id"], **kw})


def token_of(url):
    return url.rsplit("/", 1)[1]


@pytest.fixture()
def fake_routing(monkeypatch):
    def route(origin, dest, alternatives=True):
        line = {"type": "LineString", "coordinates": [origin, [(origin[0] + dest[0]) / 2, (origin[1] + dest[1]) / 2], dest]}
        return {"status": "ok", "provider": "test", "primary": {"distance_km": 2.0, "duration_min": 6.0, "geometry": line, "steps": [{"instruction": "Head to the incident", "distance_m": 2000, "location": origin, "type": "depart"}]}, "alternatives": []}

    monkeypatch.setattr(rr.router_service, "route", route)
    monkeypatch.setattr(rr, "rainfall_summary", lambda *a, **k: (_ for _ in ()).throw(WeatherUnavailable("offline")))


def test_a_link_needs_a_real_and_approved_incident():
    assert client.post("/api/v1/navigation/mobile-links", headers=ADMIN, json={"incident_id": "nope"}).status_code == 404
    new = incident(POLY, approve=False)
    r = link(new)
    assert r.status_code == 409 and "approve" in r.json()["detail"].lower()
    assert client.post("/api/v1/navigation/mobile-links", headers={"X-Role": "read_only"}, json={"incident_id": new["id"]}).status_code == 403


@pytest.mark.parametrize("geom", [POLY, POINT])
def test_the_link_is_signed_expiring_and_carries_no_credentials(geom):
    i = incident(geom)
    r = link(i, route_index=1)
    assert r.status_code == 200
    d = r.json()
    assert "/mobile/ar/" in d["url"] and d["incident_id"] == i["id"] and d["route_index"] == 1 and d["geometry_type"] == geom["type"]
    tok = token_of(d["url"])
    payload = json.loads(base64.urlsafe_b64decode(tok.split(".")[0] + "=="))
    assert set(payload) == {"v", "i", "r", "e"} and payload["i"] == i["id"]      # only the incident, the route and the expiry
    flat = json.dumps(payload).lower()
    assert "bearer" not in flat and "role" not in flat and "secret" not in flat and "password" not in flat
    info = client.get(f"/api/v1/mobile/ar/{tok}").json()                         # no login needed: the signature is the proof
    assert info["incident"]["geometry"]["type"] == geom["type"] and info["reviewed"] is True and info["route_index"] == 1
    assert d["warning"] and "localhost" in d["warning"]                            # the test client is on localhost


def test_a_bad_or_expired_link_is_refused():
    i = incident(POLY)
    good = token_of(link(i).json()["url"])
    payload, sig = good.split(".")
    for bad in ("garbage", payload + "." + sig[::-1], payload + ".", "x." + sig, good[:-3] + "AAA"):
        assert client.get(f"/api/v1/mobile/ar/{bad}").status_code == 401
    old, _ = mobilelink.make(i["id"], 0, ttl_s=-5)
    r = client.get(f"/api/v1/mobile/ar/{old}")
    assert r.status_code == 410 and "expired" in r.json()["detail"].lower()
    # the signature protects the incident id: a token for another incident cannot be forged by editing the payload
    forged = base64.urlsafe_b64encode(json.dumps({"v": 1, "i": "other", "r": 0, "e": 9999999999}).encode()).decode().rstrip("=") + "." + sig
    assert client.get(f"/api/v1/mobile/ar/{forged}").status_code == 401


def test_the_public_base_address_can_be_set(monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.get_settings(), "mobile_public_base_url", "https://rangers.example.org/")
    base, warn = mobilelink.base_url("http://127.0.0.1:8000/")
    assert base == "https://rangers.example.org" and warn is None


def test_guidance_session_through_a_link_keeps_only_the_latest_position(fake_routing):
    i = incident(POLY)
    tok = token_of(link(i).json()["url"])
    origin = {"type": "Point", "coordinates": [80.99, 21.69]}
    r = client.post(f"/api/v1/mobile/ar/{tok}/route", json={"origin": origin}).json()
    assert r["status"] == "ok" and r["evaluation"]["routes"]
    s = client.post(f"/api/v1/mobile/ar/{tok}/session", json={"origin": origin, "consent_location_use": True})
    assert s.status_code == 200, s.text
    sid = s.json()["session_id"]
    assert client.post(f"/api/v1/mobile/ar/{tok}/session", json={"origin": origin, "consent_location_use": False}).status_code == 422
    p = client.post(f"/api/v1/mobile/ar/{tok}/sessions/{sid}/position", json={"lon": 80.995, "lat": 21.695, "accuracy_m": 12}).json()
    assert "bearing_to_destination_deg" in p and "progress" in p
    far = client.post(f"/api/v1/mobile/ar/{tok}/sessions/{sid}/position", json={"lon": 81.2, "lat": 21.9, "accuracy_m": 80}).json()
    assert far["on_route"] is False and far["warnings"]
    assert get_one("nav_sessions", "id", sid)["result"]["last_position"] is not None
    assert client.post(f"/api/v1/mobile/ar/{tok}/sessions/{sid}/reroute", json={"lon": 81.2, "lat": 21.9}).json()["rerouted"] is True
    # another incident's link cannot touch this session
    other = token_of(link(incident(POINT)).json()["url"])
    assert client.post(f"/api/v1/mobile/ar/{other}/sessions/{sid}/position", json={"lon": 81, "lat": 21.7}).status_code == 403
    # ending works and removes the position, even after the link has expired
    stale, _ = mobilelink.make(i["id"], 0, ttl_s=-5)
    assert client.post(f"/api/v1/mobile/ar/{stale}/sessions/{sid}/end", json={"outcome": "aborted"}).status_code == 200
    assert get_one("nav_sessions", "id", sid)["result"]["last_position"] is None
    assert client.post(f"/api/v1/mobile/ar/{stale}/sessions/{sid}/position", json={"lon": 81, "lat": 21.7}).status_code == 410


def test_the_phone_page_is_served_without_a_login():
    r = client.get("/mobile/ar/anything")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"] and "no-store" in r.headers["cache-control"]
    assert "camera=(self)" in r.headers["permissions-policy"] and "Browser guidance can be inaccurate" in r.text
