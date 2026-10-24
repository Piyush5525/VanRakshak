import io
import json
import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import rakshak_routes
from app.core.config import get_settings
from app.db.database import get_one, insert, list_rows, now
from app.main import app
from app.services import datasources, environment, forestscene, industrial, navigation, projection
from app.services.firevision import FireDetector, TemporalFilter, correlate
from app.services.reports import generic_pdf, to_csv
from app.services.satellite import Grid

client = TestClient(app)
ADMIN = {"X-Role": "admin"}
settings = get_settings()
SQUARE = {"type": "Polygon", "coordinates": [[[76, 11], [76.1, 11], [76.1, 11.1], [76, 11.1], [76, 11]]]}
LOSS = {"type": "Polygon", "coordinates": [[[76.02, 11.02], [76.03, 11.02], [76.03, 11.03], [76.02, 11.03], [76.02, 11.02]]]}
ROUTE = {"type": "LineString", "coordinates": [[76.0, 11.0], [76.01, 11.0], [76.02, 11.0], [76.03, 11.0]]}


def mk_region(name="Rakshak test region"):
    return client.post("/api/v1/regions", headers=ADMIN, json={"name": name, "jurisdiction": "Kerala", "geometry": SQUARE}).json()


def mk_change(region_id, start="2024-02-01", end="2025-02-01", area=1.5, geometry=LOSS, status="CANDIDATES_FOUND"):
    cid = f"chg-{random.random()}"
    insert("changes", {"id": cid, "region_id": region_id, "geometry": geometry, "area_hectares": area, "baseline_date": f"{start}T00:00:00+00:00", "comparison_date": f"{end}T00:00:00+00:00", "disturbance_type": "CANDIDATE_FOREST_LOSS", "score": 0.3, "status": status, "provenance": {"polygon_scores": [{"evidence_level": "optical_radar_agree", "area_hectares": area}]}, "created_at": now()})
    return cid


def mk_incident(status="APPROVED_FOR_INSPECTION", point=(76.03, 11.001), change_id=None):
    iid = f"inc-{random.random()}"
    insert("incidents", {"id": iid, "change_id": change_id, "wildfire_id": None, "status": status, "title": "Rakshak test incident", "geometry": {"type": "Point", "coordinates": list(point)}, "evidence": {}, "created_at": now(), "updated_at": now()})
    return iid


def mk_hazard(kind="flood", geometry=None, verified=True, age_h=1, source="Range office report", expires_h=24):
    hid = f"hz-{random.random()}"
    t = datetime.now(timezone.utc) - timedelta(hours=age_h)
    insert("hazards", {"id": hid, "kind": kind, "geometry": geometry or {"type": "Point", "coordinates": [76.015, 11.0]}, "severity": "critical", "source": source, "reported_by": "officer", "reported_at": t.isoformat(), "expires_at": (t + timedelta(hours=expires_h)).isoformat(), "verified": int(verified), "note": None, "created_at": now()})
    return hid


# ---------------------------------------------------------------- hazards / routing evaluation
def test_route_evaluation_blocked_penalised_and_explained():
    routes = [{"distance_km": 3.3, "duration_min": 10.0, "geometry": ROUTE}, {"distance_km": 5.0, "duration_min": 14.0, "geometry": {"type": "LineString", "coordinates": [[76.0, 11.0], [76.0, 11.05], [76.03, 11.05], [76.03, 11.0]]}}]
    hz = [{"id": "h1", "kind": "flood", "geometry": {"type": "Point", "coordinates": [76.015, 11.0]}, "verified": 1, "age_hours": 2.0, "stale": False, "source": "Range office", "reported_at": "2026-10-24T08:00:00+00:00"}]
    ev = navigation.evaluate_routes(routes, hz)
    assert ev["recommended_index"] == 1 and ev["routes"][0]["status"] == "blocked" and ev["routes"][0]["recommendation"] == "not_recommended_blocked"
    assert "flood" in ev["routes"][0]["reasons"][0]["text"] and "Range office" in ev["routes"][0]["reasons"][0]["text"]
    assert "not that the route is safe" in ev["disclaimer"]
    stale = [{**hz[0], "age_hours": 40.0, "stale": True}]
    ev2 = navigation.evaluate_routes(routes, stale)
    assert ev2["routes"][0]["status"] == "penalised" and "out of date" in ev2["routes"][0]["reasons"][0]["text"]
    unverified = [{**hz[0], "verified": 0}]
    assert navigation.evaluate_routes(routes, unverified)["routes"][0]["status"] == "penalised"
    both = [hz[0], {**hz[0], "id": "h2", "geometry": {"type": "Point", "coordinates": [76.0, 11.05]}}]
    none_ok = navigation.evaluate_routes(routes, both)
    assert none_ok["recommended_index"] is None and "No verified suitable route" in none_ok["message"]
    clear = navigation.evaluate_routes(routes, [])
    assert clear["routes"][0]["status"] == "no_recorded_hazard" and clear["recommended_index"] == 0
    nogeo = navigation.evaluate_routes([{"distance_km": 1, "duration_min": 3, "geometry": None}], [])
    assert nogeo["routes"][0]["status"] == "penalised" and nogeo["routes"][0]["reasons"][0]["code"] == "no_geometry"


