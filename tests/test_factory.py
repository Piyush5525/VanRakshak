import io
import time
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app.main import app
from app.services import boundary, factorydoc, factoryimpact

client = TestClient(app)
ADMIN = {"X-Role": "admin"}
LAT, LON = 21.9, 78.0


@pytest.fixture(autouse=True)
def in_india(monkeypatch):
    monkeypatch.setattr(boundary, "contains", lambda lat, lon: True)


def make_pdf(lines: list[str]) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for ln in lines:
        c.drawString(50, y, ln)
        y -= 18
    c.showPage()
    c.save()
    return buf.getvalue()


DOC = ["Project: Sunrise Cement Unit", "Proposed cement clinker grinding unit, capacity 2 MTPA", "Total plot area 45 acres at the site", "Forest land of 3.2 ha is proposed for diversion", "Stack height 90 m with a bag filter chimney", "Water requirement 1200 KLD from the river",
       "Emissions: SO2 50 mg/Nm3 and PM 30 mg/Nm3 at the stack", "Latitude: 21.9520, Longitude: 78.0510", "Village: Rampur  District: Chhindwara  State: Madhya Pradesh", "Environmental Clearance and Consent to Establish are pending. Public hearing was held."]


def test_pdf_text_is_turned_into_checked_fields_with_their_source():
    pages, warnings = factorydoc.read_pdf(make_pdf(DOC))
    assert warnings == []
    out = factorydoc.extract(pages)
    f = out["fields"]
    assert f["name"]["value"].startswith("Sunrise Cement Unit") and f["kind"]["value"] == "cement"
    assert f["footprint_ha"]["value"] == pytest.approx(45 * 0.404686, abs=0.01) and f["footprint_ha"]["page"] == 1 and "45 acres" in f["footprint_ha"]["snippet"]
    assert f["forest_land_ha"]["value"] == 3.2 and f["stack_height_m"]["value"] == 90 and f["water_kld"]["value"] == 1200
    assert f["coordinates"]["value"] == {"lat": 21.952, "lon": 78.051}
    assert {(e["pollutant"], e["value"]) for e in f["emissions"]["value"]} >= {("SO2", 50.0), ("PM", 30.0)}
    assert f["place"]["value"]["district"].startswith("Chhindwara") and "Environmental Clearance" in out["mentions"] and "Public hearing" in out["mentions"]


def test_unreadable_or_wrong_files_are_refused_or_flagged():
    with pytest.raises(ValueError, match="not a PDF"):
        factorydoc.read_pdf(b"hello world")
    _, warnings = factorydoc.read_pdf(make_pdf([]))
    assert warnings and "no readable text" in warnings[0].lower() or "almost no readable text" in warnings[0]


def test_extract_endpoint_reads_an_upload_and_never_stores_the_file():
    r = client.post("/api/v1/factory/extract", headers=ADMIN, files={"file": ("plan.pdf", make_pdf(DOC), "application/pdf")})
    assert r.status_code == 200 and r.json()["fields"]["name"]["value"].startswith("Sunrise") and len(r.json()["sha256"]) == 64
    assert client.post("/api/v1/factory/extract", headers=ADMIN, files={"file": ("x.pdf", b"not a pdf", "application/pdf")}).status_code == 422
    assert client.post("/api/v1/factory/extract", headers={"X-Role": "read_only"}, files={"file": ("plan.pdf", make_pdf(DOC), "application/pdf")}).status_code == 403


@pytest.fixture
def fake_world(monkeypatch):
    lc = np.full((500, 500), 10, dtype="uint8")
    lc[:, 330:] = 40   # farmland on the east side of the window
    monkeypatch.setattr(factoryimpact, "provider", lambda: SimpleNamespace(worldcover_classes=lambda b, g: lc))
    monkeypatch.setattr(factoryimpact, "_wind", lambda lat, lon: {"status": "ok", "source": "test wind", "from_deg": 270.0, "from_name": "west", "mean_kmh": 12.0, "share_prevailing_pct": 40.0, "rose": [{"from": n, "pct": 12.5} for n in ("north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west")]})

    def osm(q):
        if "waterway" in q:
            return [{"type": "way", "tags": {"waterway": "river", "name": "Test River"}, "geometry": [{"lat": LAT + 0.01, "lon": LON - 0.02}, {"lat": LAT + 0.01, "lon": LON + 0.02}]}], None
        if "place" in q:
            return [{"type": "node", "lat": LAT, "lon": LON + 0.02, "tags": {"name": "Eastville", "place": "village"}}, {"type": "node", "lat": LAT, "lon": LON - 0.02, "tags": {"name": "Westpur", "place": "village"}}], None
        return [{"type": "way", "center": {"lat": LAT + 0.2, "lon": LON}, "tags": {"name": "Test Reserve", "boundary": "protected_area"}}], None

    monkeypatch.setattr(factoryimpact, "_osm", osm)


