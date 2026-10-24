import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import fire_routes, live_routes
from app.core.config import get_settings
from app.db.database import connection, insert, now
from app.main import app
from app.services import fireimpact, fireivr, firecomms, fireops
from app.services import firespread as FS
from app.services import firestations

client = TestClient(app)
ADMIN = {"X-Role": "admin"}
settings = get_settings()


def new_incident(demo=False, tag=None, lat=21.9, lon=78.0, status=None):
    inc, _ = fireops.create("nasa_firms" if not demo else "demo", lat + (0 if demo else time.time() % 1e-3), lon, now(), {"frp_mw": 20, "confidence": 0.8, "satellite": "N", "currency": "current"}, "admin", is_demo=demo, demo_tag=tag or (f"t{time.time_ns()}" if demo else None))
    if status:
        path = {"CONFIRMED": ["CONFIRMED"], "DISPATCH_REQUESTED": ["CONFIRMED", "DISPATCH_REQUESTED"], "DISPATCHED": ["CONFIRMED", "DISPATCH_REQUESTED", "DISPATCHED"]}[status]
        for s in path:
            inc = fireops.transition(inc["id"], s, "admin")
    return inc


# ------------------------------------------------------------------ workflow
def test_incident_is_idempotent_and_enters_verification_not_confirmed():
    a, created = fireops.create("nasa_firms", 20.1234, 80.4321, "2026-10-24T08:01:00+00:00", {"frp_mw": 5}, "admin")
    b, created2 = fireops.create("nasa_firms", 20.1234, 80.4321, "2026-10-24T08:40:00+00:00", {"frp_mw": 5}, "admin")  # same place, same hour
    assert created and not created2 and a["id"] == b["id"] and a["status"] == "NEEDS_VERIFICATION"
    evs = fireops.events(a["id"])
    assert [e["event_type"] for e in evs][:3] == ["incident_created", "status_changed", "incident_duplicate"]


def test_state_machine_blocks_skipping_and_terminal_states():
    inc = new_incident()
    with pytest.raises(fireops.WorkflowError):
        fireops.transition(inc["id"], "DISPATCH_REQUESTED", "admin")  # cannot dispatch an unverified hotspot
    inc = fireops.transition(inc["id"], "REJECTED", "admin", "industrial heat")
    assert inc["closed_at"]
    with pytest.raises(fireops.WorkflowError):
        fireops.transition(inc["id"], "CONFIRMED", "admin")


def test_reset_demo_removes_only_demo_incidents():
    real = new_incident()
    demo = new_incident(demo=True)
    fireops.log_event(demo["id"], "x", "demo", "info", "demo event", is_demo=True)
    out = fireops.reset_demo()
    assert demo["id"] in out["ids"] and fireops.get(demo["id"]) is None
    assert fireops.get(real["id"]) is not None and fireops.events(real["id"])


# ------------------------------------------------------------------ spread model
def horizon(wind=25.0, frm=270.0, temp=35.0, rh=25.0):
    return {"wind_kmh": wind, "wind_from_deg": frm, "temp_c": temp, "rh": rh}


def run_sim(hz=None, rain7=0.0, rain24=0.0, slope=0.0, lc=None, seed=1, params=FS.SpreadParams(n_runs=120)):
    inputs = {"horizons": {3: hz or horizon(), 6: hz or horizon(), 12: hz or horizon()}, "rain_7d_mm": rain7, "rain_24h_mm": rain24, "slope_deg": slope}
    return FS.simulate(21.9, 78.0, inputs, params, seed, landcover=lc)