def test_hazard_api_roles_verification_and_expiry():
    r = client.post("/api/v1/navigation/hazards", headers={"X-Role": "officer"}, json={"kind": "flood", "geometry": {"type": "Point", "coordinates": [76.5, 11.5]}, "source": "Field call", "verified": True})
    assert r.status_code == 200 and r.json()["verified"] is False  # a field report cannot self-verify
    hid = r.json()["id"]
    assert client.post(f"/api/v1/navigation/hazards/{hid}/verify", headers={"X-Role": "officer"}).status_code == 403
    assert client.post(f"/api/v1/navigation/hazards/{hid}/verify", headers={"X-Role": "reviewer"}).json()["verified"] is True
    assert hid in [h["id"] for h in client.get("/api/v1/navigation/hazards", headers=ADMIN).json()]
    client.post(f"/api/v1/navigation/hazards/{hid}/resolve", headers=ADMIN)
    assert hid not in [h["id"] for h in client.get("/api/v1/navigation/hazards", headers=ADMIN).json()]
    assert client.post("/api/v1/navigation/hazards", headers={"X-Role": "read_only"}, json={"kind": "flood", "geometry": {"type": "Point", "coordinates": [76, 11]}, "source": "x1"}).status_code == 403


def test_progress_and_next_step_use_route_position():
    p = navigation.progress_on_route(ROUTE, 76.01, 11.0)
    assert p["on_route"] and 0.3 < p["progress"] < 0.37 and p["distance_remaining_m"] == pytest.approx(2180, rel=0.03)
    off = navigation.progress_on_route(ROUTE, 76.01, 11.01)
    assert not off["on_route"] and off["off_route_m"] > 1000
    steps = [{"instruction": "Depart", "location": [76.0, 11.0], "type": "depart"}, {"instruction": "Turn left", "location": [76.02, 11.0], "type": "turn", "modifier": "left"}, {"instruction": "Arrive", "location": [76.03, 11.0], "type": "arrive"}]
    n = navigation.next_step(steps, ROUTE, 76.012, 11.0)
    assert n["instruction"] == "Turn left" and 800 < n["distance_m"] < 900
    assert navigation.next_step(steps, ROUTE, 76.025, 11.0)["instruction"] == "Arrive"
    assert navigation.bearing_deg(76, 11, 76.1, 11) == pytest.approx(90, abs=0.5)


def fake_route(origin, dest, alternatives=True):
    return {"status": "ok", "provider": "mapbox", "primary": {"distance_km": 3.3, "duration_min": 10.0, "geometry": ROUTE, "steps": [{"instruction": "Head east", "distance_m": 3300, "location": [76.0, 11.0], "type": "depart"}, {"instruction": "You have arrived", "distance_m": 0, "location": [76.03, 11.0], "type": "arrive"}]}, "alternatives": [], "waypoint_snap_distance_m": [5, 8], "caveat": "x"}


