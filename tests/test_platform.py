import time
import hashlib
import hmac
import base64
import random
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.middleware import RequestContextMiddleware
from app.core.security import Role, issue_token
from app.db.database import get_one, insert, list_rows, now
from app.main import app
from app.ops_routes import notifier
from app.services import forecasting
from app.services.energy import predict_observed
from app.services.notify import Notifier, NotificationBlocked, verify_twilio_signature
from app.services.routing import Router
from app.services.satellite import AcquisitionConfig, PlanetaryComputer, ProviderError, make_grid
from app.services.wildfire import FirmsUnavailable, parse_firms_csv, store_detections

client = TestClient(app)
ADMIN = {"X-Role": "admin"}
SQUARE = {"type": "Polygon", "coordinates": [[[76, 11], [76.2, 11], [76.2, 11.2], [76, 11.2], [76, 11]]]}
settings = get_settings()


@pytest.fixture
def prod_flags():
    saved = (settings.environment, settings.allow_real_notifications, settings.telegram_bot_token, settings.callback_secret)
    yield settings
    settings.environment, settings.allow_real_notifications, settings.telegram_bot_token, settings.callback_secret = saved


def make_incident(status="ASSIGNED"):
    iid = f"inc-{random.random()}"
    insert("incidents", {"id": iid, "change_id": None, "wildfire_id": None, "status": status, "title": "Platform test case", "geometry": {"type": "Point", "coordinates": [76.05, 11.05]}, "evidence": {}, "created_at": now(), "updated_at": now()})
    return iid


# ---------------------------------------------------------------- auth / platform
def test_jwt_mode_requires_valid_token_and_ignores_role_header():
    saved = (settings.auth_mode, settings.jwt_secret)
    settings.auth_mode, settings.jwt_secret = "jwt", "x" * 40
    try:
        assert client.get("/api/v1/regions", headers=ADMIN).status_code == 401
        assert client.get("/api/v1/regions", headers={"Authorization": "Bearer garbage"}).status_code == 401
        ro = {"Authorization": "Bearer " + issue_token("u1", Role.READ_ONLY)}
        assert client.get("/api/v1/regions", headers=ro).status_code == 200
        body = {"name": "JWT region", "jurisdiction": "Kerala", "geometry": SQUARE}
        assert client.post("/api/v1/regions", headers=ro, json=body).status_code == 403
        assert client.post("/api/v1/regions", headers={"Authorization": "Bearer " + issue_token("a1", Role.ADMIN)}, json=body).status_code == 200
        assert client.get("/health").status_code == 200  # health stays open
        expired = {"Authorization": "Bearer " + issue_token("u1", Role.ADMIN, ttl_seconds=-10)}
        assert client.get("/api/v1/regions", headers=expired).status_code == 401
    finally:
        settings.auth_mode, settings.jwt_secret = saved


def test_production_refuses_header_auth_and_short_secret():
    s = get_settings().model_copy(update={"environment": "production", "auth_mode": "dev_header"})
    with pytest.raises(RuntimeError):
        s.validate_runtime()
    with pytest.raises(RuntimeError):
        get_settings().model_copy(update={"auth_mode": "jwt", "jwt_secret": "short"}).validate_runtime()
    with pytest.raises(RuntimeError):
        get_settings().model_copy(update={"allow_real_notifications": True, "callback_secret": None}).validate_runtime()


