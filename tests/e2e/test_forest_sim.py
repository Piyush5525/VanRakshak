"""Browser tests for the incident-driven forest simulation in the Forest Explorer, on a private server with a throwaway database.

The incidents, region and scene below are synthetic and exist only to drive the interface.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from tests.e2e.test_ui_smoke import browser, free_port  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[2]
HDR = {"X-Role": "admin"}
POLY = {"type": "Polygon", "coordinates": [[[75.60, 26.80], [75.606, 26.80], [75.606, 26.806], [75.603, 26.808], [75.60, 26.806], [75.60, 26.80]]]}
POINT = {"type": "Point", "coordinates": [75.70, 26.90]}
SCENE = {"region_id": "r", "attribution": "Synthetic test scene", "notes": [], "terrain": {"width": 6, "height": 6, "cell_size_m": 100, "heights_m": [[100 + r + c for c in range(6)] for r in range(6)], "min_m": 100, "max_m": 110, "origin": {"lat": 26.8, "lon": 75.6}},
         "tree_density": {"values": [0.6] * 36}, "timeline": [{"label": "Reference", "claim_type": "reference", "newly_cleared_cells": [], "cumulative_loss_hectares": 0, "date": "2025-01-01"}], "hypothetical_scenarios": [], "forecast_risk": None}


@pytest.fixture(scope="module")
def sim_server():
    tmp = tempfile.mkdtemp(prefix="rakshak-sim-e2e-")
    port = free_port()
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp}/e2e.db", "REPORT_OUTPUT_DIR": f"{tmp}/r", "RASTER_DIR": f"{tmp}/ras", "MODEL_DIR": f"{tmp}/m", "EVIDENCE_DIR": f"{tmp}/ev", "INFRA_DIR": f"{tmp}/infra", "ENVIRONMENT": "test", "AUTH_MODE": "dev_header", "RATE_LIMIT_PER_MINUTE": "0",
           "OSM_DISABLED": "true", "PYTHONUTF8": "1", **{k: "" for k in ("MAPBOX_API", "SERP_API", "FIRMS_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "OSRM_URL", "OMNIDIM_API_KEY", "OMNIDIM_AGENT_ID", "ALERT_PHONE_NUMBER")}, "ROUTING_PROVIDER": "unconfigured", "OSRM_PUBLIC": "false"}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            break
        except OSError:
            time.sleep(0.5)
    else:
        proc.kill()
        pytest.skip("server did not start")
    mk = lambda title, g, ev=None: httpx.post(base + "/api/v1/incidents", headers=HDR, json={"title": title, "geometry": g, "evidence": ev or {}}, timeout=30).json()  # noqa: E731
    out = {"base": base, "poly": mk("Cutting at the test ridge", POLY, {"kind": "cutting"}), "point": mk("Fire seen near the test village", POINT, {"kind": "fire"})}
    out["region"] = httpx.post(base + "/api/v1/regions", headers=HDR, json={"name": "Test region", "jurisdiction": "Test", "geometry": POLY}, timeout=30).json()
    yield out
    proc.kill()


def page_for(browser, w=1400, h=950):  # noqa: F811
    ctx = browser.new_context(viewport={"width": w, "height": h})
    ctx.add_init_script("localStorage.setItem('rk.role','admin')")
    page = ctx.new_page()
    problems: list[str] = []
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    return ctx, page, problems


def test_the_live_map_incident_detail_links_to_the_forest_explorer(sim_server, browser):  # noqa: F811
    ctx, page, problems = page_for(browser)
    page.goto(sim_server["base"] + "/live", wait_until="load")
    page.click('#rtabs [data-t=inc]')
    page.wait_for_selector("#rightBody .row-item[data-id]")
    page.click(f'#rightBody .row-item[data-id="{sim_server["poly"]["id"]}"]')
    page.wait_for_selector("#sim3d")
    assert page.get_attribute("#sim3d", "href") == f"/app#/ar-explorer/{sim_server['poly']['id']}"
    assert "Open in Forest Explorer" in page.inner_text("#sim3d")
    assert problems == []
    ctx.close()


def test_the_investigation_detail_links_to_the_forest_explorer(sim_server, browser):  # noqa: F811
    ctx, page, problems = page_for(browser)
    iid = sim_server["poly"]["id"]
    page.goto(sim_server["base"] + f"/app#/investigations/{iid}", wait_until="load")
    page.wait_for_selector("#openSim")
    assert page.get_attribute("#openSim", "href") == f"#/ar-explorer/{iid}"
    page.click("#openSim")
    page.wait_for_selector("#modeChip")
    assert page.inner_text("#modeChip").lower() == "incident simulation mode"
    ctx.close()


def test_a_polygon_incident_uses_its_exact_boundary_and_a_point_gets_a_synthetic_one(sim_server, browser):  # noqa: F811
    ctx, page, problems = page_for(browser)
    page.goto(sim_server["base"] + f"/app#/ar-explorer/{sim_server['poly']['id']}", wait_until="load")
    page.wait_for_selector("#stage canvas", timeout=60000)
    t = page.inner_text("#incPanel")
    assert "Cutting at the test ridge" in t and "Polygon" in t and "Exact incident boundary." in t and "Synthetic boundary" not in t
    info = page.evaluate("() => { const s = document.querySelector('#view').__sim; return { kind: s.bnd.kind, ha: s.bnd.areaHa, trees: s.trees.length, ring: s.bnd.polygons[0][0].length }; }")
    assert info["kind"] == "polygon" and info["ring"] == len(POLY["coordinates"][0]) and 30 < info["ha"] < 60 and info["trees"] > 100
    # every simulated tree is inside the boundary
    assert page.evaluate("async () => { const g = await import('/assets/js/sim/geo.js'); const s = document.querySelector('#view').__sim; return s.trees.every((t) => g.pointInPolygons(t.x, t.z, s.bnd.polygons)); }") is True
    assert "INCIDENT SIMULATION MODE" in page.inner_text("#modeChip").upper() and "SIMULATED" in page.inner_text("#lab")
    # a point incident
    page.goto(sim_server["base"] + f"/app#/ar-explorer/{sim_server['point']['id']}")
    page.wait_for_function("document.querySelector('#incPanel')?.innerText.includes('Fire seen near')")
    page.wait_for_selector("#stage canvas")
    t = page.inner_text("#incPanel")
    assert "Point" in t and "Synthetic boundary generated from point incident." in t
    assert page.evaluate("() => document.querySelector('#view').__sim.bnd.kind") == "synthetic"
    assert page.input_value("#scn") == "fire"     # a fire incident opens on the fire scenario
    assert problems == []
    ctx.close()


def test_tree_generation_is_deterministic_for_one_incident_and_differs_between_incidents(sim_server, browser):  # noqa: F811
    ctx, page, problems = page_for(browser)
    first = None
    for _ in range(2):   # a fresh load of the same incident gives the same forest
        page.goto(sim_server["base"] + "/app#/")
        page.goto(sim_server["base"] + f"/app#/ar-explorer/{sim_server['poly']['id']}")
        page.wait_for_selector("#stage canvas", timeout=60000)
        page.wait_for_function("document.querySelector('#view').__sim")
        sig = page.evaluate("() => { const t = document.querySelector('#view').__sim.trees; return JSON.stringify([t.length, t.slice(0, 40).map((q) => [q.x.toFixed(3), q.z.toFixed(3), q.s.toFixed(3)])]); }")
        if first is None:
            first = sig
    assert sig == first
    assert page.evaluate("""async (a) => { const g = await import('/assets/js/sim/geo.js'); const b = g.boundaryFor(a[0], 'x'); const s1 = JSON.stringify(g.generateTrees(b, 'x').trees), s2 = JSON.stringify(g.generateTrees(b, 'x').trees), s3 = JSON.stringify(g.generateTrees(b, 'y').trees); return s1 === s2 && s1 !== s3; }""", [POLY]) is True
    ctx.close()


def test_scenarios_play_pause_reset_and_are_labelled_as_simulations(sim_server, browser):  # noqa: F811
    ctx, page, problems = page_for(browser)
    page.goto(sim_server["base"] + f"/app#/ar-explorer/{sim_server['poly']['id']}", wait_until="load")
    page.wait_for_selector("#stage canvas", timeout=60000)
    labels = {"deforestation": "SIMULATED DEFORESTATION — NOT AN OBSERVATION", "fire": "SIMULATED FIRE SPREAD — NOT A FORECAST"}
    for sc, label in labels.items():
        page.select_option("#scn", sc)
        assert page.inner_text("#lab") == label
        assert "0%" in page.inner_text("#prog")
        page.select_option("#spd", "2")
        page.click("#play")
        assert page.is_disabled("#play") and not page.is_disabled("#pause")
        page.wait_for_function("!document.querySelector('#prog').innerText.includes(': 0%')", timeout=15000)
        page.click("#pause")
        assert not page.is_disabled("#play") and page.is_disabled("#pause")
        page.click("#reset")
        assert ": 0%" in page.inner_text("#prog")
        assert page.evaluate("() => document.querySelector('#view').__sim.st.t") == 0
    page.select_option("#scn", "combined")
    assert "SIMULATED FIRE, THEN CLEARING" in page.inner_text("#lab") and page.is_visible("#clrAfter")
    page.evaluate("() => document.querySelector('#view').__sim.setTime(0.95)")
    page.wait_for_function("document.querySelector('#counts').innerText.includes('cleared')")
    assert "all simulated" in page.inner_text("#counts")
    assert "browser-generated simulation" in page.inner_text("#view") and "not real mapping" in page.inner_text("#view")
    assert problems == []
    ctx.close()


def test_region_mode_still_loads_with_its_own_mode_label(sim_server, browser):  # noqa: F811
    ctx, page, problems = page_for(browser)
    page.route("**/api/v1/ar/forest-scenes/*", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(SCENE)))
    page.goto(sim_server["base"] + f"/app#/ar-explorer/{sim_server['region']['id']}", wait_until="load")
    page.wait_for_selector("#stage canvas", timeout=60000)
    assert page.inner_text("#modeChip").lower() == "region analysis mode"
    assert page.locator("#tl").count() == 1 and page.locator("#reg").count() == 1 and page.locator("#place").count() == 1
    page.wait_for_function("document.querySelector('#loadMsg').innerText.includes('simulated tree markers')", timeout=30000)
    ctx.close()


def test_the_explorer_opens_on_a_chooser_of_incidents_and_regions(sim_server, browser):  # noqa: F811
    ctx, page, problems = page_for(browser)
    page.goto(sim_server["base"] + "/app#/ar-explorer", wait_until="load")
    page.wait_for_selector("#fxList a.fx-card-link")
    t = page.inner_text("#view")
    assert "Cutting at the test ridge" in t and "Fire seen near the test village" in t and "Test region" in t and "simulated" in t.lower()
    assert page.locator("#canvas, #stage canvas").count() == 0            # no 3D scene until something is chosen
    page.fill("#fxQ", "ridge")
    assert page.locator("#fxList a.fx-card-link").count() == 1
    page.fill("#fxQ", "")
    page.click('#fxGroups button[data-g="fire"]')
    assert page.locator("#fxList a.fx-card-link").count() == 1 and page.get_attribute('#fxGroups button[data-g="fire"]', "aria-pressed") == "true"
    page.click("#fxList a.fx-card-link")
    page.wait_for_selector("#stage canvas", timeout=60000)
    assert page.inner_text("#modeChip").lower() == "incident simulation mode" and page.input_value("#scn") == "fire"
    page.click("text=All incidents")
    page.wait_for_selector("#fxList")
    # keyboard: the scene takes focus and space plays
    page.click('#fxList a.fx-card-link >> nth=0'); page.wait_for_selector("#stage canvas")
    page.focus("#stage"); page.keyboard.press("Space")
    assert page.is_disabled("#play")
    page.keyboard.press("Space")
    assert not page.is_disabled("#play")
    ctx.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_no_horizontal_overflow_in_the_simulation_layout(sim_server, browser, width):  # noqa: F811
    ctx, page, problems = page_for(browser, width, 900)
    page.goto(sim_server["base"] + f"/app#/ar-explorer/{sim_server['poly']['id']}", wait_until="load")
    page.wait_for_selector("#stage canvas", timeout=60000)
    page.wait_for_timeout(500)
    sw = page.evaluate("() => [document.documentElement.scrollWidth, document.documentElement.clientWidth, document.querySelector('.main-content')?.scrollWidth, document.querySelector('.main-content')?.clientWidth]")
    assert sw[0] <= sw[1] + 1 and (sw[2] or 0) <= (sw[3] or 0) + 1, sw
    ctx.close()


def test_an_unknown_incident_falls_back_to_the_chooser(sim_server, browser):  # noqa: F811
    ctx, page, problems = page_for(browser)
    page.goto(sim_server["base"] + "/app#/ar-explorer/not-a-real-incident", wait_until="load")
    page.wait_for_selector("#fxList")
    assert page.locator("#modeChip").inner_text().lower() == "forest explorer"
    ctx.close()
