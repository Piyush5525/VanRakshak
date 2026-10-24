import time

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)
HEADERS = {"X-Role": "admin"}


def test_health_and_capabilities_are_explicit():
    assert client.get("/health").json()["status"] == "ok"
    capabilities = client.get("/api/v1/system/capabilities").json()
    assert capabilities["features"]["real_satellite_inference"] is False
    assert capabilities["features"]["real_forecast_model"] is False


def test_role_protection_and_geometry_validation():
    payload = {"name": "Blocked", "jurisdiction": "India", "geometry": {"type": "Point", "coordinates": [76, 11]}}
    assert client.post("/api/v1/regions", json=payload).status_code == 403
    invalid = {**payload, "geometry": {"type": "Point", "coordinates": [999, 11]}}
    assert client.post("/api/v1/regions", headers=HEADERS, json=invalid).status_code == 422


def test_end_to_end_safe_evidence_flow():
    region = client.post("/api/v1/regions", headers=HEADERS, json={"name": "Test forest", "jurisdiction": "Kerala", "geometry": {"type": "Polygon", "coordinates": [[[76, 11], [76.1, 11], [76.1, 11.1], [76, 11.1], [76, 11]]]}})
    assert region.status_code == 200
    region_id = region.json()["id"]

    run = client.post("/api/v1/forest/change-detection", headers=HEADERS, json={"region_id": region_id, "baseline_date": "2025-01-01T00:00:00Z", "comparison_date": "2025-02-01T00:00:00Z"})
    assert run.status_code == 200
    job_id = run.json()["run_id"]
    job = None
    for _ in range(20):
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in {"completed", "failed"}:
            break
        time.sleep(0.02)
    assert job is not None
    assert job["status"] == "completed"
    assert job["result"]["status"] == "UNAVAILABLE"
    assert job["result"]["provenance"]["claim_type"] == "candidate_inference"

    forecast = client.post("/api/v1/forest/forecasts", headers=HEADERS, json={"region_id": region_id, "horizon_days": 30})
    assert forecast.status_code == 200
    assert forecast.json()["status"] == "UNAVAILABLE"

    incident = client.post("/api/v1/incidents", headers=HEADERS, json={"title": "Reviewable test case", "geometry": {"type": "Point", "coordinates": [76.04, 11.04]}, "evidence": {"claim_type": "human_decision"}})
    assert incident.status_code == 200
    incident_id = incident.json()["id"]
    assert client.post(f"/api/v1/incidents/{incident_id}/dispatch", headers=HEADERS, json={"channel": "test", "recipient": "test-recipient"}).status_code == 409
    approved = client.post(f"/api/v1/incidents/{incident_id}/review", headers=HEADERS, json={"decision": "approve", "notes": "Evidence package ready for inspection"})
    assert approved.json()["status"] == "APPROVED_FOR_INSPECTION"
    assigned = client.post(f"/api/v1/incidents/{incident_id}/assign", headers={"X-Role": "officer"}, json={"assignee": "Station Alpha"})
    assert assigned.json()["status"] == "ASSIGNED"
    dispatched = client.post(f"/api/v1/incidents/{incident_id}/dispatch", headers={"X-Role": "officer"}, json={"channel": "test", "recipient": "station-alpha"})
    assert dispatched.json()["delivery"]["safe_mode"] is True


def test_missing_authorization_is_not_illegality():
    change = client.get("/api/v1/forest/changes").json()[0]
    result = client.post("/api/v1/authorizations/match-assessment", headers=HEADERS, json={"change_id": change["id"], "candidate_authorization_ids": []})
    assert result.status_code == 200
    body = result.json()
    assert body["status"] == "NO_RECORD_FOUND"
    assert "not a finding of illegality" in body["rationale"]["warning"]