def test_rate_limit_and_correlation_id():
    small = FastAPI()
    small.add_middleware(RequestContextMiddleware, per_minute=3)

    @small.get("/api/x")
    def x():
        return {}

    c = TestClient(small)
    codes = [c.get("/api/x").status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    assert c.get("/api/x", headers={"x-correlation-id": "abc"}).headers["x-correlation-id"] == "abc"


def test_unhandled_error_does_not_leak_internals():
    boom = FastAPI()
    boom.add_middleware(RequestContextMiddleware, per_minute=0)

    @boom.get("/api/boom")
    def b():
        raise RuntimeError("secret-internal-detail")

    r = TestClient(boom, raise_server_exceptions=False).get("/api/boom")
    assert r.status_code == 500 and "secret-internal-detail" not in r.text


def test_migrations_recorded():
    from app.db.database import connection

    with connection() as conn:
        versions = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    assert {"001_initial", "002_operations"} <= versions


# ---------------------------------------------------------------- satellite
def test_grid_rejects_oversized_region():
    big = {"type": "Polygon", "coordinates": [[[75, 10], [77, 10], [77, 12], [75, 12], [75, 10]]]}
    with pytest.raises(ProviderError):
        make_grid(big, AcquisitionConfig())
    g = make_grid(SQUARE, AcquisitionConfig(resolution_m=100))
    assert g.crs == "EPSG:32643" and g.width > 0


def test_stac_search_error_is_explicit_provider_error(monkeypatch):
    monkeypatch.setattr("app.services.satellite.time.sleep", lambda *_: None)
    pc = PlanetaryComputer(client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))))
    with pytest.raises(ProviderError):
        pc.search("sentinel-2-l2a", [76, 11, 76.1, 11.1], datetime(2025, 1, 1).date(), datetime(2025, 2, 1).date())


def test_unconfigured_acquisition_returns_unavailable_status():
    region = client.post("/api/v1/regions", headers=ADMIN, json={"name": "No raster", "jurisdiction": "Kerala", "geometry": SQUARE}).json()
    r = client.post("/api/v1/forest/change-detection", headers=ADMIN, json={"region_id": region["id"], "baseline_date": "2025-01-01T00:00:00Z", "comparison_date": "2025-02-01T00:00:00Z"})
    assert r.status_code == 200
    assert client.post(f"/api/v1/regions/{region['id']}/observations", headers=ADMIN, json={"provider": "unconfigured"}).status_code == 422


# ---------------------------------------------------------------- forecasting
def add_loss(lon, lat, when, region_id=None):
    d = 0.0004
    insert("changes", {"id": f"chg-{random.random()}", "region_id": region_id, "geometry": {"type": "Polygon", "coordinates": [[[lon, lat], [lon + d, lat], [lon + d, lat + d], [lon, lat + d], [lon, lat]]]}, "area_hectares": 0.2, "baseline_date": (when - timedelta(days=365)).isoformat(), "comparison_date": when.isoformat(), "disturbance_type": "CANDIDATE_FOREST_LOSS", "score": 0.3, "status": "CANDIDATES_FOUND", "provenance": {"polygon_scores": []}, "created_at": now()})


def test_forecast_unavailable_without_model():
    region = client.post("/api/v1/regions", headers=ADMIN, json={"name": "Forecast region", "jurisdiction": "Kerala", "geometry": SQUARE}).json()
    f = client.post("/api/v1/forest/forecasts", headers=ADMIN, json={"region_id": region["id"], "horizon_days": 30}).json()
    assert f["status"] == "UNAVAILABLE" and f["risk_score"] is None
    assert client.get(f"/api/v1/forest/forecasts/{f['id']}/layers").json()["layers"] == []
    future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    assert client.post("/api/v1/forest/forecasts", headers=ADMIN, json={"region_id": region["id"], "horizon_days": 30, "origin": future}).status_code == 422


def test_features_never_use_future_events():
    origin = datetime(2024, 6, 1, tzinfo=timezone.utc)
    cells = [(76.005, 11.005)]
    past = {"lon": 76.005, "lat": 11.005, "date": origin - timedelta(days=10), "area_ha": 1.0}
    future = {"lon": 76.005, "lat": 11.005, "date": origin + timedelta(days=5), "area_ha": 99.0}
    with_future = forecasting.features_at(cells, [past, future], origin)
    without = forecasting.features_at(cells, [past], origin)
    assert np.array_equal(with_future, without)
    assert forecasting.labels_for(cells, [past, future], origin, 7)[0] == 1.0   # future event is the label...
    assert forecasting.labels_for(cells, [past, future], origin, 3)[0] == 0.0  # ...only inside the horizon


def test_average_precision_known_values():
    y = np.array([1, 0, 1, 0]); s = np.array([0.9, 0.8, 0.7, 0.1])
    assert forecasting.average_precision(y, s) == pytest.approx((1 + 2 / 3) / 2)


def test_training_refuses_small_history():
    r = forecasting.train(None, 30, "admin")
    assert r["status"] == "INSUFFICIENT_DATA"