def test_navigation_session_lifecycle_privacy_and_gates(monkeypatch):
    monkeypatch.setattr(rakshak_routes.router_service, "route", fake_route)
    monkeypatch.setattr(rakshak_routes, "rainfall_summary", lambda lat, lon: {"heavy_rain_in_window": True, "last_7_days_mm": 120.0, "anomaly_ratio": 2.0})
    inc = mk_incident()
    body = {"incident_id": inc, "origin": {"type": "Point", "coordinates": [76.0, 11.0]}, "consent_location_use": False}
    assert client.post("/api/v1/navigation/sessions", headers=ADMIN, json=body).status_code == 422  # no consent
    assert client.post("/api/v1/navigation/sessions", headers={"X-Role": "read_only"}, json={**body, "consent_location_use": True}).status_code == 403
    unreviewed = mk_incident("NEEDS_REVIEW")
    assert client.post("/api/v1/navigation/sessions", headers=ADMIN, json={**body, "incident_id": unreviewed, "consent_location_use": True}).status_code == 409
    s = client.post("/api/v1/navigation/sessions", headers={"X-Role": "officer"}, json={**body, "consent_location_use": True}).json()
    assert s["privacy"] and s["route"]["status"] in ("no_recorded_hazard", "penalised")
    assert any("Advisory" in r["text"] for r in s["route"]["reasons"])  # heavy rain advisory shown, not presented as a verified hazard
    sid = s["session_id"]
    on = client.post(f"/api/v1/navigation/sessions/{sid}/position", headers={"X-Role": "officer"}, json={"lon": 76.01, "lat": 11.0, "accuracy_m": 8}).json()
    assert on["on_route"] and not on["recalculate_suggested"] and on["next_step"]["instruction"] == "You have arrived"
    off = client.post(f"/api/v1/navigation/sessions/{sid}/position", headers={"X-Role": "officer"}, json={"lon": 76.01, "lat": 11.02, "accuracy_m": 80}).json()
    assert not off["on_route"] and off["recalculate_suggested"] and any("GPS accuracy is poor" in w for w in off["warnings"])
    hid = mk_hazard("closure", {"type": "Point", "coordinates": [76.02, 11.0]})
    blocked = client.post(f"/api/v1/navigation/sessions/{sid}/position", headers={"X-Role": "officer"}, json={"lon": 76.005, "lat": 11.0}).json()
    assert blocked["recalculate_suggested"] and blocked["route_status"] == "blocked"
    rr = client.post(f"/api/v1/navigation/sessions/{sid}/reroute", headers={"X-Role": "officer"}, json={"lon": 76.005, "lat": 11.0}).json()
    assert rr["rerouted"] is False and "No verified suitable route" in rr["message"]  # honest: no alternative exists
    end = client.post(f"/api/v1/navigation/sessions/{sid}/end", headers={"X-Role": "officer"}, json={"outcome": "aborted"}).json()
    assert end["summary"]["max_off_route_m"] > 1000
    stored = get_one("nav_sessions", "id", sid)
    assert stored["result"]["last_position"] is None and stored["status"] == "ENDED"
    assert client.post(f"/api/v1/navigation/sessions/{sid}/position", headers={"X-Role": "officer"}, json={"lon": 76.0, "lat": 11.0}).status_code == 409
    client.post(f"/api/v1/navigation/hazards/{hid}/resolve", headers=ADMIN)


def test_plan_route_without_provider_or_station_is_honest(monkeypatch):
    inc = mk_incident()
    monkeypatch.setattr(rakshak_routes.router_service, "route", lambda *a, **k: {"status": "unavailable", "reason": "No routing provider configured"})
    r = client.post("/api/v1/navigation/routes", headers=ADMIN, json={"incident_id": inc, "origin": {"type": "Point", "coordinates": [76.0, 11.0]}}).json()
    assert r["status"] == "unavailable" and "evaluation" not in r


