import json
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest
from affine import Affine
from fastapi.testclient import TestClient

from app.db.database import connection, insert, now
from app.main import app
from app.services import alerts, boundary
from app.services import forestwatch as fw

client = TestClient(app)
ADMIN = {"X-Role": "admin"}
REVIEWER = {"X-Role": "reviewer"}
SQUARE = {"type": "Polygon", "coordinates": [[[81.0, 21.7], [81.003, 21.7], [81.003, 21.703], [81.0, 21.703], [81.0, 21.7]]]}
TODAY = datetime.now(timezone.utc).date()


@pytest.fixture(autouse=True)
def in_india(monkeypatch):
    monkeypatch.setattr(boundary, "contains", lambda lat, lon: True)


def patch_dict(geom=SQUARE, area=1.0, est=None, unc=6.0):
    return {"geometry": geom, "area_ha": area, "est_date": (est or TODAY - timedelta(days=10)).isoformat(), "date_uncertainty_days": unc, "issue_date": TODAY.isoformat(), "evidence": "optical_radar_agree", "score": 1.0, "evidence_mix": {"optical_radar_agree": 12}, "pixels": [[0], [0]]}


def new_record(**kw):
    fw._ingest(str(uuid.uuid4()), [patch_dict(**kw)], "test")
    with connection() as conn:
        return dict(conn.execute("SELECT id FROM fc_records ORDER BY created_at DESC LIMIT 1").fetchone())["id"]


def shifted(dx):
    return {"type": "Polygon", "coordinates": [[[x + dx, y] for x, y in SQUARE["coordinates"][0]]]}


def test_validation_limits_area_dates_and_country(monkeypatch):
    ok, start, end = fw.validate([81.0, 21.7, 81.05, 21.75], 30, None, None)
    assert (end - start).days == 30 and ok[0] == 81.0
    with pytest.raises(fw.WatchError, match="km²"):
        fw.validate([80.0, 21.0, 82.0, 23.0], 30, None, None)
    with pytest.raises(fw.WatchError, match="future"):
        fw.validate([81.0, 21.7, 81.05, 21.75], None, TODAY, TODAY + timedelta(days=2))
    with pytest.raises(fw.WatchError, match="days"):
        fw.validate([81.0, 21.7, 81.05, 21.75], None, TODAY - timedelta(days=400), TODAY)
    monkeypatch.setattr(boundary, "contains", lambda lat, lon: False)
    with pytest.raises(fw.WatchError, match="outside India"):
        fw.validate([0.0, 0.0, 0.05, 0.05], 30, None, None)