def test_training_pipeline_time_split_and_registry():
    rng = random.Random(7)
    region = client.post("/api/v1/regions", headers=ADMIN, json={"name": "Training region", "jurisdiction": "Kerala", "geometry": {"type": "Polygon", "coordinates": [[[80, 10], [80.2, 10], [80.2, 10.2], [80, 10.2], [80, 10]]]}}).json()
    start = datetime(2022, 1, 1, tzinfo=timezone.utc)
    pts = [(80.1, 10.1)]
    for month in range(36):
        when = start + timedelta(days=30 * month + 3)
        for _ in range(10):
            if rng.random() < 0.7:
                bx, by = rng.choice(pts)
                p = (min(max(bx + rng.uniform(-0.02, 0.02), 80.005), 80.195), min(max(by + rng.uniform(-0.02, 0.02), 10.005), 10.195))
            else:
                p = (rng.uniform(80.005, 80.195), rng.uniform(10.005, 10.195))
            pts.append(p); add_loss(p[0], p[1], when + timedelta(days=rng.randint(0, 20)), region["id"])
    result = forecasting.train(region["id"], 30, "admin")
    assert result["status"] == "TRAINED", result
    m = result["metrics"]
    assert m["positives"] >= forecasting.MIN_TEST_POS and "baseline_pr_auc" in m and m["calibration"]
    reg = get_one("model_registry", "id", result["model_id"])
    assert reg["training"]["embargo_days"] == 30 and reg["checksum"]
    assert list_rows("evaluations", "WHERE model_id = ?", (result["model_id"],))
    # a model that is not "usable" must never produce a surface
    if not result["usable"]:
        out = forecasting.run_forecast(region, 30, datetime.now(timezone.utc))
        assert out["status"] == "UNAVAILABLE"


# ---------------------------------------------------------------- wildfire
CSV = "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_ti5,frp,daynight\n11.05,76.05,330.1,0.4,0.4,2025-02-10,0742,N,VIIRS,h,2.0NRT,290.2,5.2,D\nbad,row,,,,,,,,,,,,\n"


def test_firms_parse_dedupe_and_missing_key():
    rows = parse_firms_csv(CSV, "VIIRS_SNPP_NRT")
    assert len(rows) == 1 and rows[0]["confidence"] == 0.9 and rows[0]["observed_at"].hour == 7
    first = store_detections(rows, "VIIRS_SNPP_NRT", None, "admin")
    again = store_detections(rows, "VIIRS_SNPP_NRT", None, "admin")
    assert first["inserted"] == 1 and again["duplicates"] == 1
    saved = settings.firms_api_key
    settings.firms_api_key = None
    try:
        region = client.post("/api/v1/regions", headers=ADMIN, json={"name": "Fire region", "jurisdiction": "Kerala", "geometry": SQUARE}).json()
        r = client.post("/api/v1/wildfires/ingest-firms", headers=ADMIN, json={"region_id": region["id"]})
        assert r.status_code == 503 and "FIRMS_API_KEY" in r.json()["detail"]
    finally:
        settings.firms_api_key = saved
    w = [x for x in list_rows("wildfire_observations") if x["source"].startswith("NASA FIRMS")][0]
    assert w["observation_type"] == "active_fire" and w["status"] == "NEEDS_REVIEW"


# ---------------------------------------------------------------- notifications
def test_safe_mode_captures_and_is_idempotent():
    iid = make_incident()
    a = notifier.send(channel="telegram", recipient="12345", text="t", incident_id=iid, event="dispatch", actor="admin", approved=True)
    b = notifier.send(channel="telegram", recipient="12345", text="t", incident_id=iid, event="dispatch", actor="admin", approved=True)
    assert a["status"] == "CAPTURED_NOT_SENT" and b["duplicate_suppressed"] and a["id"] == b["id"]