# ---------------------------------------------------------------- projections
def test_projection_gates_scenarios_and_backtest():
    reg = mk_region("Projection region")
    assert client.post("/api/v1/forest/projections", headers=ADMIN, json={"region_id": reg["id"]}).json()["status"] == "INSUFFICIENT_DATA"
    mk_change(reg["id"], "2024-02-01", "2025-02-01", 3.66)
    one = client.post("/api/v1/forest/projections", headers=ADMIN, json={"region_id": reg["id"]}).json()
    assert one["status"] == "OK" and one["projections"]["continued_disturbance"]["by_horizon_days"]["30"]["range_ha"] is None
    assert one["projections"]["continued_disturbance"]["by_horizon_days"]["30"]["expected_additional_loss_ha"] == pytest.approx(0.3, rel=0.05)  # 3.66 ha / 366 d * 30
    assert one["projections"]["effective_protection"]["kind"] == "hypothetical_scenario" and one["backtest"]["status"] == "not_possible"
    for y in (2022, 2023, 2026):
        mk_change(reg["id"], f"{y - 1}-02-01", f"{y}-02-01", 3.0 + y % 3)
    many = client.post("/api/v1/forest/projections", headers=ADMIN, json={"region_id": reg["id"], "scenario_multipliers": {"effective_protection": 0.1}}).json()
    assert many["projections"]["continued_disturbance"]["by_horizon_days"]["90"]["range_ha"] and many["backtest"]["abs_error_ha"] >= 0
    assert many["projections"]["effective_protection"]["multiplier"] == 0.1 and many["is_forecast_of_location"] is False
    assert client.post("/api/v1/forest/projections", headers=ADMIN, json={"region_id": reg["id"], "scenario_multipliers": {"x": 9}}).status_code == 422
    pdf = client.get(f"/api/v1/forest/projections/{many['id']}/report.pdf", headers=ADMIN)
    assert pdf.content[:4] == b"%PDF"


# ---------------------------------------------------------------- environment
def test_environment_assessment_labels_every_section(monkeypatch):
    reg = mk_region("Env region")
    mk_change(reg["id"])
    monkeypatch.setattr(environment, "stats_for", lambda g, dem=None: {"status": "ok", "source": "test DEM", "slope_deg": {"mean": 10}, "share_steeper_than_screen": 0.4, "screen_threshold_deg": 15.0})
    monkeypatch.setattr(environment, "rainfall_summary", lambda lat, lon: {"status": "ok", "last_30_days_mm": 400, "anomaly_ratio": 1.8, "heavy_rain_in_window": True})
    monkeypatch.setattr(environment, "osm_context", lambda g: {"status": "ok", "nearest_waterway": {"distance_m": 90, "name": "X", "type": "river"}})
    a = environment.assess(reg["id"], None, "admin")
    sec = a["sections"]
    assert sec["terrain"]["evidence_class"] == "measured" and sec["erosion_susceptibility"]["evidence_class"] == "screening" and sec["erosion_susceptibility"]["level"] == "elevated"
    assert sec["carbon"]["evidence_class"] == "not_available" and sec["biodiversity"]["evidence_class"] == "not_available"
    assert "not an erosion" in sec["erosion_susceptibility"]["note"] and sec["water_runoff_screening"]["flags"]
    assert any("waterway" in f for f in a["recommended_follow_up"])
    pdf = client.get(f"/api/v1/environment/assessments/{a['id']}/report.pdf", headers={"X-Role": "read_only"})
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    csv_text = client.get(f"/api/v1/environment/assessments/{a['id']}/report.csv", headers=ADMIN).text
    assert "evidence_class" in csv_text and "carbon" in csv_text
    assert client.post("/api/v1/environment/assessments", headers={"X-Role": "read_only"}, json={"region_id": reg["id"]}).status_code == 403


def test_environment_assessment_survives_provider_failures(monkeypatch):
    reg = mk_region("Env gaps region")
    from app.services.osm import OsmUnavailable
    from app.services.weather import WeatherUnavailable

    def boom(*a, **k):
        raise RuntimeError("dem down")

    monkeypatch.setattr(environment, "stats_for", boom)
    monkeypatch.setattr(environment, "rainfall_summary", lambda *a: (_ for _ in ()).throw(WeatherUnavailable("rain down")))
    monkeypatch.setattr(environment, "osm_context", lambda g: (_ for _ in ()).throw(OsmUnavailable("osm down")))
    a = environment.assess(reg["id"], None, "admin")
    assert a["sections"]["forest_change"]["evidence_class"] == "not_available"  # no detections stored for this region
    assert a["sections"]["terrain"]["evidence_class"] == "not_available" and a["sections"]["rainfall"]["evidence_class"] == "not_available" and a["warnings"]
    assert get_one("environment_assessments", "id", a["id"])["status"] == "COMPLETE_WITH_GAPS"


