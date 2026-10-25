"""Infrastructure change: detection logic on small synthetic images (tests only), classification, the whole run with stubbed imagery,
and the API, pictures and reports."""
import time

import numpy as np
import pytest
from affine import Affine
from fastapi.testclient import TestClient
from shapely.geometry import LineString, Polygon, box

from app.db.database import get_one
from app.main import app
from app.services import boundary, infra, osm
from app.services.satellite import Grid, ProviderError

client = TestClient(app)
ADMIN = {"X-Role": "admin"}
N = 60   # test images are 60 x 60 pixels at 10 m


@pytest.fixture(autouse=True)
def in_india(monkeypatch, tmp_path):
    monkeypatch.setattr(boundary, "contains", lambda lat, lon: True)
    monkeypatch.setattr(infra, "DATA", tmp_path / "infra")


def bands(red=0.08, nir=0.35, blue=0.04, green=0.07, swir=0.18):
    return {k: np.full((N, N), v, dtype="float32") for k, v in (("B02", blue), ("B03", green), ("B04", red), ("B08", nir), ("B11", swir))}


def paint(b, rows, cols, **kw):
    for k, v in kw.items():
        b[k][rows, cols] = v


def bare_after(b, rows, cols):   # green -> bare, bright soil
    paint(b, rows, cols, B04=0.28, B08=0.30, B02=0.14, B03=0.22, B11=0.38)


def grid():
    return Grid("EPSG:32643", Affine(10, 0, 500000, 0, -10, 2965000), N, N, 10.0)


def test_green_ground_that_stays_bare_is_found_but_a_field_that_greens_again_is_not():
    before = bands()
    after, spring, summer = bands(), bands(), bands()
    sl = (slice(10, 25), slice(10, 25))      # cleared for good: bare now and in between
    bare_after(after, *sl); bare_after(spring, *sl); bare_after(summer, *sl)
    fl = (slice(35, 50), slice(35, 50))      # harvested now, but green in spring: a crop cycle
    bare_after(after, *fl)
    m = infra.detect(before, [after, spring, summer])
    assert m["veg"][sl].all() and not m["veg"][fl].any()
    assert m["veg"].sum() == 15 * 15


def test_water_and_clouds_are_never_reported():
    before, after, mid = bands(), bands(), bands()
    w = (slice(5, 20), slice(5, 20))
    for b in (before, after, mid):    # a lake: water index > 0
        paint(b, *w, B03=0.12, B11=0.02, B04=0.05, B08=0.03)
    cl = (slice(30, 45), slice(30, 45))
    after["B04"][cl] = np.nan; after["B08"][cl] = np.nan
    m = infra.detect(before, [after, mid])
    assert not m["veg"][w].any() and not m["veg"][cl].any()


def test_dark_ground_that_became_bright_hard_surface_is_found_when_it_persists():
    before = bands(red=0.10, nir=0.16, blue=0.07, green=0.09, swir=0.17)   # dark, not green
    after, spring, summer = (bands(red=0.10, nir=0.16, blue=0.07, green=0.09, swir=0.17) for _ in range(3))
    sl = (slice(12, 30), slice(12, 30))
    for b in (after, spring, summer):
        paint(b, *sl, B04=0.30, B03=0.30, B02=0.29, B08=0.28, B11=0.36)
    m = infra.detect(before, [after, spring, summer])
    assert m["bare"][sl].all()


def test_patches_are_polygons_with_area_and_tiny_specks_are_dropped():
    g = grid()
    veg = np.zeros((N, N), bool)
    veg[10:30, 10:25] = True      # 300 px = 3 ha
    veg[50, 50] = True            # a single pixel
    lab, found = infra.patches_from({"veg": veg, "bare": np.zeros((N, N), bool)}, g, infra.Params())
    assert len(found) == 1 and found[0]["area_ha"] == pytest.approx(3.0, abs=0.1)
    assert found[0]["poly_ll"].geom_type == "Polygon"


def ctx(**kinds):
    return infra.build_ctx({k: [(g, t) for g, t in v] for k, v in kinds.items()})


def test_osm_buildings_and_roads_decide_the_class_and_the_source_is_stated():
    patch = box(0, 0, 40, 40)
    c = infra.classify(patch, ctx(building=[(box(5, 5, 35, 35), {"building": "house", "name": "Sharma House"})]), {})
    assert c["cls"] == "building" and "OpenStreetMap" in c["source"] and c["name"] == "Sharma House"
    c = infra.classify(patch, ctx(landuse=[(box(-5, -5, 45, 45), {"landuse": "industrial", "name": "RIICO"})]), {})
    assert c["cls"] == "industrial"
    c = infra.classify(patch, ctx(landuse=[(box(-5, -5, 45, 45), {"landuse": "construction"})]), {})
    assert c["cls"] == "construction"
    strip = box(0, 0, 400, 20)
    c = infra.classify(strip, ctx(road=[(LineString([(0, 10), (400, 10)]), {"highway": "primary", "name": "NH 48"})]), {})
    assert c["cls"] == "highway" and c["name"] == "NH 48"
    c = infra.classify(strip, ctx(road=[(LineString([(0, 10), (400, 10)]), {"highway": "residential"})]), {})
    assert c["cls"] == "road"