def test_real_send_blocked_unless_approved_and_enabled(prod_flags):
    s = prod_flags
    s.environment, s.allow_real_notifications, s.telegram_bot_token = "production", True, "TOKEN"
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    n = Notifier(s, client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(NotificationBlocked):
        n.send(channel="telegram", recipient="111", text="x", incident_id="i-unapproved", event="dispatch", actor="admin", approved=False)
    assert calls == []
    ok = n.send(channel="telegram", recipient="111", text="x", incident_id="i-approved", event="dispatch", actor="admin", approved=True)
    assert ok["status"] == "SENT_TO_PROVIDER" and len(calls) == 1  # NOT "DELIVERED"


def test_provider_failure_is_not_acknowledged(prod_flags):
    s = prod_flags
    s.environment, s.allow_real_notifications, s.telegram_bot_token = "production", True, "TOKEN"
    n = Notifier(s, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"ok": False, "description": "chat not found"}))))
    r = n.send(channel="telegram", recipient="999", text="x", incident_id="i-fail", event="dispatch", actor="admin", approved=True)
    assert r["status"] == "FAILED"
    assert n.acknowledge(r["id"], "999") is None


def test_dispatch_flow_requires_review_and_assignment():
    iid = make_incident("DETECTED")
    assert client.post(f"/api/v1/incidents/{iid}/dispatch", headers=ADMIN, json={"channel": "telegram", "recipient": "1"}).status_code == 409
    assert client.post(f"/api/v1/incidents/{iid}/dispatch", headers={"X-Role": "read_only"}, json={"channel": "test", "recipient": "1"}).status_code == 403
    assert client.post(f"/api/v1/incidents/{iid}/review", headers={"X-Role": "officer"}, json={"decision": "approve"}).status_code == 403


def test_telegram_callback_validates_secret_and_sender(prod_flags):
    prod_flags.callback_secret = "s" * 20
    iid = make_incident("DISPATCHED")
    nid = f"n-{random.random()}"
    insert("notifications", {"id": nid, "channel": "telegram", "recipient": "4242", "status": "SENT_TO_PROVIDER", "payload": {"incident_id": iid}, "provider_response": {}, "created_at": now(), "idempotency_key": nid})
    body = {"callback_query": {"data": f"ack:{nid}", "from": {"id": 4242}, "message": {"chat": {"id": 4242}}}}
    assert client.post("/api/v1/notifications/telegram/callback", json=body).status_code == 403
    hdr = {"x-telegram-bot-api-secret-token": "s" * 20}
    forged = {"callback_query": {"data": f"ack:{nid}", "from": {"id": 666}, "message": {"chat": {"id": 666}}}}
    assert client.post("/api/v1/notifications/telegram/callback", json=forged, headers=hdr).status_code == 403
    assert get_one("notifications", "id", nid)["status"] == "SENT_TO_PROVIDER"
    assert client.post("/api/v1/notifications/telegram/callback", json=body, headers=hdr).json()["acknowledged"] is True
    assert get_one("notifications", "id", nid)["status"] == "ACKNOWLEDGED"
    assert get_one("incidents", "id", iid)["status"] == "ACKNOWLEDGED"


def test_twilio_signature_check():
    url, params, token = "https://x.test/cb?nid=1", {"CallStatus": "completed", "To": "+911"}, "tok"
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    sig = base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()
    assert verify_twilio_signature(token, url, params, sig)
    assert not verify_twilio_signature(token, url, {**params, "To": "+912"}, sig)
    assert client.post("/api/v1/notifications/voice/status?nid=1", data=params).status_code == 403


# ---------------------------------------------------------------- routing
def test_routing_unavailable_without_engine_and_parses_osrm():
    assert Router(settings).route([76, 11], [76.1, 11.1])["status"] == "unavailable"
    s = settings.model_copy(update={"routing_provider": "osrm", "osrm_url": "http://osrm.test"})

    def handler(req):
        return httpx.Response(200, json={"code": "Ok", "routes": [{"distance": 12345, "duration": 900, "geometry": {"type": "LineString", "coordinates": [[76, 11], [76.1, 11.1]]}}], "waypoints": [{"distance": 10}, {"distance": 900}]})

    r = Router(s, client=httpx.Client(transport=httpx.MockTransport(handler))).route([76, 11], [76.1, 11.1])
    assert r["primary"]["distance_km"] == 12.35 and "warning" in r and "not verified" in r["caveat"]
    down = Router(s, client=httpx.Client(transport=httpx.MockTransport(lambda q: (_ for _ in ()).throw(httpx.ConnectError("x")))))
    assert down.route([76, 11], [76.1, 11.1])["status"] == "error"