# ---------------------------------------------------------------- industrial
def test_industrial_site_never_asserts_a_factory(monkeypatch):
    reg = mk_region("Industrial region")
    cid = mk_change(reg["id"])
    site = client.post(f"/api/v1/industrial/sites/from-change/{cid}", headers=ADMIN).json()
    assert "verification required" in site["name"].lower() and site["status"] == "POTENTIAL_LAND_USE_CHANGE_VERIFICATION_REQUIRED"
    insert("authorizations", {"id": f"auth-{random.random()}", "authority": "Test authority", "project_identifier": "P1", "project_name": "Test project", "status": "application", "geometry": LOSS, "dates": {}, "source_url": "https://example.test/p1", "evidence": {}, "created_at": now()})
    monkeypatch.setattr(industrial, "osm_context", lambda g: {"status": "ok"})
    monkeypatch.setattr(industrial, "stats_for", lambda g: {"status": "ok", "share_steeper_than_screen": 0.1})
    monkeypatch.setattr(industrial, "_ambient_air", lambda lat, lon: {"evidence_class": "measured", "kind": "ambient modelled PM2.5 (CAMS via Open-Meteo), NOT site emissions"})
    a = industrial.assess(site["id"], "admin")
    s = a["sections"]
    assert "verification required" in a["classification_note"].lower()
    assert s["forest_loss"]["rows"][0]["candidate_loss_hectares"] > 0
    assert s["water_pollution_exposure"]["evidence_class"] == "not_available" and "NOT site emissions" in s["ambient_air_quality"]["kind"]
    assert s["permit_and_clearance_records"]["intersecting_records"] and "not proof of authorisation" in s["permit_and_clearance_records"]["warning"]
    assert client.get(f"/api/v1/industrial/sites/{site['id']}/report.pdf", headers=ADMIN).content[:4] == b"%PDF"
    assert client.post("/api/v1/industrial/sites", headers={"X-Role": "officer"}, json={"name": "Plant", "geometry": LOSS}).status_code == 403


# ---------------------------------------------------------------- wildfire vision
def test_yolo_decode_and_nms():
    n = 30
    out = np.zeros((1, 6, n), dtype=np.float32)  # [x,y,w,h,fire,smoke] x anchors
    for i in range(3):
        out[0, :4, i] = [320 + i, 320, 100, 100]; out[0, 4, i] = 0.9 - 0.1 * i
    out[0, :4, 10] = [100, 100, 50, 50]; out[0, 5, 10] = 0.7
    boxes = FireDetector._decode(out, 0.4, 0.5)
    assert len(boxes) == 2 and {b[2] for b in boxes} == {0, 1}
    e2e = np.array([[[10, 10, 50, 50, 0.9, 1], [10, 10, 50, 50, 0.2, 0]]], dtype=np.float32)
    assert len(FireDetector._decode(e2e, 0.4, 0.5)) == 1


def test_temporal_filter_requires_repeated_detection():
    f = TemporalFilter(k=3, n=5)
    assert [f.update("cam", p)["confirmed_by_filter"] for p in (True, False, True, True)] == [False, False, False, True]
    assert not TemporalFilter().update("other", True)["confirmed_by_filter"]


def test_correlation_groups_but_does_not_merge():
    base = datetime(2026, 3, 1, 6, 0, tzinfo=timezone.utc)
    rows = [{"id": "a", "geometry": {"type": "Point", "coordinates": [76.0, 11.0]}, "observed_at": base.isoformat(), "observation_type": "active_fire", "source": "NASA FIRMS VIIRS"}, {"id": "b", "geometry": {"type": "Point", "coordinates": [76.005, 11.005]}, "observed_at": (base + timedelta(hours=3)).isoformat(), "observation_type": "camera_detection", "source": "camera:c1"}, {"id": "c", "geometry": {"type": "Point", "coordinates": [78.0, 12.0]}, "observed_at": base.isoformat(), "observation_type": "active_fire", "source": "NASA FIRMS VIIRS"}]
    cl = correlate(rows, 2.0, 24)
    assert len(cl) == 2 and sorted(len(c["member_ids"]) for c in cl) == [1, 2]
    multi = [c for c in cl if c["multi_source"]][0]
    assert set(multi["evidence_kinds"]) == {"camera", "satellite"} and "Review before" in multi["caveat"]