def test_redaction_hides_tokens_keys_phones_and_emails():
    t = fw.redact("call +91 98765 43210 mail a.b@x.org url https://x/y?key=SECRET123&z=1 bot 123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    assert "98765" not in t and "a.b@x.org" not in t and "SECRET123" not in t and "AAAAAAAA" not in t and "…210" in t
    d = fw.redact({"auth_token": "abc", "n": ["+919876543210"]})
    assert d["auth_token"] == "[hidden]" and "9876" not in json.dumps(d)


def test_timeline_shows_cover_shrinking_and_loss_growing():
    grid = SimpleNamespace(transform=Affine(30, 0, 500000, 0, -30, 2400000), crs="EPSG:32644")
    h = w = 40
    forest = np.ones((h, w), dtype=bool)
    est = np.full((h, w), np.nan)
    end = TODAY
    end_day = (end - alerts.EPOCH).days
    est[0:5, 0:10] = end_day - 60     # lost 60 days ago
    est[10:15, 0:10] = end_day - 20   # lost 20 days ago
    est[20:25, 0:10] = end_day - 3    # lost 3 days ago
    ys, xs = np.nonzero(~np.isnan(est))
    tl = fw._timeline({"forest": forest, "est_day": est}, [{"pixels": [ys.tolist(), xs.tolist()]}], grid, end, 90)
    by = {s["days_ago"]: s for s in tl}
    assert [s["days_ago"] for s in tl] == [90, 30, 7, 0]
    assert by[90]["lost_ha"] == 0 and by[30]["lost_ha"] > 0 and by[7]["lost_ha"] > by[30]["lost_ha"] and by[0]["lost_ha"] > by[7]["lost_ha"]
    assert by[90]["forest_ha"] > by[30]["forest_ha"] > by[7]["forest_ha"] > by[0]["forest_ha"]
    assert by[90]["lost"] is None and by[0]["lost"]["type"] in ("Polygon", "MultiPolygon")
    short = fw._timeline({"forest": forest, "est_day": est}, [{"pixels": [ys.tolist(), xs.tolist()]}], grid, end, 20)
    assert [s["days_ago"] for s in short] == [7, 0]


def test_records_are_deduplicated_across_scans():
    run = str(uuid.uuid4())
    geom = shifted(0.01)
    assert fw._ingest(run, [patch_dict(geom)], "t") == (1, 0)
    assert fw._ingest(str(uuid.uuid4()), [patch_dict(geom)], "t") == (0, 1)
    assert fw._ingest(str(uuid.uuid4()), [patch_dict(shifted(0.5))], "t") == (1, 0)
    rows = [r for r in fw.query(start=TODAY - timedelta(days=30)) if abs(r["lon"] - 81.0015 - 0.01) < 1e-3]
    assert len(rows) == 1 and rows[0]["seen_count"] == 2


def test_five_record_classes():
    # no permit anywhere -> not proof of anything, just "no record found"
    a = new_record(geom=shifted(1.0))
    assert fw.verify(a)["class"] == "NO_RECORD_FOUND" and "does not mean" in fw.VERIFICATION["NO_RECORD_FOUND"][1]
    # weak evidence
    b = new_record(geom=shifted(1.1), unc=60.0)
    assert fw.verify(b)["class"] == "INSUFFICIENT_EVIDENCE"
    # valid permit covering it
    c = new_record(geom=shifted(1.2))
    permit = {"type": "Polygon", "coordinates": [[[82.19, 21.69], [82.21, 21.69], [82.21, 21.71], [82.19, 21.71], [82.19, 21.69]]]}
    insert("authorizations", {"id": str(uuid.uuid4()), "authority": "Test Division", "project_identifier": "P-1", "project_name": "Road", "status": "issued", "geometry": permit, "dates": {"valid_from": "2020-01-01T00:00:00+00:00", "valid_to": "2099-01-01T00:00:00+00:00"}, "source_url": None, "evidence": {}, "created_at": now()})
    assert fw.verify(c)["class"] == "AUTHORIZED_RECORD_MATCH"
    # expired permit
    d = new_record(geom=shifted(1.4))
    insert("authorizations", {"id": str(uuid.uuid4()), "authority": "Test Division", "project_identifier": "P-2", "project_name": "Mine", "status": "issued", "geometry": {"type": "Polygon", "coordinates": [[[82.39, 21.69], [82.41, 21.69], [82.41, 21.71], [82.39, 21.71], [82.39, 21.69]]]}, "dates": {"valid_from": "2010-01-01T00:00:00+00:00", "valid_to": "2012-01-01T00:00:00+00:00"}, "source_url": None, "evidence": {}, "created_at": now()})
    assert fw.verify(d)["class"] == "EXPIRED_OR_REVOKED_RECORD"
    # permit still pending -> unclear
    e = new_record(geom=shifted(1.6))
    insert("authorizations", {"id": str(uuid.uuid4()), "authority": "Test Division", "project_identifier": "P-3", "project_name": "Quarry", "status": "pending", "geometry": {"type": "Polygon", "coordinates": [[[82.59, 21.69], [82.61, 21.69], [82.61, 21.71], [82.59, 21.71], [82.59, 21.69]]]}, "dates": {}, "source_url": None, "evidence": {}, "created_at": now()})
    assert fw.verify(e)["class"] == "PARTIAL_OR_UNCLEAR_RECORD"


def test_review_edit_and_status_rules():
    rid = new_record(geom=shifted(3.0))
    r = client.patch(f"/api/v1/forest-change/records/{rid}", headers=REVIEWER, json={"status": "CONFIRMED", "category": "suspected_felling", "notes": "seen on road side"})
    assert r.status_code == 200 and r.json()["status"] == "CONFIRMED" and r.json()["category"] == "suspected_felling"
    assert any("status" in h["change"] for h in r.json()["history"])
    assert client.patch(f"/api/v1/forest-change/records/{rid}", headers=REVIEWER, json={"status": "REPORTED"}).status_code == 422
    assert client.patch(f"/api/v1/forest-change/records/{rid}", headers=REVIEWER, json={"category": "made_up"}).status_code == 422
    assert client.patch(f"/api/v1/forest-change/records/{rid}", headers={"X-Role": "read_only"}, json={"notes": "x"}).status_code == 403
    gone = client.patch(f"/api/v1/forest-change/records/{rid}", headers=REVIEWER, json={"status": "FALSE_POSITIVE"}).json()
    assert gone["status"] == "FALSE_POSITIVE"
    ids = [x["id"] for x in client.get("/api/v1/forest-change/records?days=90", headers=ADMIN).json()]
    assert rid not in ids
    assert rid in [x["id"] for x in client.get("/api/v1/forest-change/records?days=90&include_false=true", headers=ADMIN).json()]


def test_notifications_need_confirmation_then_simulate_and_never_pretend():
    rid = new_record(geom=shifted(4.0))
    url = f"/api/v1/forest-change/records/{rid}/notify"
    assert client.post(url, headers=ADMIN, json={"channels": ["telegram"]}).status_code == 422          # not confirmed by a person
    client.patch(f"/api/v1/forest-change/records/{rid}", headers=REVIEWER, json={"status": "CONFIRMED"})
    assert client.post(url, headers={"X-Role": "analyst"}, json={"channels": ["telegram"]}).status_code == 403    # analysts cannot send
    sim = client.post(url, headers=ADMIN, json={"channels": ["telegram", "ivr", "email", "dashboard"]}).json()
    by = {x["channel"]: x for x in sim["results"]}
    assert by["telegram"]["status"] == "SIMULATED" and by["ivr"]["status"] == "SIMULATED" and by["email"]["status"] == "SIMULATED" and by["dashboard"]["status"] == "SENT"
    assert "not a finding" in by["telegram"]["preview"] or "not a finding" in by["telegram"]["preview"].lower()
    assert sim["record"]["status"] == "CONFIRMED"                                                         # practice never marks it reported
    dup = client.post(url, headers=ADMIN, json={"channels": ["telegram"]}).json()
    assert dup["results"][0]["status"] == "DUPLICATE_SUPPRESSED"
    assert client.post(url, headers=ADMIN, json={"channels": ["telegram"], "mode": "real"}).status_code == 422   # real needs confirm
    real = client.post(url, headers=ADMIN, json={"channels": ["telegram", "email", "ivr"], "mode": "real", "confirm_real": True}).json()
    assert all(x["status"] in ("NOT_CONFIGURED", "REFUSED") for x in real["results"])                      # keyless test server: honest failure
    assert real["record"]["status"] == "CONFIRMED"
    evs = client.get(f"/api/v1/forest-change/events?record_id={rid}", headers=ADMIN).json()
    kinds = {e["event_type"] for e in evs}
    assert {"telegram_simulated", "telegram_not_configured", "notify_refused", "email_not_configured"} <= kinds
    assert all("@" not in e["message"] for e in evs)


def test_authority_lookup_handles_missing_offices_and_missing_routing():
    rid = new_record(geom=shifted(5.0))
    r = client.post(f"/api/v1/forest-change/records/{rid}/authority", headers=REVIEWER, json={}).json()
    assert r["status"] == "NO_AUTHORITY_FOUND"
    insert("stations", {"id": str(uuid.uuid4()), "name": "Test Forest Range Office", "jurisdiction": "unknown", "location": {"type": "Point", "coordinates": [81.5, 21.75]}, "coverage": None, "contacts": {"phone": "+911234567890"}, "availability_note": None, "created_at": now(), "kind": "forest_office", "source": "osm", "verified": 0, "external_ref": "node/" + rid})
    rid2 = new_record(geom=shifted(0.3))
    r2 = client.post(f"/api/v1/forest-change/records/{rid2}/authority", headers=REVIEWER, json={}).json()
    assert r2["status"] == "ROUTING_UNAVAILABLE" and r2["office"]["name"] == "Test Forest Range Office" and "…890" == r2["office"]["phone"]
    full = client.get(f"/api/v1/forest-change/records/{rid2}", headers=ADMIN).json()
    assert full["authority"]["office"]["verified"] is False


def test_scan_endpoint_validates_and_status_reports_channels():
    r = client.post("/api/v1/forest-change/runs", headers=ADMIN, json={"bbox": [78.0, 20.0, 80.0, 22.0]})
    assert r.status_code == 422 and "km²" in r.json()["detail"]
    assert client.post("/api/v1/forest-change/runs", headers={"X-Role": "read_only"}, json={"bbox": [81.0, 21.7, 81.05, 21.75]}).status_code == 403
    s = client.get("/api/v1/forest-change/status", headers=ADMIN).json()
    assert s["history_days"] == 90 and s["channels"]["telegram"]["ready"] is False and "NO_RECORD_FOUND" in s["verification_classes"]
    assert client.get("/api/v1/forest-change/timeline", headers=ADMIN).status_code == 200
    assert client.get("/api/v1/forest-change/export.geojson", headers=ADMIN).json()["type"] == "FeatureCollection"


def test_drawn_areas_are_limited_by_their_own_area_and_pick_a_sensible_resolution():
    def square(km):   # a square of about km x km near 21.7 N
        d = km / 111.32 / np.cos(np.radians(21.7)) , km / 110.54
        return {"type": "Polygon", "coordinates": [[[81.0, 21.7], [81.0 + d[0], 21.7], [81.0 + d[0], 21.7 + d[1]], [81.0, 21.7 + d[1]], [81.0, 21.7]]]}

    small, wide, huge = square(7), square(14), square(20)
    for poly, ok in ((small, True), (wide, True), (huge, False)):
        w, s, e, n = *poly["coordinates"][0][0], *poly["coordinates"][0][2]
        if ok:
            fw.validate([w, s, e, n], 30, None, None, poly)
        else:
            with pytest.raises(fw.WatchError, match="km²"):
                fw.validate([w, s, e, n], 30, None, None, poly)
    assert fw.resolution_for(fw.polygon_area_km2(small)) == 30.0 and fw.resolution_for(fw.polygon_area_km2(wide)) == 60.0
    with pytest.raises(fw.WatchError, match="valid area"):
        fw.validate([81.0, 21.7, 81.1, 21.8], 30, None, None, {"type": "Point", "coordinates": [81.0, 21.7]})


def test_case_studies_show_forest_shrinking_and_effects_building_up():
    from app.services import casestudies
    ids = [c["id"] for c in client.get("/api/v1/forest-change/cases", headers=ADMIN).json()]
    assert len(ids) == 3
    for cid in ids:
        c = client.get(f"/api/v1/forest-change/cases/{cid}", headers=ADMIN).json()
        keys = [s["key"] for s in c["stages"]]
        assert keys == ["before", "clearing", "building", "running"] and "not a record of a real company" in c["notice"]
        forest = [s["metrics"]["forest_ha"] for s in c["stages"]]
        assert forest[0] > forest[1] and forest[1] == forest[3] and c["stages"][0]["metrics"]["lost_ha"] == 0
        run = c["stages"][3]
        assert run["layers"]["plumes"] and len(run["layers"]["plumes"]) == 3 and run["layers"]["effluent"] and run["layers"]["chimneys"]
        assert c["stages"][0]["layers"].get("plant") is None and c["stages"][2]["layers"]["plant"]
        assert {e["area"] for e in c["effects"]} == {"Forest", "Habitat", "Wildlife corridor", "Water", "Air", "Carbon"}
        assert run["metrics"]["carbon_t_co2"]["low"] < run["metrics"]["carbon_t_co2"]["central"] < run["metrics"]["carbon_t_co2"]["high"]
    steel = casestudies.build("steel-sal")
    assert steel["stages"][3]["metrics"]["patches"] == 2      # the road across the forest splits it in two
    assert client.get("/api/v1/forest-change/cases/nope", headers=ADMIN).status_code == 404


def test_permits_show_their_exact_details_and_can_be_added_and_removed_by_hand():
    rid = new_record(geom=shifted(7.0))
    assert fw.verify(rid)["permits"] == []
    body = {"authority": "State Forest Department", "permit_number": "FC/2026/114", "project_name": "Quarry road", "status": "issued", "valid_from": "2026-01-01", "valid_to": "2027-01-01", "source_url": "https://example.gov.in/p/114"}
    r = client.post(f"/api/v1/forest-change/records/{rid}/permits", headers=REVIEWER, json=body)
    assert r.status_code == 200
    ver = r.json()
    p = ver["permits"][0]
    assert ver["class"] == "AUTHORIZED_RECORD_MATCH" and p["number"] == "FC/2026/114" and p["authority"] == "State Forest Department" and p["project"] == "Quarry road"
    assert p["valid_from"] == "2026-01-01" and p["valid_to"] == "2027-01-01" and p["source_url"] == body["source_url"] and p["entered_by"] == "reviewer" and p["overlap"] >= 0.95
    assert client.post(f"/api/v1/forest-change/records/{rid}/permits", headers=REVIEWER, json={**body, "permit_number": ""}).status_code == 422
    assert client.post(f"/api/v1/forest-change/records/{rid}/permits", headers=REVIEWER, json={**body, "source_url": "javascript:alert(1)"}).status_code == 422
    assert client.post(f"/api/v1/forest-change/records/{rid}/permits", headers={"X-Role": "read_only"}, json=body).status_code == 403
    back = client.delete(f"/api/v1/forest-change/records/{rid}/permits/{p['id']}", headers=REVIEWER)
    assert back.status_code == 200 and back.json()["permits"] == [] and back.json()["class"] == "NO_RECORD_FOUND"
    # a permit that was not entered by hand (loaded from a register) cannot be deleted from here
    other = str(uuid.uuid4())
    insert("authorizations", {"id": other, "authority": "Register", "project_identifier": "R-1", "project_name": "Loaded", "status": "issued", "geometry": None, "dates": {}, "source_url": None, "evidence": {}, "created_at": now()})
    assert client.delete(f"/api/v1/forest-change/records/{rid}/permits/{other}", headers=REVIEWER).status_code == 422


def test_a_spot_can_be_removed_and_a_false_alarm_undone():
    rid = new_record(geom=shifted(8.0))
    assert client.patch(f"/api/v1/forest-change/records/{rid}", headers=REVIEWER, json={"status": "FALSE_POSITIVE"}).json()["status"] == "FALSE_POSITIVE"
    assert client.patch(f"/api/v1/forest-change/records/{rid}", headers=REVIEWER, json={"status": "NEEDS_REVIEW"}).json()["status"] == "NEEDS_REVIEW"
    assert client.delete(f"/api/v1/forest-change/records/{rid}", headers={"X-Role": "officer"}).status_code == 403
    assert client.delete(f"/api/v1/forest-change/records/{rid}", headers=REVIEWER).status_code == 200
    assert client.get(f"/api/v1/forest-change/records/{rid}", headers=ADMIN).status_code == 404


def test_person_reported_incidents_get_the_same_two_decisions():
    inc = client.post("/api/v1/incidents", headers=ADMIN, json={"title": "Trees cut by the river", "geometry": {"type": "Point", "coordinates": [78.2, 21.9]}, "evidence": {"kind": "cutting", "note": "saw a truck"}}).json()
    iid = inc["id"]
    script = client.get(f"/api/v1/incidents/{iid}/escalate/script", headers=ADMIN).json()["script"]
    assert "illegal tree cutting" in script and "degrees north" in script and "has not been verified" in script
    r = client.post(f"/api/v1/incidents/{iid}/escalate", headers=REVIEWER).json()
    assert r["telegram"]["status"] == "NOT_CONFIGURED" and r["call"]["status"] == "NOT_CONFIGURED"      # keyless test server: honest, nothing pretended
    assert client.post(f"/api/v1/incidents/{iid}/escalate", headers={"X-Role": "read_only"}).status_code == 403
    assert client.post(f"/api/v1/incidents/{iid}/false-alarm", headers=REVIEWER).json()["status"] == "DISMISSED"
    assert client.get(f"/api/v1/incidents/{iid}", headers=ADMIN).json()["status"] == "DISMISSED"
    assert client.post(f"/api/v1/incidents/{iid}/reopen", headers=REVIEWER).json()["status"] == "DETECTED"
    assert client.delete(f"/api/v1/incidents/{iid}", headers={"X-Role": "officer"}).status_code == 403
    assert client.delete(f"/api/v1/incidents/{iid}", headers=REVIEWER).status_code == 200
    assert client.get("/api/v1/calls/status?call_id=123", headers=ADMIN).json() == {"status": None, "duration_s": None, "hangup": None}


def _scan_with_history(days_ago_of_spots):
    """A finished scan of a 2 km box with a 100 ha forest, and loss spots at the given ages (synthetic, for this test only)."""
    from shapely.geometry import box as _box, mapping as _map
    w, s, e, n = 95.0, 21.70, 95.02, 21.72
    def sq(x, y, d): return _map(_box(x, y, x + d, y + d))
    snaps = [{"days_ago": d, "date": (TODAY - timedelta(days=d)).isoformat(), "forest_ha": 100.0 - lost, "lost_ha": lost, "forest": _map(_box(w, s, e, n)), "lost": None} for d, lost in ((90, 0.0), (30, 1.0), (7, 2.0), (0, 3.0))]
    run = str(uuid.uuid4())
    insert("fc_runs", {"id": run, "status": "COMPLETE", "progress": 1.0, "bbox": [w, s, e, n], "window_start": (TODAY - timedelta(days=90)).isoformat(), "window_end": TODAY.isoformat(), "window_days": 90, "params": {}, "result": {"steps": [], "summary": {}, "timeline": snaps}, "created_by": "t", "created_at": now(), "finished_at": now()})
    ids = []
    for k, age in enumerate(days_ago_of_spots):
        fw._ingest(run, [patch_dict(sq(95.002 + 0.0009 * k, 21.702, 0.0006), area=0.5, est=TODAY - timedelta(days=age))], "t")
        with connection() as conn:
            ids.append(dict(conn.execute("SELECT id FROM fc_records ORDER BY created_at DESC LIMIT 1").fetchone())["id"])
    return run, ids


def test_a_scanned_area_is_cut_into_labelled_cells_with_loss_for_7_30_and_90_days():
    run, ids = _scan_with_history([3, 20, 60])
    g = client.get(f"/api/v1/forest-change/runs/{run}/grid", headers=ADMIN).json()
    assert g["cell_m"] >= 250 and g["cells"] and all(c["id"][0].isalpha() and c["id"][1:].isdigit() for c in g["cells"])
    top = g["cells"][0]                                   # sorted by the most loss in 90 days
    assert top["loss_ha"]["90"] >= top["loss_ha"]["30"] >= top["loss_ha"]["7"] > 0
    assert {s["id"] for s in top["spots"]} <= set(ids) and top["cover_ha"]["90"] >= top["cover_ha"]["0"] and top["polygon"]["type"] == "Polygon"
    assert g["totals"]["90"] >= g["totals"]["30"] >= g["totals"]["7"] > 0
    assert client.get(f"/api/v1/forest-change/runs/{uuid.uuid4()}/grid", headers=ADMIN).status_code == 422


def test_the_list_can_be_limited_to_one_scanned_area_and_a_drawn_shape_is_remembered():
    run, ids = _scan_with_history([10])
    far = new_record(geom=shifted(40.0))                  # a spot far outside that scan
    inside = {r["id"] for r in client.get(f"/api/v1/forest-change/records?days=90&run_id={run}", headers=ADMIN).json()}
    assert ids[0] in inside and far not in inside
    r = client.get(f"/api/v1/forest-change/runs/{run}", headers=ADMIN).json()
    assert r["polygon"] is None and r["area_km2"] > 1
    assert client.get(f"/api/v1/forest-change/records?days=90&run_id={uuid.uuid4()}", headers=ADMIN).status_code == 404


def test_a_newer_stricter_scan_sets_aside_unseen_spots_but_never_a_persons_decision():
    from datetime import date as _d
    far = shifted(60.0)
    a = new_record(geom=far)
    b = new_record(geom=shifted(60.2))
    client.patch(f"/api/v1/forest-change/records/{b}", headers=REVIEWER, json={"status": "CONFIRMED"})   # a person decided this one
    new_run = str(uuid.uuid4())
    area = {"type": "Polygon", "coordinates": [[[141.0, 21.69], [141.4, 21.69], [141.4, 21.72], [141.0, 21.72], [141.0, 21.69]]]}
    assert fw._retire_unseen(new_run, area, TODAY - timedelta(days=90)) == 1
    statuses = {r["id"]: r["status"] for r in client.get("/api/v1/forest-change/records?days=90&include_false=true", headers=ADMIN).json()}
    assert statuses[a] == "SUPERSEDED" and statuses[b] == "CONFIRMED"
    assert a not in [r["id"] for r in client.get("/api/v1/forest-change/records?days=90", headers=ADMIN).json()]
    # seen again later: it comes back for review; and Undo also works
    fw._ingest(str(uuid.uuid4()), [patch_dict(far)], "t")
    assert {r["id"]: r["status"] for r in client.get("/api/v1/forest-change/records?days=90", headers=ADMIN).json()}[a] == "NEEDS_REVIEW"


def test_a_running_check_can_be_cancelled_and_stops_at_its_next_step():
    rid = str(uuid.uuid4())
    insert("fc_runs", {"id": rid, "status": "RUNNING", "progress": 0.3, "bbox": [95.0, 21.7, 95.02, 21.72], "window_start": TODAY.isoformat(), "window_end": TODAY.isoformat(), "window_days": 90, "params": {}, "result": {"steps": []}, "created_by": "t", "created_at": now(), "finished_at": None})
    r = client.post(f"/api/v1/forest-change/runs/{rid}/cancel", headers=ADMIN).json()
    assert r == {"status": "CANCELLED", "cancelled": True}
    with pytest.raises(fw.WatchError, match="Cancelled"):
        fw._step(rid, [], "next step", 0.5)
    assert client.post(f"/api/v1/forest-change/runs/{rid}/cancel", headers=ADMIN).json()["cancelled"] is False     # already over
    assert client.post(f"/api/v1/forest-change/runs/{uuid.uuid4()}/cancel", headers=ADMIN).status_code == 404
    assert client.post(f"/api/v1/forest-change/runs/{rid}/cancel", headers={"X-Role": "read_only"}).status_code == 403


def test_police_station_is_used_when_no_forest_office_is_near(monkeypatch):
    from app.services import responders

    class Router:
        available = True

        def route(self, o, d, alt):
            return {"status": "ok", "provider": "t", "primary": {"distance_km": 5.0, "duration_min": 9.0, "adjusted_duration_min": 9.0, "geometry": {"type": "LineString", "coordinates": [o, d]}, "reasons": [], "segments": []}, "alternatives": []}

    monkeypatch.setattr(fw, "discover_offices", lambda *a, **k: None)
    monkeypatch.setattr(responders, "discover_police", lambda *a, **k: {"added": 0, "sources": {}})
    rid = new_record(geom=shifted(9.0))
    c = [x for x in fw._get(rid) and [fw._get(rid)]][0]
    assert responders.store("police_station", "Test Police Station", c["lat"], c["lon"] + 0.05, "test", "p-" + rid)
    assert not responders.store("police_station", "Same place", c["lat"], c["lon"] + 0.05, "test", "q-" + rid)   # never stored twice
    res = fw.assign_authority(rid, Router(), "test")
    assert res["status"] == "OK" and res["office"]["kind"] == "police_station" and res["office"]["icon"] == "👮" and res["police"] == {"same_as_main": True}


def test_nearest_police_station_is_always_given_next_to_a_forest_office(monkeypatch):
    from app.services import responders

    class Router:
        available = True

        def route(self, o, d, alt):
            return {"status": "ok", "provider": "t", "primary": {"distance_km": 5.0, "duration_min": 9.0, "adjusted_duration_min": 9.0, "geometry": {"type": "LineString", "coordinates": [o, d]}, "reasons": [], "segments": []}, "alternatives": []}

    monkeypatch.setattr(fw, "discover_offices", lambda *a, **k: None)
    monkeypatch.setattr(responders, "discover_police", lambda *a, **k: {"added": 0, "sources": {}})
    rid = new_record(geom=shifted(11.0))
    c = fw._get(rid)
    insert("stations", {"id": str(uuid.uuid4()), "name": "Range Office X", "jurisdiction": "unknown", "location": {"type": "Point", "coordinates": [c["lon"] + 0.02, c["lat"]]}, "coverage": None, "contacts": {}, "availability_note": None, "created_at": now(), "kind": "forest_office", "source": "test", "verified": 0, "external_ref": "fo-" + rid})
    responders.store("police_station", "Police Y", c["lat"], c["lon"] + 0.04, "test", "py-" + rid)
    res = fw.assign_authority(rid, Router(), "test")
    assert res["office"]["kind"] == "forest_office" and res["police"]["name"] == "Police Y" and res["police"]["geometry"]


def test_a_spot_has_a_pdf_report_with_summary_and_links():
    rid = new_record(geom=shifted(13.0))
    r = client.get(f"/api/v1/forest-change/records/{rid}/report.pdf", headers=ADMIN)
    assert r.status_code == 200 and r.content[:4] == b"%PDF" and len(r.content) > 3000