def test_inspection_route_needs_station_when_no_origin():
    iid = make_incident()
    r = client.post("/api/v1/routes/inspection", headers=ADMIN, json={"incident_id": iid})
    assert r.status_code in (200, 409)
    st = client.post("/api/v1/stations", headers=ADMIN, json={"name": "Station Alpha", "jurisdiction": "Kerala", "location": {"type": "Point", "coordinates": [76.0, 11.0]}, "contacts": {"telegram_chat_id": "777"}}).json()
    assert st["contacts"] == {"telegram_chat_id": "***"}
    out = client.post("/api/v1/routes/inspection", headers=ADMIN, json={"incident_id": iid}).json()
    assert out["status"] == "unavailable" and "Station Alpha" in out["origin_source"]


# ---------------------------------------------------------------- energy
def test_energy_never_invents_numbers():
    base = {"floor_area_m2": 1000, "building_type": "factory"}
    r = client.post("/api/v1/buildings/energy-predictions", headers=ADMIN, json=base).json()
    assert r["status"] == "UNAVAILABLE" and "annual_kwh" not in r
    hist = [{"month": f"{y}-{m:02d}", "kwh": 1000 + 50 * m + 10 * y % 7} for y in (2023, 2024) for m in range(1, 13)]
    ok = client.post("/api/v1/buildings/energy-predictions", headers=ADMIN, json={**base, "meter_history": hist}).json()
    assert ok["status"] == "ESTIMATED_FROM_METER_HISTORY" and ok["prediction_interval"] and ok["annual_kwh"] > 0
    assert client.post("/api/v1/buildings/energy-predictions", headers=ADMIN, json={**base, "meter_history": [{"month": "bad", "kwh": 1}]}).status_code == 422
    assert client.post("/api/v1/buildings/scenarios", headers=ADMIN, json={**base, "assumptions": {}}).status_code == 422
    sc = client.post("/api/v1/buildings/scenarios", headers=ADMIN, json={**base, "assumptions": {"intensity_kwh_per_m2_year": 100, "intensity_source": "user estimate"}}).json()
    assert sc["annual_kwh"] == 100000 and sc["is_prediction_of_real_facility"] is False and sc["emissions"]["status"] == "not_calculated"
    sc2 = client.post("/api/v1/buildings/scenarios", headers=ADMIN, json={**base, "assumptions": {"intensity_kwh_per_m2_year": 100, "emission_factor_kg_per_kwh": 0.5, "emission_factor_source": "user-cited"}}).json()
    assert sc2["emissions"]["kg_co2e_per_year"] == 50000


# ---------------------------------------------------------------- reports / incidents / history
def test_report_contains_separated_evidence_and_is_role_gated():
    from app.services.analysis import _maybe_open_incident

    region = client.post("/api/v1/regions", headers=ADMIN, json={"name": "Report region", "jurisdiction": "Kerala", "geometry": SQUARE}).json()
    chg = {"id": f"chg-{random.random()}", "region_id": region["id"], "geometry": {"type": "Polygon", "coordinates": [[[76.05, 11.05], [76.06, 11.05], [76.06, 11.06], [76.05, 11.06], [76.05, 11.05]]]}, "area_hectares": 1.2, "baseline_date": "2024-02-01T00:00:00+00:00", "comparison_date": "2025-02-01T00:00:00+00:00", "disturbance_type": "CANDIDATE_FOREST_LOSS", "score": 0.4, "status": "CANDIDATES_FOUND", "provenance": {"polygon_scores": [{"evidence_level": "optical_radar_agree"}]}, "created_at": now()}
    insert("changes", chg)
    assert _maybe_open_incident({**chg}, chg["provenance"], "admin")
    assert _maybe_open_incident({**chg, "area_hectares": 0.1}, chg["provenance"], "admin") is None
    assert _maybe_open_incident({**chg}, {"polygon_scores": [{"evidence_level": "optical_radar_disagree"}]}, "admin") is None
    incident = [i for i in list_rows("incidents") if i["change_id"] == chg["id"]][0]
    assert incident["status"] == "NEEDS_REVIEW"
    rep = client.post(f"/api/v1/incidents/{incident['id']}/reports", headers=ADMIN).json()
    assert rep["status"] == "generated" and len(rep["package_sha256"]) == 64
    meta = client.get(f"/api/v1/reports/{rep['pdf_id']}", headers=ADMIN).json()
    assert "path" not in meta
    assert client.get(f"/api/v1/reports/{rep['pdf_id']}/download", headers=ADMIN).content[:4] == b"%PDF"
    pkg = client.get(f"/api/v1/reports/{rep['id']}/download", headers=ADMIN).json()
    assert set(pkg["evidence"]) >= {"observed", "inferred", "forecast", "simulation", "official_records", "human_decisions"}
    assert pkg["evidence"]["inferred"][0]["score_note"].startswith("uncalibrated")
    assert "not a finding of illegality" in pkg["disclaimer"]
    assert client.get(f"/api/v1/reports/{rep['pdf_id']}/download", headers={"X-Role": "read_only"}).status_code == 403


