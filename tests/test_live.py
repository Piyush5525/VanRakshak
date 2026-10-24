import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app import live_routes
from app.core.config import get_settings
from app.main import app
from app.services import boundary

client = TestClient(app)
ADMIN = {"X-Role": "admin"}
settings = get_settings()
NOW = datetime.now(timezone.utc)


def row(lat, lon, hours_ago=2, frp=10.0):
    return {"lat": lat, "lon": lon, "observed_at": NOW - timedelta(hours=hours_ago), "confidence": 0.9, "raw_confidence": "h", "satellite": "N", "instrument": "VIIRS", "frp_mw": frp, "brightness_k": 330.0, "daynight": "D", "scan_km": 0.4}


@pytest.fixture(autouse=True)
def clear_cache():
    live_routes._cache.clear()
    live_routes._air_cache.clear()
    yield


def test_live_fires_filters_to_india_caches_and_reports_what_it_hid(monkeypatch):
    calls = []
    rows = [row(22.0, 78.0), row(23.0, 90.3), row(11.6, 76.0, frp=None)]  # middle point lies in Bangladesh
    monkeypatch.setattr(live_routes, "fetch_firms", lambda s, bbox, src, days, client=None: calls.append((src, days)) or rows)
    monkeypatch.setattr(boundary, "contains", lambda lat, lon: lon < 85)
    d = client.get("/api/v1/live/fires?days=1", headers={"X-Role": "read_only"}).json()
    assert d["count"] == 2 and d["total_in_bbox"] == 3 and "1 outside it were hidden" in d["bbox_note"] and d["cached"] is False
    assert d["fires"][0][:2] == [22.0, 78.0] and d["fields"][4] == "frp_mw" and "not confirmed wildfires" in d["attribution"]
    again = client.get("/api/v1/live/fires?days=1", headers=ADMIN).json()
    assert again["cached"] is True and len(calls) == 1  # FIRMS has a quota; repeated views must not re-query
    assert client.get("/api/v1/live/fires?days=1&force=true", headers=ADMIN).json()["cached"] is False and len(calls) == 2
    assert client.get("/api/v1/live/fires?days=1&india_only=false", headers=ADMIN).json()["count"] == 3


def test_live_fires_validation_and_honest_failure(monkeypatch):
    assert client.get("/api/v1/live/fires?days=9", headers=ADMIN).status_code == 422
    assert client.get("/api/v1/live/fires?source=FAKE", headers=ADMIN).status_code == 422
    r = client.get("/api/v1/live/fires", headers=ADMIN)  # no FIRMS key in tests
    assert r.status_code == 503 and "FIRMS_API_KEY" in r.json()["detail"]
    # a failed refresh serves the last good copy and says so
    monkeypatch.setattr(live_routes, "fetch_firms", lambda *a, **k: [row(22.0, 78.0)])
    monkeypatch.setattr(boundary, "contains", lambda lat, lon: True)
    client.get("/api/v1/live/fires?days=2", headers=ADMIN)
    live_routes._cache[("VIIRS_SNPP_NRT", 2, True)]["at"] -= 10_000

    def boom(*a, **k):
        raise live_routes.FirmsUnavailable("quota exceeded")

    monkeypatch.setattr(live_routes, "fetch_firms", boom)
    d = client.get("/api/v1/live/fires?days=2", headers=ADMIN).json()
    assert d["stale"] is True and "quota exceeded" in d["warning"] and d["count"] == 1