def test_without_openstreetmap_the_class_comes_from_shape_and_colour_and_says_so():
    assert infra.classify(box(0, 0, 500, 20), None, {})["cls"] == "road"
    roof = infra.classify(box(0, 0, 40, 40), None, {"sat_after": 0.1, "bright_after": 0.3})
    assert roof["cls"] == "building" and "satellite" in roof["source"]
    soil = infra.classify(box(0, 0, 40, 40), None, {"sat_after": 0.3, "bright_after": 0.28})
    assert soil["cls"] == "cleared" and "use unknown" in soil["source"]


def test_area_limits_and_place_are_checked():
    assert infra.validate([75.6, 26.78, 75.7, 26.85])[2] == 10.0
    assert infra.validate([75.0, 26.0, 75.5, 26.5])[2] == 20.0 if infra.area_km2([75.0, 26.0, 75.5, 26.5]) < infra.MAX_AREA_KM2 else True
    with pytest.raises(infra.InfraError):
        infra.validate([75.0, 26.0, 75.001, 26.001])
    with pytest.raises(infra.InfraError):
        infra.validate([70.0, 20.0, 80.0, 30.0])


def stub_imagery(monkeypatch, mark_fn):
    g = grid()
    calls = []

    def fake_composite(bbox, res, centre, half, n, max_cloud, polygon=None):
        calls.append(centre)
        b = bands()
        if centre.year == infra.dates()["after"].year and centre == infra.dates()["after"] or centre in infra.dates()["mids"]:
            mark_fn(b)
        return {"bands": b, "scenes": [{"id": "s", "date": str(centre), "cloud": 0}], "grid": g, "cached": False}

    class FakePC:
        def worldcover_classes(self, box_, grid_):
            out = np.full((grid_.height, grid_.width), 40, dtype="uint8")
            out[10:30, 10:25] = 10
            return out

    monkeypatch.setattr(infra, "composite", fake_composite)
    monkeypatch.setattr(infra, "provider", lambda: FakePC())
    monkeypatch.setattr(osm, "fetch", lambda q, client=None: (_ for _ in ()).throw(osm.OsmUnavailable("offline")))
    return g, calls


def new_run(name="Test area"):
    import uuid

    from app.db.database import insert, now

    rid = str(uuid.uuid4())
    insert("infra_runs", {"id": rid, "name": name, "status": "QUEUED", "progress": 0.0, "bbox": [75.6, 26.78, 75.7, 26.85], "geometry": None, "params": {}, "result": None, "created_by": "test", "created_at": now(), "finished_at": None})
    return rid


def test_a_whole_run_finds_the_change_measures_it_and_stores_pictures(monkeypatch):
    stub_imagery(monkeypatch, lambda b: bare_after(b, slice(10, 30), slice(10, 25)))
    rid = new_run()
    summary = infra.run(rid, [75.6, 26.78, 75.7, 26.85], None, "test")
    assert summary["patches"] == 1 and summary["osm"] == "unavailable"
    run = get_one("infra_runs", "id", rid)
    assert run["status"] == "COMPLETE" and run["result"]["summary"]["total_ha"] == pytest.approx(3.0, abs=0.1)
    ch = infra.changes_of(rid)[0]
    assert ch["code"] == "IN-0001" and ch["impact"]["vegetation_lost_ha"] == pytest.approx(3.0, abs=0.1)
    assert ch["impact"]["tree_cover_lost_ha"] > 2 and ch["impact"]["carbon_t_co2"]["central"] > 0      # WorldCover says it was trees
    assert any("typical" in k or k == "typical" for k in ch["impact"]["effects"]) and ch["impact"]["effects"]["measured"]
    assert (infra.DATA / f"{rid}.npz").is_file()
    img = infra.compare_image(ch, rid, run)
    assert img[:2] == b"\xff\xd8"


def test_a_run_with_no_change_is_complete_with_zero_changes(monkeypatch):
    stub_imagery(monkeypatch, lambda b: None)
    rid = new_run()
    assert infra.run(rid, [75.6, 26.78, 75.7, 26.85], None, "test")["patches"] == 0
    assert get_one("infra_runs", "id", rid)["status"] == "COMPLETE"


def test_a_cancelled_run_stops_and_is_marked(monkeypatch):
    stub_imagery(monkeypatch, lambda b: None)
    rid = new_run()
    from app.db.database import update

    update("infra_runs", "id", rid, {"status": "CANCELLED"})
    assert infra.run(rid, [75.6, 26.78, 75.7, 26.85], None, "test").get("cancelled") is True
    assert get_one("infra_runs", "id", rid)["status"] == "CANCELLED"