def test_vision_endpoint_unavailable_then_creates_review_candidate(monkeypatch):
    png = io.BytesIO(); Image.new("RGB", (64, 48), (200, 80, 20)).save(png, "PNG")
    files = {"file": ("frame.png", png.getvalue(), "image/png")}
    data = {"camera_id": "cam-test", "lat": "11.05", "lon": "76.05"}
    r = client.post("/api/v1/wildfires/vision/analyze", headers={"X-Role": "officer"}, files=files, data=data)
    assert r.status_code == 503 and "FIRE_MODEL_PATH" in r.json()["detail"]
    assert client.get("/api/v1/wildfires/vision/status", headers=ADMIN).json()["usable"] is False
    monkeypatch.setattr(rakshak_routes.detector, "status", lambda: {"usable": True, "configured": True})
    monkeypatch.setattr(rakshak_routes.detector, "detect", lambda img: {"image_size": [64, 48], "detections": [{"class": "smoke", "confidence": 0.81, "box_xyxy": [1, 1, 20, 20]}], "model": "test-model", "model_sha256": "a" * 64, "confidence_threshold": 0.4, "claim_type": "candidate_inference"})
    rakshak_routes.temporal.history.pop("cam-test", None)
    outs = []
    for i in range(3):
        outs.append(client.post("/api/v1/wildfires/vision/analyze", headers={"X-Role": "officer"}, files={"file": ("frame.png", png.getvalue(), "image/png")}, data={**data, "observed_at": f"2026-03-01T06:0{i}:00Z"}).json())
    assert [o["candidate_incident_created"] for o in outs] == [False, False, True]
    w = get_one("wildfire_observations", "id", outs[2]["wildfire_id"])
    assert w["status"] == "NEEDS_REVIEW" and w["observation_type"] == "camera_detection" and "camera position" in w["evidence"]["location_kind"]
    assert client.post("/api/v1/wildfires/vision/analyze", headers={"X-Role": "officer"}, files={"file": ("x.png", b"not an image", "image/png")}, data=data).status_code == 422
    assert client.post(f"/api/v1/wildfires/{w['id']}/create-incident", headers=ADMIN).status_code == 409  # must be reviewed first
    client.post(f"/api/v1/wildfires/{w['id']}/review", headers=ADMIN, json={"decision": "confirm", "notes": "reviewed"})
    assert client.post(f"/api/v1/wildfires/{w['id']}/create-incident", headers=ADMIN).status_code == 200


# ---------------------------------------------------------------- evidence / case
def test_evidence_upload_validation_hashing_and_audit():
    inc = mk_incident()
    ok = client.post(f"/api/v1/incidents/{inc}/evidence", headers={"X-Role": "analyst"}, files={"file": ("../../evil name.txt", b"field notes", "text/plain")})
    assert ok.status_code == 200
    e = ok.json()
    assert "path" not in e and ".." not in e["filename"] and len(e["sha256"]) == 64
    assert client.post(f"/api/v1/incidents/{inc}/evidence", headers=ADMIN, files={"file": ("run.exe", b"MZ", "application/octet-stream")}).status_code == 415
    assert client.post(f"/api/v1/incidents/{inc}/evidence", headers=ADMIN, files={"file": ("fake.pdf", b"hello", "application/pdf")}).status_code == 422
    assert client.post(f"/api/v1/incidents/{inc}/evidence", headers=ADMIN, files={"file": ("fake.png", b"hello", "image/png")}).status_code == 422
    old = settings.max_evidence_mb
    settings.max_evidence_mb = 0
    try:
        assert client.post(f"/api/v1/incidents/{inc}/evidence", headers=ADMIN, files={"file": ("a.txt", b"x", "text/plain")}).status_code == 413
    finally:
        settings.max_evidence_mb = old
    assert client.post(f"/api/v1/incidents/{inc}/evidence", headers={"X-Role": "read_only"}, files={"file": ("a.txt", b"x", "text/plain")}).status_code == 403
    d = client.get(f"/api/v1/evidence/{e['id']}/download", headers=ADMIN)
    assert d.content == b"field notes" and d.headers["x-content-type-options"] == "nosniff"
    assert any(a["action"] == "evidence_accessed" for a in list_rows("audit_events", "WHERE resource_id = ?", (inc,)))
    assert len(client.get(f"/api/v1/incidents/{inc}/evidence", headers=ADMIN).json()) == 1


