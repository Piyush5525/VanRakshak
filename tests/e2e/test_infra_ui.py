"""Browser test of the "New construction" panel on a private server whose throwaway database holds one finished check with two changes.

The pictures and shapes only exist to drive the interface; nothing here is shown outside this test database.
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from tests.e2e.test_ui_smoke import browser, free_port  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[2]
SEED = r'''
import uuid
import numpy as np
from pyproj import Transformer
from shapely.geometry import box, mapping
from shapely.ops import transform
from app.db.database import init_db, insert, now
from app.services import infra
init_db()
to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32643", always_xy=True).transform
to_ll = Transformer.from_crs("EPSG:32643", "EPSG:4326", always_xy=True).transform
x0, y0 = to_utm(75.64, 26.84)
N = 80
rng = np.random.default_rng(3)
before = (rng.random((N, N, 3)) * 40 + 90).astype("uint8"); after = before.copy(); after[10:30, 10:40] = (200, 190, 170)
rid = str(uuid.uuid4())
DATA = infra.DATA; DATA.mkdir(parents=True, exist_ok=True)
np.savez_compressed(DATA / f"{rid}.npz", before=before, after=after, transform=np.array([10, 0, x0, 0, -10, y0]), crs=np.array("EPSG:32643"), res=np.array(10.0))
bbox = [75.64, 26.82, 75.66, 26.84]
def patch(r0, r1, c0, c1):
    return mapping(transform(to_ll, box(x0 + c0 * 10, y0 - r1 * 10, x0 + c1 * 10, y0 - r0 * 10)))
effects = {"measured": [{"icon": "📐", "text": "0.6 ha changed"}, {"icon": "🌿", "text": "0.5 ha of green vegetation is gone (greenness 0.40 → 0.12)"}], "typical": [{"icon": "🌧", "text": "Hard surfaces let less rain soak in."}]}
def row(code, cls, geom, ha, name):
    c = geom and __import__("shapely.geometry", fromlist=["shape"]).shape(geom).representative_point()
    return {"id": str(uuid.uuid4()), "run_id": rid, "code": code, "cls": cls, "cls_source": "OpenStreetMap: highway=primary" if cls == "highway" else "satellite: bright grey surface (roof or concrete), not yet in OpenStreetMap", "name": name, "geometry": geom, "lat": c.y, "lon": c.x, "area_ha": ha,
            "evidence": {"detected_as": "vegetation lost", "length_m": 300, "width_m": 20, "aspect": 15.0, "pixels": 60, "resolution_m": 10.0}, "impact": {"area_ha": ha, "vegetation_lost_ha": ha, "ndvi_before": 0.4, "ndvi_after": 0.12, "hard_surface_added_ha": ha, "effects": effects, "land_before": {"cropland": ha}}, "status": "OPEN", "note": None, "created_at": now()}
insert("infra_runs", {"id": rid, "name": "Test area", "status": "COMPLETE", "progress": 1.0, "bbox": bbox, "geometry": None, "params": {"preset": "jaipur-sanganer"}, "created_by": "test", "created_at": now(), "finished_at": now(),
                      "result": {"steps": [], "summary": {"patches": 2, "total_ha": 1.4, "vegetation_lost_ha": 1.4, "tree_cover_lost_ha": 0.0, "hard_surface_ha": 1.4, "by_class": {"highway": {"n": 1, "ha": 0.8}, "building": {"n": 1, "ha": 0.6}}, "area_km2": 4.0, "resolution_m": 10.0, "before": "2025-10-05", "after": "2026-10-05", "osm": "ok", "scenes": {}}}})
insert("infra_changes", row("IN-0001", "highway", patch(10, 14, 10, 40), 0.8, "NH 48"))
insert("infra_changes", row("IN-0002", "building", patch(40, 46, 40, 50), 0.6, None))
'''


@pytest.fixture()
def seeded():
    tmp = tempfile.mkdtemp(prefix="rakshak-infra-e2e-")
    port = free_port()
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp}/e2e.db", "REPORT_OUTPUT_DIR": f"{tmp}/r", "RASTER_DIR": f"{tmp}/ras", "MODEL_DIR": f"{tmp}/m", "EVIDENCE_DIR": f"{tmp}/ev", "INFRA_DIR": f"{tmp}/infra", "ENVIRONMENT": "test", "AUTH_MODE": "dev_header",
           "RATE_LIMIT_PER_MINUTE": "0", "ENABLE_EXAMPLES": "true", "OSM_DISABLED": "true", "PYTHONUTF8": "1", **{k: "" for k in ("MAPBOX_API", "SERP_API", "FIRMS_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "OSRM_URL", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
           "OMNIDIM_API_KEY", "OMNIDIM_AGENT_ID", "OMNIDIM_FROM_NUMBER_ID", "IVR_RESPONSE_NUMBER", "ALERT_PHONE_NUMBER")}, "ROUTING_PROVIDER": "unconfigured", "OSRM_PUBLIC": "false"}
    subprocess.run([sys.executable, "-c", SEED], cwd=ROOT, env=env, check=True)
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            break
        except OSError:
            time.sleep(0.5)
    else:
        proc.kill()
        pytest.skip("server did not start")
    yield f"http://127.0.0.1:{port}"
    proc.kill()


def test_new_construction_panel_lists_changes_compares_before_after_sets_aside_and_reports(seeded, browser):  # noqa: F811
    ctx = browser.new_context(viewport={"width": 1500, "height": 950})
    ctx.add_init_script("localStorage.setItem('rk.role','admin')")
    page = ctx.new_page()
    problems: list[str] = []
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}") if "setItem" not in str(e) else None)   # the PDF viewer frame has no storage for the init script
    page.goto(seeded + "/live", wait_until="load")
    # the layer switch opens the panel; the ready-made Jaipur area is already checked
    page.click("label.layer:has(#showInfra)")
    page.wait_for_selector("#infraBody [data-open]")
    assert "Jaipur" in page.inner_text("#infraBody") and "Draw a rectangle" in page.inner_text("#infraBody")
    page.click("#infraBody [data-open]")
    page.wait_for_selector("#chlist .row-item[data-id]")
    txt = page.inner_text("#infraBody")
    assert "IN-0001" in txt and "Highway" in txt and "IN-0002" in txt and "Buildings" in txt
    assert page.locator(".cchip").count() == 2
    # a type can be switched off and on
    page.click('.cchip[data-cls="building"]')
    assert page.locator("#chlist .row-item[data-id]").count() == 1
    page.click('.cchip[data-cls="building"]')
    assert page.locator("#chlist .row-item[data-id]").count() == 2
    # one change: before / after, measured impact, typical effects, report buttons
    page.locator("#chlist .row-item[data-id]").first.click()
    page.wait_for_selector("#cmp img")
    assert page.locator("#cmp img").count() == 2 and page.locator("#cslide").count() == 1
    page.wait_for_function("[...document.querySelectorAll('#cmp img')].every(i => i.complete && i.naturalWidth > 0)")
    t = page.inner_text("#infraBody")
    assert "Impact (measured)" in t and "green vegetation is gone" in t and "Typical effects" in t and "NH 48" in t
    page.locator("#cslide").evaluate("e => { e.value = 30; e.dispatchEvent(new Event('input')); }")
    assert "70%" in page.get_attribute("#cmpTop", "style")
    page.click("#cview")
    page.wait_for_selector("#rptView iframe")
    page.click("#rptClose")
    assert page.locator("#rptView").count() == 0
    # set aside, then put back
    page.click("#cnot")
    page.wait_for_selector("#chlist")
    assert page.locator("#chlist .row-item[data-id]").count() == 1 and "Set aside as not a change (1)" in page.inner_text("#infraBody")
    page.click("#aside summary")
    page.click("[data-back]")
    page.wait_for_function("document.querySelectorAll('#chlist .row-item[data-id]').length === 2")
    # the whole check can be removed (tap twice)
    page.click("#rdel")
    page.click("#rdel")
    page.wait_for_selector("#infraBody [data-preset]")
    assert problems == []
    ctx.close()