def test_live_air_is_labelled_as_ambient_not_emissions(monkeypatch):
    hours = [(NOW - timedelta(hours=h)).strftime("%Y-%m-%dT%H:00") for h in range(24, -1, -1)]
    payload = {"hourly": {"time": hours, "pm2_5": [10.0 + i for i in range(len(hours))], "pm10": [20.0] * len(hours), "nitrogen_dioxide": [5.0] * len(hours), "sulphur_dioxide": [None] * len(hours)}}
    monkeypatch.setattr(live_routes.httpx, "get", lambda *a, **k: httpx.Response(200, json=payload, request=httpx.Request("GET", "https://x")))
    a = client.get("/api/v1/live/air?lat=11.6&lon=76.0", headers=ADMIN).json()
    assert "NOT facility emissions" in a["kind"] and a["pm2_5"]["max_24h"] == 34.0 and a["pm2_5"]["unit"] == "ug/m3" and a["so2"]["max_24h"] is None
    assert client.get("/api/v1/live/air?lat=95&lon=76", headers=ADMIN).status_code == 422
    monkeypatch.setattr(live_routes.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("x")))
    assert client.get("/api/v1/live/air?lat=12.6&lon=77.0", headers=ADMIN).status_code == 503


def test_mark_incident_add_site_and_report_carries_user_entered_data_with_labels():
    inc = client.post("/api/v1/incidents", headers=ADMIN, json={"title": "Smoke from a plant near river", "geometry": {"type": "Point", "coordinates": [76.5, 11.5]}, "evidence": {"claim_type": "human_report", "kind": "pollution", "note": "Seen from the road", "source": "Marked on the live map"}}).json()
    body = {"name": "Test Works", "kind": "factory", "note": "north bank", "emissions": [{"pollutant": "SO2", "value": 120, "unit": "mg/Nm3", "source": "Consent order 2024", "as_of": "2024-06"}]}
    assert client.post(f"/api/v1/incidents/{inc['id']}/site", headers={"X-Role": "read_only"}, json=body).status_code == 403
    assert client.post(f"/api/v1/incidents/{inc['id']}/site", headers={"X-Role": "officer"}, json=body).status_code == 403
    site = client.post(f"/api/v1/incidents/{inc['id']}/site", headers=ADMIN, json=body).json()
    assert site["claim_type"] == "user_supplied_unverified" and site["entered_by_role"] == "admin"
    bad = {**body, "emissions": [{"pollutant": "SO2", "value": 1, "unit": "x", "source": ""}]}
    assert client.post(f"/api/v1/incidents/{inc['id']}/site", headers=ADMIN, json=bad).status_code == 422  # a figure without a source is refused
    assert client.post(f"/api/v1/incidents/{inc['id']}/site", headers=ADMIN, json={**body, "surprise": 1}).status_code == 422
    rep = client.post(f"/api/v1/incidents/{inc['id']}/reports", headers=ADMIN).json()
    pkg = client.get(f"/api/v1/reports/{rep['id']}/download", headers=ADMIN).json()
    ua = {u["type"]: u for u in pkg["evidence"]["user_assumptions"]}
    assert "not verified" in ua["incident_report_by_person"]["label"] and ua["industrial_site"]["name"] == "Test Works"
    assert ua["reported_emission_figure"]["value"] == "120.0 mg/Nm3" and "not measured or verified" in ua["reported_emission_figure"]["label"]
    assert "does not show that this site caused anything" in ua["industrial_site"]["label"]
    assert client.delete(f"/api/v1/incidents/{inc['id']}/site", headers={"X-Role": "officer"}).status_code == 403
    assert client.delete(f"/api/v1/incidents/{inc['id']}/site", headers=ADMIN).json()["removed"] is True
    assert "site" not in client.get(f"/api/v1/incidents/{inc['id']}", headers=ADMIN).json()["evidence"]
    assert client.post("/api/v1/incidents/nope/site", headers=ADMIN, json=body).status_code == 404


def test_pages_are_served_and_boundary_helper_contains_points(tmp_path, monkeypatch):
    assert "Open live map" in client.get("/").text and "VanRakshak" in client.get("/live").text
    assert client.get("/assets/public/logo.svg").status_code == 200
    f = tmp_path / "india.geojson"
    f.write_text(json.dumps({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[70, 8], [90, 8], [90, 30], [70, 30], [70, 8]]]}, "properties": {}}))
    monkeypatch.setattr(boundary, "_path", lambda: f)
    boundary._state.clear()
    assert boundary.contains(20, 80) is True and boundary.contains(20, 95) is False
    boundary._state.clear()