def test_spread_is_reproducible_grows_with_time_and_follows_the_wind():
    a, b = run_sim(seed=7), run_sim(seed=7)
    assert a["horizons"]["12"]["area_ha"] == b["horizons"]["12"]["area_ha"] and a["horizons"]["12"]["contours"]["likely_50"] == b["horizons"]["12"]["contours"]["likely_50"]
    areas = [a["horizons"][h]["area_ha"]["p50"] for h in ("3", "6", "12")]
    assert areas[0] < areas[1] < areas[2]
    west = run_sim(horizon(frm=270))["horizons"]["12"]
    assert west["spread_toward_deg"] == 90.0  # wind FROM the west blows the fire east
    from shapely.geometry import shape
    cx = shape(west["contours"]["likely_50"]).centroid.x
    assert cx > 78.0, "footprint must be downwind (east) of the ignition point"
    north = run_sim(horizon(frm=0))["horizons"]["12"]
    assert shape(north["contours"]["likely_50"]).centroid.y < 21.9


def test_wet_calm_conditions_give_almost_no_spread_and_uncertainty_is_reported():
    wet = run_sim(horizon(wind=3, temp=24, rh=90), rain7=150.0, rain24=30.0)["horizons"]["12"]
    dry = run_sim()["horizons"]["12"]
    assert wet["head_distance_km"]["p50"] < 0.1 < dry["head_distance_km"]["p50"]
    assert dry["area_ha"]["p10"] < dry["area_ha"]["p50"] < dry["area_ha"]["p90"]
    assert dry["contour_area_ha"]["possible_10"] >= dry["contour_area_ha"]["likely_50"]