def test_assessment_counts_forest_inside_the_site_and_builds_up_over_90_days(fake_world):
    r = factoryimpact.assess({"lat": LAT, "lon": LON, "kind": "cement", "footprint_ha": 25, "name": "T"})
    assert r["status"] == "ok" and r["site"]["forest_in_footprint_ha"] == pytest.approx(25, abs=1.5)
    assert r["site"]["edge_zone_forest_ha"] > 50 and r["site"]["forest_within_3km_ha"] > 1000
    areas = {e["area"]: e for e in r["effects"]}
    assert set(areas) == {"Forest", "Habitat", "Air", "Water", "People", "Carbon"} and areas["Forest"]["level"] == "high"
    assert [x["label"] for x in r["smoke_zones"]] == ["closest ring", "middle ring", "outer ring"] and r["smoke_zones"][0]["forest_ha"] <= r["smoke_zones"][2]["forest_ha"]
    # the village to the east is downwind of a west wind; the one to the west is not
    down = {v["name"]: v["downwind"] for v in r["layers"]["villages"]}
    assert down == {"Eastville": True, "Westpur": False}
    h = {x["day"]: x for x in r["projection"]["horizons"]}
    assert h[7]["cleared_ha"] < h[30]["cleared_ha"] < h[90]["cleared_ha"] == pytest.approx(r["site"]["forest_in_footprint_ha"], abs=0.2)
    assert h[7]["edge_affected_ha"] < h[90]["edge_affected_ha"] and not h[30]["operating"] and h[90]["operating"] and h[7]["smoke_forest_ha"] == 0 < h[90]["smoke_forest_ha"]
    assert h[90]["water_pathway"] == "open" and r["carbon_t_co2"]["low"] < r["carbon_t_co2"]["high"]
    assert "not an environmental impact assessment" in r["notice"] and r["unavailable"] == {}


def test_missing_sources_are_reported_not_invented(fake_world, monkeypatch):
    monkeypatch.setattr(factoryimpact, "_osm", lambda q: (None, "Overpass unavailable"))
    monkeypatch.setattr(factoryimpact, "_wind", lambda lat, lon: {"status": "unavailable", "reason": "Wind history unavailable"})
    r = factoryimpact.assess({"lat": LAT, "lon": LON, "kind": "textile", "name": "T"})
    assert set(r["unavailable"]) == {"rivers", "villages", "protected_areas"} and r["smoke_zones"] is None and r["layers"]["plumes"] == []
    air = next(e for e in r["effects"] if e["area"] == "Air")
    assert air["title"] == "Wind data unavailable"
    assert r["inputs"]["footprint_assumed"] is True and r["inputs"]["footprint_ha"] == factoryimpact.PROFILES["textile"]["footprint_ha"]


def test_check_endpoints_run_a_job_save_the_result_and_make_a_pdf(fake_world):
    body = {"name": "Sunrise Cement Unit", "kind": "cement", "lat": LAT, "lon": LON, "footprint_ha": 20, "emissions": [{"pollutant": "SO2", "value": 50, "unit": "mg/Nm3", "source": "Consent order p.4"}]}
    r = client.post("/api/v1/factory/checks", headers=ADMIN, json=body)
    assert r.status_code == 200
    cid = r.json()["id"]
    for _ in range(60):
        d = client.get(f"/api/v1/factory/checks/{cid}", headers=ADMIN).json()
        if d["status"] != "RUNNING":
            break
        time.sleep(0.5)
    assert d["status"] == "COMPLETE" and d["result"]["inputs"]["emissions"][0]["source"] == "Consent order p.4"
    assert cid in [c["id"] for c in client.get("/api/v1/factory/checks", headers=ADMIN).json()]
    pdf = client.get(f"/api/v1/factory/checks/{cid}/report.pdf", headers=ADMIN)
    assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-"
    assert client.post("/api/v1/factory/checks", headers=ADMIN, json={**body, "emissions": [{"pollutant": "SO2", "value": 5, "unit": "x", "source": ""}]}).status_code == 422   # a figure needs its source
    assert client.post("/api/v1/factory/checks", headers={"X-Role": "read_only"}, json=body).status_code == 403
    assert client.delete(f"/api/v1/factory/checks/{cid}", headers=ADMIN).status_code == 200 and client.get(f"/api/v1/factory/checks/{cid}", headers=ADMIN).status_code == 404