def test_case_detail_aggregates_without_verdict():
    reg = mk_region("Case region")
    cid = mk_change(reg["id"])
    inc = mk_incident("NEEDS_REVIEW", change_id=cid)
    client.post("/api/v1/authorizations/match-assessment", headers=ADMIN, json={"change_id": cid})
    case = client.get(f"/api/v1/incidents/{inc}/case", headers={"X-Role": "analyst"}).json()
    assert case["incident"]["id"] == inc and case["change"]["id"] == cid and case["authorization_assessments"]
    no_record = any(a["status"] == "NO_RECORD_FOUND" for a in case["authorization_assessments"])
    assert (case["status_note"] is not None and "not that the activity is illegal" in case["status_note"]) if no_record else case["status_note"] is None
    assert client.get(f"/api/v1/incidents/{inc}/case", headers={"X-Role": "read_only"}).status_code == 403


# ---------------------------------------------------------------- scene, summary, sources, email, csv
def test_forest_scene_from_real_shaped_inputs(monkeypatch):
    reg = mk_region("Scene region")
    mk_change(reg["id"])
    grid = Grid("EPSG:32643", __import__("rasterio").transform.from_origin(500000, 1230000, 100.0, 100.0), 20, 20, 100.0)
    heights = np.linspace(700, 800, 400).reshape(20, 20)
    dem = {"heights": heights, "grid": grid, "source": "test DEM", "resolution_m": 100.0}
    monkeypatch.setattr(forestscene, "elevation_grid", lambda g, max_dim=80, margin_deg=0: dem)
    monkeypatch.setattr(forestscene, "mesh_payload", lambda g, max_dim=80, margin_deg=0: {"width": 20, "height": 20, "cell_size_m": 100.0, "heights_m": heights.tolist(), "min_m": 700, "max_m": 800, "origin": {"lon": 76.05, "lat": 11.05}, "source": "test DEM"})
    monkeypatch.setattr(forestscene, "provider", lambda: SimpleNamespace(worldcover_tree_fraction=lambda box, g: np.full((20, 20), 0.5)))
    monkeypatch.setattr(forestscene, "_mask", lambda geometry, grid, to_utm: np.ones((20, 20), dtype=bool) if geometry == reg["geometry"] else np.pad(np.ones((4, 4), dtype=bool), ((2, 14), (2, 14))))
    scene = forestscene.build(reg["id"], 20)
    assert scene["schema"] == "rakshak-forest-scene/1" and "SIMULATED" in scene["tree_density"]["placement"]
    assert [f["claim_type"] for f in scene["timeline"]] == ["reference_map", "candidate_inference"]
    assert len(scene["timeline"][1]["newly_cleared_cells"]) == 16 and scene["forecast_risk"] is None
    assert max(scene["tree_density"]["values"]) == 0.5


def test_dashboard_summary_counts_real_records():
    reg = mk_region("Summary region")
    mk_change(reg["id"], area=2.5)
    mk_incident("NEEDS_REVIEW")
    s = client.get("/api/v1/dashboard/summary", headers={"X-Role": "read_only"}).json()
    assert s["monitored_regions"] >= 1 and s["monitored_area_hectares"] > 0 and s["candidate_loss_hectares_total"] >= 2.5
    assert s["pending_incidents"] >= 1 and s["elevated_risk_regions"] == [] and s["forecast_model_usable"] in (True, False)