def test_upslope_spreads_faster_and_water_and_built_up_cells_cannot_burn():
    flat, up = run_sim(slope=0)["horizons"]["12"], run_sim(slope=15)["horizons"]["12"]
    assert up["head_distance_km"]["p50"] > flat["head_distance_km"]["p50"]
    g = FS.make_grid(21.9, 78.0, {"horizons": {3: horizon(), 6: horizon(), 12: horizon()}, "rain_7d_mm": 0, "rain_24h_mm": 0, "slope_deg": 0}, FS.SpreadParams(n_runs=120))
    lc = np.full((g["n"], g["n"]), 10, dtype="uint8")
    lc[:, g["n"] // 2 + 3:] = 80  # a lake to the east
    blocked = run_sim(lc=lc)["horizons"]["12"]
    assert blocked["area_ha"]["p50"] < run_sim()["horizons"]["12"]["area_ha"]["p50"] * 0.6


def test_physical_formulas_known_values():
    assert FS.drought_factor(0, 0) == 10 and FS.drought_factor(100, 25) == 1 and FS.drought_factor(25, 0) == 5
    assert float(FS.ffdi(10, 30, 30, 20)) > float(FS.ffdi(10, 30, 60, 20)) > 0  # drier air -> higher danger
    assert float(FS.length_breadth(0, 0.4)) == pytest.approx(1.0, abs=0.05) and float(FS.length_breadth(40, 0.4)) > 2
    speed, frm = FS.wind_mean([10, 10], [350, 10])
    assert frm in (0.0, 360.0) or abs(frm) < 1 and speed == pytest.approx(9.85, abs=0.05)


# ------------------------------------------------------------------ stations / routing
class FakeRouter:
    available = True

    def __init__(self, table):
        self.table = table

    def route(self, origin, dest, alternatives=True):
        key = round(origin[0], 3)
        if key not in self.table:
            return {"status": "no_route", "reason": "no road"}
        km, mins = self.table[key]
        g = {"type": "LineString", "coordinates": [origin, dest]}
        return {"status": "ok", "provider": "fake", "primary": {"distance_km": km, "duration_min": mins, "geometry": g, "steps": []}, "alternatives": []}


def add_station(name, lon, lat):
    insert("stations", {"id": f"st-{name}", "name": name, "jurisdiction": "x", "location": {"type": "Point", "coordinates": [lon, lat]}, "coverage": None, "contacts": {"phone": "+911234567"}, "availability_note": None, "created_at": now(), "kind": "fire_station", "source": "test", "verified": 0, "external_ref": None})


def test_route_selection_uses_road_time_not_straight_line_and_surfaces_failures():
    inc = new_incident(lat=30.0, lon=70.0)  # an isolated spot so only these stations are candidates
    add_station("Near but slow", 70.10, 30.0)   # ~10 km straight line, slow road
    add_station("Farther but fast", 70.25, 30.0)  # ~24 km straight line, fast road
    add_station("No road", 70.15, 30.0)
    router = FakeRouter({70.1: (35.0, 90.0), 70.25: (28.0, 41.0)})
    res = firestations.compute_route(inc["id"], router)
    assert res["status"] == "OK" and res["station"]["name"] == "Farther but fast" and res["duration_min"] == 41.0
    assert res["station"]["verified"] is False and "unverified" in " ".join(res["caveats"]).lower() and "No dispatch has occurred" in " ".join(res["caveats"])
    names = {c["station_name"]: c for c in res["candidates"]}
    assert names["No road"]["status"] == "no_route" and names["Near but slow"]["straight_line_km"] < names["Farther but fast"]["straight_line_km"]
    assert any(e["status"] == "failed" and "No route from No road" in e["message"] for e in fireops.events(inc["id"]))
    assert "***" in res["station"]["contacts"]["phone"]  # phone masked
    manual = firestations.compute_route(inc["id"], router, "st-Near but slow")
    assert manual["selected_manually"] and manual["station"]["name"] == "Near but slow"


def test_route_reports_unavailable_routing_and_no_stations_honestly():
    far = new_incident(lat=-60.0, lon=-120.0)
    assert firestations.compute_route(far["id"], FakeRouter({}))["status"] == "NO_STATIONS"
    near = new_incident(lat=30.0, lon=70.0)
    off = SimpleNamespace(available=False)
    assert firestations.compute_route(near["id"], off)["status"] == "ROUTING_UNAVAILABLE"


# ------------------------------------------------------------------ impact
def test_carbon_needs_inputs_and_is_labelled_illustrative_in_demo():
    c = fireimpact.carbon(100, 12, [0.2, 0.5], "supplied by the operator", True)
    assert c["evidence_class"] == "modelled" and c["tonnes_co2"]["low"] < c["tonnes_co2"]["central"] < c["tonnes_co2"]["high"]
    assert c["tonnes_co2"]["central"] == pytest.approx(100 * 12 * 0.35 * 1.643, rel=0.01) and "CO2 only" in c["pollutant"] and "wider" in c["uncertainty"]
    real = new_incident()
    import app.services.fireimpact as fi

    fi.nearby_places = lambda *a, **k: {"status": "unavailable", "reason": "test"}
    fi.air_quality = lambda *a, **k: {"status": "unavailable", "reason": "test"}
    out = fi.build(real["id"])
    assert out["carbon"]["status"] == "NOT_ENOUGH_INFORMATION" and "hotspot counts" in out["carbon"]["reason"]
    assert out["observed"]["burned_area_ha"] is None and "Not available" in out["observed"]["actual_fire_perimeter"]
    given = fi.build(real["id"], 50.0, 10.0)
    assert given["carbon"]["status"] == "CALCULATED" and given["carbon"]["evidence_class"] == "modelled"


# ------------------------------------------------------------------ Telegram
def configure_telegram(monkeypatch, chat="555"):
    monkeypatch.setattr(settings, "telegram_bot_token", "TESTTOKEN")
    monkeypatch.setattr(settings, "telegram_chat_id", chat)


def test_telegram_gates_simulation_real_send_and_duplicates(monkeypatch):
    inc = new_incident(demo=True, status="DISPATCH_REQUESTED")
    iid = inc["id"]
    monkeypatch.setattr(firecomms, "build_attachments", lambda i, h="12": [{"kind": "incident", "label": "Incident map", "filename": "a.jpg", "bytes": b"jpg", "mime": "image/jpeg", "basemap": "mapbox"}, {"kind": "report", "label": "Report", "filename": "r.pdf", "bytes": b"%PDF", "mime": "application/pdf"}])
    sim = firecomms.send_incident(iid, "simulated", False, "admin")
    assert [e["status"] for e in sim["entries"]] == ["SIMULATED"] * 3 and "Test scenario" in sim["entries"][0]["preview"] and "delivery does not mean" in sim["entries"][0]["preview"]
    assert firecomms.send_incident(iid, "simulated", False, "admin")["duplicate_suppressed"] is True
    with pytest.raises(fireops.WorkflowError, match="not configured"):
        firecomms.send_incident(iid, "real", True, "admin")
    configure_telegram(monkeypatch)
    with pytest.raises(fireops.WorkflowError, match="confirmation"):
        firecomms.send_incident(iid, "real", False, "admin")
    calls = []

    def fake_call(method, data=None, files=None, client=None):
        calls.append((method, data))
        if method == "sendPhoto":
            raise firecomms.TelegramError("Bad Request: chat not found", 400)
        return {"message_id": 100 + len(calls)}

    monkeypatch.setattr(firecomms, "_call", fake_call)
    real = firecomms.send_incident(iid, "real", True, "admin")
    st = {e["kind"]: e for e in real["entries"]}
    assert st["message"]["status"] == "SENT" and st["message"]["message_id"] == 101 and st["incident"]["status"] == "FAILED" and st["report"]["status"] == "SENT"
    assert "TESTTOKEN" not in str(fireops.events(iid)) and any(e["event_type"] == "telegram_attachment_failed" and e["status"] == "failed" for e in fireops.events(iid))
    assert firecomms.send_incident(iid, "real", True, "admin")["duplicate_suppressed"] is True  # same incident is not sent twice
    assert calls[0][0] == "sendMessage" and "inline_keyboard" in calls[0][1]["reply_markup"] and "chat_id" in calls[0][1]


def test_telegram_refused_before_authorisation_and_failure_is_recorded(monkeypatch):
    inc = new_incident(demo=True, status="CONFIRMED")
    with pytest.raises(fireops.WorkflowError):
        firecomms.send_incident(inc["id"], "simulated", False, "admin")
    assert any(e["event_type"] == "telegram_refused" for e in fireops.events(inc["id"]))
    inc = fireops.transition(inc["id"], "DISPATCH_REQUESTED", "admin")
    configure_telegram(monkeypatch)
    monkeypatch.setattr(firecomms, "build_attachments", lambda i, h="12": [])
    monkeypatch.setattr(firecomms, "_call", lambda *a, **k: (_ for _ in ()).throw(firecomms.TelegramError("Unauthorized", 401)))
    r = firecomms.send_incident(inc["id"], "real", True, "admin")
    assert r["entries"][0]["status"] == "FAILED"
    assert fireops.get(inc["id"])["status"] == "DISPATCH_REQUESTED"  # a failed send never advances the incident
    assert any(e["event_type"] == "telegram_failed" and e["error"] == "Unauthorized" for e in fireops.events(inc["id"]))


def test_telegram_buttons_only_work_for_authorised_chats_and_ack_is_not_arrival(monkeypatch):
    configure_telegram(monkeypatch, chat="555")
    monkeypatch.setattr(firecomms, "_call", lambda *a, **k: {})
    inc = new_incident(demo=True, status="DISPATCH_REQUESTED")
    cb = lambda chat: {"callback_query": {"id": "1", "data": f"fa:{inc['id']}", "from": {"first_name": "Asha"}, "message": {"chat": {"id": chat}}}}  # noqa: E731
    assert firecomms.handle_update(cb(999))["handled"] is False  # a forwarded button in some other chat does nothing
    assert fireops.get(inc["id"])["status"] == "DISPATCH_REQUESTED"
    assert firecomms.handle_update(cb(555))["handled"] is True
    after = fireops.get(inc["id"])
    assert after["status"] == "DISPATCHED" and "not station arrival" in after["notify"]["acknowledged"]["meaning"]
    assert any(e["event_type"] == "telegram_unauthorised" for e in fireops.events(None, limit=50))


# ------------------------------------------------------------------ IVR
def test_ivr_simulated_is_labelled_and_real_is_gated(monkeypatch):
    inc = new_incident(demo=True, status="DISPATCH_REQUESTED")
    e = fireivr.place_call(inc["id"], "simulated", False, "admin")
    assert e["simulated"] is True and "test scenario" not in e["script"].lower() and "forest fire has been detected" in e["script"]
    time.sleep(5.5)
    calls = fireops.get(inc["id"])["ivr"]["calls"]
    assert [h["status"] for h in calls[0]["history"]] == ["QUEUED", "INITIATED", "RINGING", "ANSWERED", "COMPLETED"]
    assert all(ev["message"].startswith("[SIMULATED]") for ev in fireops.events(inc["id"], services=["ivr"]))
    monkeypatch.setattr(settings, "ivr_provider", "something_else")
    with pytest.raises(fireops.WorkflowError, match="omnidimension"):
        fireivr.place_call(inc["id"], "real", True, "admin")
    monkeypatch.setattr(settings, "ivr_provider", "twilio")
    with pytest.raises(fireops.WorkflowError, match="missing"):
        fireivr.place_call(inc["id"], "real", True, "admin")
    for k, v in (("twilio_account_sid", "ACx"), ("twilio_auth_token", "tok"), ("twilio_from_number", "+10000000000"), ("ivr_response_number", "+919999999999"), ("ivr_approved_numbers", "+911111111111")):
        monkeypatch.setattr(settings, k, v)
    with pytest.raises(fireops.WorkflowError, match="not in IVR_APPROVED_NUMBERS"):
        fireivr.place_call(inc["id"], "real", True, "admin")
    monkeypatch.setattr(settings, "ivr_approved_numbers", "+919999999999")
    with pytest.raises(fireops.WorkflowError, match="confirmation"):
        fireivr.place_call(inc["id"], "real", False, "admin")


def test_ivr_real_call_status_comes_from_the_provider_and_retries_are_bounded(monkeypatch):
    inc = new_incident(demo=True, status="DISPATCH_REQUESTED")
    for k, v in (("ivr_provider", "twilio"), ("twilio_account_sid", "ACx"), ("twilio_auth_token", "tok"), ("twilio_from_number", "+10000000000"), ("ivr_response_number", "+919999999999"), ("ivr_approved_numbers", "+919999999999")):
        monkeypatch.setattr(settings, k, v)
    monkeypatch.setattr(fireivr.time, "sleep", lambda s: None)
    monkeypatch.setattr(fireivr.threading, "Thread", lambda target, args, daemon: SimpleNamespace(start=lambda: None))
    posts = []

    class Fake:
        def __init__(self, *a, **k): pass
        def post(self, url, **k):
            posts.append(url)
            return httpx.Response(503, text="busy", request=httpx.Request("POST", url))

    monkeypatch.setattr(fireivr.httpx, "Client", Fake)
    e = fireivr.place_call(inc["id"], "real", True, "admin")
    assert e["status"] == "FAILED" and len(posts) == 3 and len(e["attempts"]) == 3  # first try + 2 retries, then stop
    assert sum(1 for ev in fireops.events(inc["id"]) if ev["event_type"] == "ivr_retry") == 2
    assert "ANSWERED" not in [h["status"] for h in e["history"]]

    class Ok(Fake):
        def post(self, url, **k):
            return httpx.Response(201, json={"sid": "CA123"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(fireivr.httpx, "Client", Ok)
    e2 = fireivr.place_call(inc["id"], "real", True, "admin")
    assert e2["status"] == "INITIATED" and e2["provider_call_id"] == "CA123"  # accepted, not answered: that needs the provider to say so


# ------------------------------------------------------------------ API
def prime_cache(lat=18.5, lon=79.5, hours_ago=2):
    epoch = int((datetime.now(timezone.utc) - timedelta(hours=hours_ago)).timestamp())
    body = {"source": "VIIRS_SNPP_NRT", "source_name": "VIIRS S-NPP (375 m)", "fetched_at": datetime.now(timezone.utc).isoformat(), "fires": [[lat, lon, epoch, 0.9, 12.5, "N", "D"], [lat + 0.01, lon + 0.01, epoch + 60, 0.6, 3.0, "N", "D"]]}
    live_routes._cache[("VIIRS_SNPP_NRT", 1, True)] = {"at": time.time(), "body": body}
    return epoch


def test_api_create_from_real_feed_only_and_workflow_permissions():
    epoch = prime_cache()
    ok = client.post("/api/v1/fire/incidents", headers=ADMIN, json={"source": "firms", "lat": 18.5, "lon": 79.5, "observed_epoch_s": epoch})
    assert ok.status_code == 200
    i = ok.json()["incident"]
    assert i["detection"]["currency"] == "current" and i["detection"]["frp_mw"] == 12.5 and len(i["detection"]["hotspot_history"]) == 2 and i["status"] == "NEEDS_VERIFICATION"
    assert client.post("/api/v1/fire/incidents", headers=ADMIN, json={"source": "firms", "lat": 18.5, "lon": 79.5, "observed_epoch_s": epoch}).json()["created"] is False
    assert client.post("/api/v1/fire/incidents", headers=ADMIN, json={"source": "firms", "lat": 10.0, "lon": 10.0, "observed_epoch_s": 5}).status_code == 422
    old = prime_cache(lat=17.0, lon=78.0, hours_ago=30)
    h = client.post("/api/v1/fire/incidents", headers=ADMIN, json={"source": "firms", "lat": 17.0, "lon": 78.0, "observed_epoch_s": old}).json()["incident"]
    assert h["detection"]["currency"] == "historical"
    iid = i["id"]
    assert client.post(f"/api/v1/fire/incidents/{iid}/authorise", headers=ADMIN, json={"note": ""}).status_code == 409
    assert client.post(f"/api/v1/fire/incidents/{iid}/verify", headers={"X-Role": "read_only"}, json={"decision": "confirm"}).status_code == 403
    assert client.post(f"/api/v1/fire/incidents/{iid}/verify", headers={"X-Role": "reviewer"}, json={"decision": "confirm", "note": "ranger saw smoke"}).json()["status"] == "CONFIRMED"
    assert client.post(f"/api/v1/fire/incidents/{iid}/authorise", headers={"X-Role": "analyst"}, json={"note": ""}).status_code == 403  # analysts cannot approve a response
    assert client.post(f"/api/v1/fire/incidents/{iid}/authorise", headers=ADMIN, json={"note": ""}).json()["status"] == "DISPATCH_REQUESTED"
    assert client.post(f"/api/v1/fire/incidents/{iid}/notify/telegram", headers=ADMIN, json={"mode": "real"}).status_code == 409  # no confirm flag
    assert client.post(f"/api/v1/fire/incidents/{iid}/ivr", headers=ADMIN, json={"mode": "real", "confirm_real": True}).status_code == 409  # provider not configured
    ack = client.post(f"/api/v1/fire/incidents/{iid}/acknowledge", headers={"X-Role": "officer"}, json={"note": "radio"}).json()
    assert ack["status"] == "DISPATCHED" and ack["notify"]["acknowledged"]["meaning"].startswith("Recorded by an operator")
    assert client.post(f"/api/v1/fire/incidents/{iid}/field-update", headers={"X-Role": "officer"}, json={"note": "fire contained to 2 ha"}).json()["status"] == "MONITORING"
    assert client.post(f"/api/v1/fire/incidents/{iid}/resolve", headers=ADMIN, json={"note": "out"}).json()["status"] == "RESOLVED"
    ev = client.get("/api/v1/fire/events", params={"incident_id": iid, "types": "status_changed"}, headers=ADMIN).json()
    assert [e["details"]["to"] for e in ev["events"]] == ["NEEDS_VERIFICATION", "CONFIRMED", "DISPATCH_REQUESTED", "DISPATCHED", "MONITORING", "RESOLVED"]
    assert client.get("/api/v1/fire/events", params={"since_id": ev["last_id"], "incident_id": iid}, headers=ADMIN).json()["events"] == []


def test_api_manual_report_demo_controls_and_real_incidents_survive_reset(monkeypatch):
    man = client.post("/api/v1/fire/incidents", headers=ADMIN, json={"source": "manual", "lat": 12.0, "lon": 77.0, "note": "smoke seen from the road"}).json()["incident"]
    assert man["source"] == "manual" and man["detection"]["currency"] == "n/a"
    from app.services import firesvc

    client.post("/api/v1/fire/demo/reset", headers=ADMIN)  # earlier tests may have left demo incidents open
    monkeypatch.setattr(firesvc, "run_forecast", lambda iid, actor, spec=None: fireops.patch(iid, forecast={"status": "OK", "horizons": {}, "demo": True}))
    monkeypatch.setattr(firestations, "discover", lambda *a, **k: {"added": 0})
    monkeypatch.setattr(firestations, "compute_route", lambda *a, **k: {"status": "NO_STATIONS"})
    monkeypatch.setattr(fire_routes.fireimpact, "build", lambda *a, **k: {})
    r = client.post("/api/v1/fire/demo/run", headers=ADMIN).json()
    assert r["created"] and r["incident"]["is_demo"] and r["incident"]["code"].startswith("SC-") and r["incident"]["detection"]["synthetic"] is True
    again = client.post("/api/v1/fire/demo/run", headers=ADMIN).json()
    assert again["created"] is False and again["incident"]["id"] == r["incident"]["id"]
    for _ in range(50):
        job = client.get(f"/api/v1/jobs/{r['job_id']}", headers=ADMIN).json()
        if job["status"] in ("completed", "failed"):
            break
        time.sleep(0.1)
    assert job["status"] == "completed" and job["result"]["status"] == "CONFIRMED"
    assert client.post("/api/v1/fire/demo/stop", headers=ADMIN).json()["was_running"] in (True, False)
    cfg = client.put("/api/v1/fire/demo/config", headers=ADMIN, json={"lat": 22.0, "lon": 78.5, "wind_kmh": 30, "wind_from_deg": 200}).json()
    assert cfg["lat"] == 22.0 and client.get("/api/v1/fire/demo/config", headers=ADMIN).json()["weather"]["wind_kmh"] == 30
    reset = client.post("/api/v1/fire/demo/reset", headers=ADMIN).json()
    assert reset["incidents_removed"] >= 1 and man["id"] in [i["id"] for i in client.get("/api/v1/fire/incidents", params={"include_demo": False}, headers=ADMIN).json()]
    assert not [i for i in client.get("/api/v1/fire/incidents", headers=ADMIN).json() if i["is_demo"]]


def test_api_status_reports_unconfigured_integrations_honestly():
    s = client.get("/api/v1/fire/status", headers=ADMIN).json()
    assert s["ivr"]["provider"] == "simulated" and s["ivr"]["keypad_acknowledgement"] is False and s["telegram"]["ready_to_send"] is False
    assert s["firms"]["configured"] is False and s["forecast_model"]["status"].startswith("prototype")


def test_omnidimension_call_is_dispatched_with_the_message_and_followed_by_its_own_status(monkeypatch):
    inc = new_incident(demo=True, status="DISPATCH_REQUESTED")
    for k, v in (("ivr_provider", "omnidimension"), ("omnidim_api_key", "key-123"), ("omnidim_agent_id", 42), ("omnidim_from_number_id", 7), ("ivr_response_number", "+919999999999"), ("ivr_approved_numbers", "")):
        monkeypatch.setattr(settings, k, v)
    st = fireivr.status()
    assert st["real_ready"] and st["missing"] == [] and st["number_approved"]      # the configured number is the allowed destination
    sent = {}

    class Fake:
        def __init__(self, *a, **k): pass
        def post(self, url, **k):
            sent.update(url=url, **k)
            return httpx.Response(200, json={"success": True, "status": "dispatched", "requestId": 77}, request=httpx.Request("POST", url))
        def get(self, url, **k):
            rows = [{"call_request_id": {"id": 76, "type_of_request": "Call"}, "call_status": "failed"}, {"call_request_id": {"id": 77, "type_of_request": "Call"}, "call_status": "completed", "call_duration_in_seconds": "14.0"}]
            return httpx.Response(200, json={"call_log_data": rows}, request=httpx.Request("GET", url))

    monkeypatch.setattr(fireivr.httpx, "Client", Fake)
    monkeypatch.setattr(fireivr.threading, "Thread", lambda target, args, daemon: SimpleNamespace(start=lambda: None))
    e = fireivr.place_call(inc["id"], "real", True, "admin")
    assert e["status"] == "INITIATED" and e["provider"] == "omnidimension" and e["provider_call_id"] == "77"
    assert sent["url"].endswith("/calls/dispatch") and sent["headers"]["Authorization"] == "Bearer key-123"
    body = sent["json"]
    assert body["agent_id"] == 42 and body["to_number"] == "+919999999999" and body["from_number_id"] == 7
    assert "forest fire has been detected" in body["call_context"]["message"] and body["call_context"]["reference"] == inc["code"]
    assert fireivr.remote_status("omnidimension", "77") == "COMPLETED" and fireivr.remote_status("omnidimension", "76") == "FAILED"
    monkeypatch.setattr(settings, "omnidim_api_key", None)
    assert "OMNIDIM_API_KEY" in fireivr.status()["missing"] and not fireivr.status()["real_ready"]


def test_choosing_one_station_keeps_the_other_candidates_listed():
    prev = [{"station_id": "a", "selected": True, "distance_km": 40}, {"station_id": "b", "selected": False, "distance_km": 20}, {"station_id": "c", "selected": False, "distance_km": 41}]
    fresh = [{"station_id": "b", "selected": True, "distance_km": 20.35}]
    out = firestations._merge_candidates(fresh, prev)
    assert [c["station_id"] for c in out] == ["a", "b", "c"]
    assert [c["selected"] for c in out] == [False, True, False] and out[1]["distance_km"] == 20.35
    assert firestations._merge_candidates(fresh, None) == fresh


def test_omnidimension_short_connection_is_not_reported_as_answered(monkeypatch):
    for k, v in (("ivr_provider", "omnidimension"), ("omnidim_api_key", "k")):
        monkeypatch.setattr(settings, k, v)

    class Fake:
        def get(self, url, **kw):
            return httpx.Response(200, json={"call_log_data": [{"call_request_id": {"id": 5}, "call_status": "completed", "call_duration_in_seconds": "1.0", "hangup_reason": "Client disconnected"}]}, request=httpx.Request("GET", url))

    d = fireivr.remote_detail("omnidimension", "5", Fake())
    assert d["status"] == "NO_ANSWER" and d["duration_s"] == 1


def test_a_false_alarm_can_be_undone_but_a_resolved_incident_stays_closed():
    inc = new_incident(demo=True)
    iid = inc["id"]
    assert client.post(f"/api/v1/fire/incidents/{iid}/verify", headers={"X-Role": "reviewer"}, json={"decision": "reject", "note": "False alarm"}).json()["status"] == "REJECTED"
    back = client.post(f"/api/v1/fire/incidents/{iid}/verify", headers={"X-Role": "reviewer"}, json={"decision": "needs_verification", "note": "Reopened"}).json()
    assert back["status"] == "NEEDS_VERIFICATION" and back["closed_at"] is None
    inc2 = new_incident(demo=True, status="CONFIRMED")
    for target in ("MONITORING", "RESOLVED"):
        fireops.transition(inc2["id"], target, "admin")
    with pytest.raises(fireops.WorkflowError):
        fireops.transition(inc2["id"], "NEEDS_VERIFICATION", "admin")