def test_history_frames_are_real_and_not_interpolated():
    region = client.post("/api/v1/regions", headers=ADMIN, json={"name": "History region", "jurisdiction": "Kerala", "geometry": SQUARE}).json()
    for i, d in enumerate(["2024-01-10T05:00:00Z", "2024-06-10T05:00:00Z"]):
        insert("observations", {"id": f"o-{region['id']}-{i}", "region_id": region["id"], "provider": "planetary_computer", "source_scene_ids": ["S2_X"], "acquisition_start": d, "acquisition_end": d, "status": "OK", "quality": {"valid_pixel_fraction": 0.9}, "provenance": {"sensor": "sentinel-2"}, "created_at": now()})
    h = client.get(f"/api/v1/forest/history/{region['id']}/frames", headers=ADMIN).json()
    assert [f["observed_from"][:10] for f in h["frames"]] == ["2024-01-10", "2024-06-10"]
    assert h["interpolated"] is False and h["observation_gaps"][0]["days"] > 100


def test_unauthorised_reviewer_actions_blocked_and_audit_requires_role():
    assert client.get("/api/v1/audit/events", headers={"X-Role": "read_only"}).status_code == 403
    assert client.post("/api/v1/forest/forecasts/train", headers={"X-Role": "reviewer"}, json={"horizon_days": 30}).status_code == 403


# ---------------------------------------------------------------- routing providers, leads, AR, client config
def _mapbox_handler(req):
    assert "api.mapbox.com" in str(req.url)
    return httpx.Response(200, json={"code": "Ok", "routes": [{"distance": 20000, "duration": 1500, "geometry": {"type": "LineString", "coordinates": [[76, 11], [76.1, 11.1]]}}, {"distance": 22000, "duration": 1700, "geometry": {"type": "LineString", "coordinates": [[76, 11], [76.1, 11.1]]}}], "waypoints": [{"distance": 5}, {"distance": 8}]})


def test_router_provider_order_and_fallback():
    s = settings.model_copy(update={"routing_provider": "mapbox,serpapi", "mapbox_api": "pk.test", "serp_api": "serp-test"})
    r = Router(s, client=httpx.Client(transport=httpx.MockTransport(_mapbox_handler)))
    assert r.providers == ["mapbox", "serpapi"]
    out = r.route([76, 11], [76.1, 11.1])
    assert out["provider"] == "mapbox" and out["primary"]["distance_km"] == 20.0 and len(out["alternatives"]) == 1

    def mapbox_down_serp_ok(req):
        if "mapbox" in str(req.url):
            return httpx.Response(500, json={"message": "boom"})
        return httpx.Response(200, json={"directions": [{"travel_mode": "Driving", "distance": 31000, "duration": 2400, "via": "NH 766"}]})

    out2 = Router(s, client=httpx.Client(transport=httpx.MockTransport(mapbox_down_serp_ok))).route([76, 11], [76.1, 11.1])
    assert out2["provider"].startswith("serpapi") and out2["primary"]["geometry"] is None and "fallback_note" in out2

    def all_down(req):
        raise httpx.ConnectError("x")

    out3 = Router(s, client=httpx.Client(transport=httpx.MockTransport(all_down))).route([76, 11], [76.1, 11.1])
    assert out3["status"] == "error" and "token" not in str(out3).lower() and "pk.test" not in str(out3)