def test_data_sources_report_without_leaking_keys(monkeypatch):
    import httpx

    real = httpx.Client
    monkeypatch.setattr(datasources.httpx, "Client", lambda *a, **k: real(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})), timeout=5))
    datasources._cache.update(at=0.0, value=None)
    saved = settings.mapbox_api
    settings.mapbox_api = "pk.super-secret-token"
    try:
        out = client.get("/api/v1/data-sources", headers=ADMIN).json()
    finally:
        settings.mapbox_api = saved
        datasources._cache.update(at=0.0, value=None)
    assert "super-secret-token" not in json.dumps(out)
    names = {s["name"]: s for s in out["sources"]}
    assert names["Mapbox Directions"]["configured"] is True and names["NASA FIRMS"]["configured"] is False
    assert names["Microsoft Planetary Computer (Sentinel-1, Sentinel-2, WorldCover, DEM)"]["reachable"] is True


def test_email_channel_gates_and_sends_only_when_enabled(monkeypatch):
    from app.services import notify as n

    saved = (settings.environment, settings.allow_real_notifications, settings.smtp_host, settings.smtp_from)
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, msg): sent.append(msg)

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    nt = n.Notifier(settings)
    try:
        captured = nt.send(channel="email", recipient="ranger@example.test", text="Subject line\nbody", incident_id="i-email-1", event="dispatch", actor="admin", approved=True)
        assert captured["status"] == "CAPTURED_NOT_SENT" and not sent
        settings.environment, settings.allow_real_notifications, settings.smtp_host, settings.smtp_from = "production", True, "smtp.test", "rakshak@example.test"
        with pytest.raises(n.NotificationBlocked):
            nt.send(channel="email", recipient="not-an-email", text="x", incident_id="i-email-2", event="dispatch", actor="admin", approved=True)
        ok = nt.send(channel="email", recipient="ranger@example.test", text="Subject line\nbody", incident_id="i-email-3", event="dispatch", actor="admin", approved=True)
        assert ok["status"] == "SENT_TO_PROVIDER" and len(sent) == 1 and sent[0]["Subject"] == "Subject line"
    finally:
        settings.environment, settings.allow_real_notifications, settings.smtp_host, settings.smtp_from = saved


def test_csv_blocks_formula_injection_and_generic_pdf():
    out = to_csv([{"a": "=HYPERLINK(\"http://x\")", "b": "ok"}], ["a", "b"])
    assert "'=HYPERLINK" in out
    assert generic_pdf("T", "S", [("H", {"k": "<b>v</b>"}), ("L", ["x"])])[:4] == b"%PDF"


def test_observation_preview_endpoints_render_real_composites(tmp_path):
    import rasterio
    from rasterio.transform import from_origin

    f = tmp_path / "s2.tif"
    nir, swir = np.full((30, 30), 0.4), np.full((30, 30), 0.1)
    cloud = np.zeros((30, 30)); cloud[:5] = 1
    with rasterio.open(f, "w", driver="GTiff", height=30, width=30, count=3, dtype="float32", crs="EPSG:32643", transform=from_origin(500000, 1230000, 20, 20)) as d:
        d.write(np.stack([nir, swir, cloud]).astype("float32"))
    reg = mk_region("Preview region")
    oid = f"obs-{random.random()}"
    insert("observations", {"id": oid, "region_id": reg["id"], "provider": "planetary_computer", "source_scene_ids": ["S2_TEST"], "acquisition_start": "2025-02-01T05:00:00Z", "acquisition_end": "2025-02-02T05:00:00Z", "status": "OK", "quality": {"valid_pixel_fraction": 0.83}, "provenance": {"sensor": "sentinel-2", "file": str(f), "processing": {}, "warnings": []}, "created_at": now()})
    meta = client.get(f"/api/v1/observations/{oid}/preview-meta", headers={"X-Role": "read_only"}).json()
    (s, w), (n, e) = meta["bounds"]
    assert 11.0 < s < n < 11.2 and 74.9 < w < e < 75.2 and "grey = no clear observation" in meta["legend"]
    png = client.get(f"/api/v1/observations/{oid}/preview.png", headers=ADMIN)
    assert png.content[:8] == b"\x89PNG\r\n\x1a\n" and Image.open(io.BytesIO(png.content)).mode == "RGBA"
    assert client.get("/api/v1/observations/nope/preview.png", headers=ADMIN).status_code == 404