def test_missing_imagery_fails_with_a_clear_reason(monkeypatch):
    monkeypatch.setattr(infra, "composite", lambda *a, **k: {"bands": None, "scenes": [], "grid": grid(), "cached": False})
    rid = new_run()
    with pytest.raises(infra.InfraError):
        infra.run(rid, [75.6, 26.78, 75.7, 26.85], None, "test")
    r = get_one("infra_runs", "id", rid)
    assert r["status"] == "FAILED" and "clear satellite images" in r["result"]["error"]


def test_api_lists_reviews_exports_reports_and_removes(monkeypatch):
    stub_imagery(monkeypatch, lambda b: bare_after(b, slice(10, 30), slice(10, 25)))
    rid = new_run()
    infra.run(rid, [75.6, 26.78, 75.7, 26.85], None, "test")
    st = client.get("/api/v1/infra/status", headers=ADMIN).json()
    assert "jaipur-sanganer" in st["presets"] and st["history_days"] == 365 and st["classes"]["road"]["color"]
    run = client.get(f"/api/v1/infra/runs/{rid}", headers=ADMIN).json()
    assert run["status"] == "COMPLETE" and run["summary"]["patches"] == 1
    ch = client.get(f"/api/v1/infra/runs/{rid}/changes", headers=ADMIN).json()
    assert len(ch) == 1 and ch[0]["cls_label"] and ch[0]["color"].startswith("#") and ch[0]["geometry"]["type"] == "Polygon"
    cid = ch[0]["id"]
    full = client.get(f"/api/v1/infra/changes/{cid}", headers=ADMIN).json()
    assert full["impact"]["effects"]["measured"] and full["evidence"]["resolution_m"] == 10.0
    for kind in ("before", "after", "compare"):
        r = client.get(f"/api/v1/infra/changes/{cid}/image/{kind}.jpg", headers=ADMIN)
        assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"
    for path in (f"/api/v1/infra/changes/{cid}/report.pdf", f"/api/v1/infra/runs/{rid}/report.pdf"):
        r = client.get(path, headers=ADMIN)
        assert r.status_code == 200 and r.content[:4] == b"%PDF" and len(r.content) > 5000
    gj = client.get(f"/api/v1/infra/runs/{rid}/export.geojson", headers=ADMIN).json()
    assert len(gj["features"]) == 1 and gj["features"][0]["properties"]["code"] == "IN-0001"
    # a false detection can be set aside and put back
    assert client.patch(f"/api/v1/infra/changes/{cid}", headers=ADMIN, json={"status": "NOT_A_CHANGE"}).json()["status"] == "NOT_A_CHANGE"
    assert client.get(f"/api/v1/infra/runs/{rid}/changes", headers=ADMIN).json() == []
    assert len(client.get(f"/api/v1/infra/runs/{rid}/changes?include_hidden=true", headers=ADMIN).json()) == 1
    assert client.patch(f"/api/v1/infra/changes/{cid}", headers=ADMIN, json={"status": "OPEN"}).json()["status"] == "OPEN"
    assert len(client.get(f"/api/v1/infra/runs/{rid}/changes", headers=ADMIN).json()) == 1
    assert client.delete(f"/api/v1/infra/runs/{rid}", headers=ADMIN).json()["deleted"] == rid
    assert client.get(f"/api/v1/infra/runs/{rid}", headers=ADMIN).status_code == 404
    assert client.get(f"/api/v1/infra/changes/{cid}", headers=ADMIN).status_code == 404


def test_api_validates_the_area_and_the_role():
    r = client.post("/api/v1/infra/runs", headers=ADMIN, json={})
    assert r.status_code == 422
    r = client.post("/api/v1/infra/runs", headers=ADMIN, json={"bbox": [70, 20, 80, 30]})
    assert r.status_code == 422 and "limit" in r.json()["detail"]
    assert client.post("/api/v1/infra/runs", headers=ADMIN, json={"preset": "nowhere"}).status_code == 422
    assert client.post("/api/v1/infra/runs", headers={"X-Role": "read_only"}, json={"preset": "jaipur-sanganer"}).status_code == 403


def test_a_preset_starts_a_background_job(monkeypatch):
    stub_imagery(monkeypatch, lambda b: bare_after(b, slice(10, 30), slice(10, 25)))
    r = client.post("/api/v1/infra/runs", headers=ADMIN, json={"preset": "jaipur-sanganer"}).json()
    for _ in range(100):
        s = client.get(f"/api/v1/infra/runs/{r['run_id']}", headers=ADMIN).json()
        if s["status"] in ("COMPLETE", "FAILED"):
            break
        time.sleep(0.2)
    assert s["status"] == "COMPLETE" and s["preset"] == "jaipur-sanganer" and s["summary"]["patches"] == 1