def test_patrol_visits_nearest_first():
    s = settings.model_copy(update={"routing_provider": "mapbox", "mapbox_api": "pk.test"})
    out = Router(s, client=httpx.Client(transport=httpx.MockTransport(_mapbox_handler))).patrol([76.0, 11.0], [[76.5, 11.5], [76.01, 11.01]])
    assert out["visit_order"] == [1, 0] and len(out["legs"]) == 2


def test_web_leads_are_labelled_and_need_key():
    from app.services.leads import LeadsUnavailable, search

    with pytest.raises(LeadsUnavailable):
        search(settings, "forest clearance Wayanad")
    s = settings.model_copy(update={"serp_api": "k"})
    resp = {"organic_results": [{"title": "Proposal X", "link": "https://parivesh.nic.in/x", "snippet": "..."}, {"title": "Blog", "link": "https://example.com/p", "snippet": "..."}]}
    out = search(s, "forest clearance Wayanad", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=resp))))
    assert out["claim_type"] == "unverified_web_lead" and [l["official_domain"] for l in out["leads"]] == [True, False]
    assert client.post("/api/v1/authorizations/web-leads", headers=ADMIN, json={"query": "forest clearance"}).status_code == 503
    assert client.post("/api/v1/authorizations/web-leads", headers={"X-Role": "officer"}, json={"query": "forest clearance"}).status_code == 403


def test_ar_scene_is_labelled_simulated_and_local_frame():
    region = client.post("/api/v1/regions", headers=ADMIN, json={"name": "AR region", "jurisdiction": "Kerala", "geometry": SQUARE}).json()
    loss = {"type": "Polygon", "coordinates": [[[76.05, 11.05], [76.06, 11.05], [76.06, 11.06], [76.05, 11.06], [76.05, 11.05]]]}
    sim = client.post("/api/v1/forest/simulations", headers=ADMIN, json={"region_id": region["id"], "geometry": loss, "horizon_days": 30}).json()
    scene = client.get(f"/api/v1/forest/simulations/{sim['id']}/ar", headers={"X-Role": "read_only"}).json()
    assert "SIMULATED" in scene["mandatory_label"] and scene["is_observation"] is False and scene["is_forecast"] is False
    ids = [l["id"] for l in scene["layers"]]
    assert ids == ["initial_forest", "remaining_forest", "simulated_loss"]
    ring = scene["layers"][2]["polygons"][0]["exterior"]
    xs = [p[0] for p in ring]; ys = [p[1] for p in ring]
    assert max(xs) - min(xs) == pytest.approx(1092, rel=0.02) and max(ys) - min(ys) == pytest.approx(1105, rel=0.02)  # ~0.01 deg
    assert abs(sum(xs) / len(xs)) < 600 and scene["anchor"]["altitude"] is None
    assert client.get("/api/v1/forest/simulations/nope/ar", headers=ADMIN).status_code == 404


def test_client_config_exposes_only_public_mapbox_token():
    saved = settings.mapbox_api
    try:
        settings.mapbox_api = "sk.secret"
        assert client.get("/api/v1/system/client-config", headers=ADMIN).json()["mapbox_public_token"] is None
        import app.main as main_mod

        settings.mapbox_api = "pk.public"
        main_mod._token_check["pk.public"] = (time.time() + 60, True)    # Mapbox accepts it
        assert client.get("/api/v1/system/client-config", headers=ADMIN).json()["mapbox_public_token"] == "pk.public"
        main_mod._token_check["pk.public"] = (time.time() + 60, False)   # Mapbox refuses it: the map must not go blank
        assert client.get("/api/v1/system/client-config", headers=ADMIN).json()["mapbox_public_token"] is None
    finally:
        settings.mapbox_api = saved


def test_http_client_loggers_are_quiet_so_keys_do_not_reach_logs():
    import logging
    from app.core.middleware import configure_logging

    configure_logging("INFO")
    assert logging.getLogger("httpx").level >= logging.WARNING
