"""Browser tests: Live Map to Ranger AR Navigation to the phone page. Routing answers are synthetic (tests only) so the flow is repeatable."""
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
POLY = {"type": "Polygon", "coordinates": [[[81.0, 21.7], [81.004, 21.7], [81.004, 21.704], [81.0, 21.704], [81.0, 21.7]]]}
LINE = {"type": "LineString", "coordinates": [[80.99, 21.69], [80.995, 21.695], [81.002, 21.702]]}
ROUTE = {"index": 0, "status": "ok", "recommendation": "recommended", "distance_km": 2.1, "duration_min": 7.0, "adjusted_duration_min": 7.0, "geometry": LINE, "reasons": [], "segments": [{"mode": "drive", "distance_km": 2.0, "geometry": LINE}], "drive_km": 2.0, "walk_km": 0.1}
PLAN = {"status": "ok", "provider": "test", "origin": {"type": "Point", "coordinates": [80.99, 21.69]}, "origin_source": "nearest registered station by straight line: Test Range Office (3.0 km); availability not known", "destination": {"type": "Point", "coordinates": [81.002, 21.702]},
        "evaluation": {"recommended_index": 0, "routes": [ROUTE], "message": None, "disclaimer": "Test route."}, "caveat": None, "fallback_note": None, "warning": None}
INFO = {"progress": 0.4, "on_route": True, "off_route_m": 3, "distance_remaining_m": 800, "bearing_to_destination_deg": 45.0, "next_step": {"instruction": "Head to the incident", "distance_m": 800, "bearing_deg": 45}, "warnings": [], "arrived": False}


@pytest.fixture(scope="module")
def srv():
    tmp = tempfile.mkdtemp(prefix="rakshak-arnav-e2e-")
    port = free_port()
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp}/e2e.db", "REPORT_OUTPUT_DIR": f"{tmp}/r", "RASTER_DIR": f"{tmp}/ras", "MODEL_DIR": f"{tmp}/m", "EVIDENCE_DIR": f"{tmp}/ev", "INFRA_DIR": f"{tmp}/infra", "ENVIRONMENT": "test", "AUTH_MODE": "dev_header",
           "RATE_LIMIT_PER_MINUTE": "0", "OSM_DISABLED": "true", "PYTHONUTF8": "1", "MOBILE_LINK_SECRET": "test-secret-for-phone-links-0123456789", "ROUTING_PROVIDER": "unconfigured", "OSRM_PUBLIC": "false",
           **{k: "" for k in ("MAPBOX_API", "SERP_API", "FIRMS_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "OSRM_URL", "OMNIDIM_API_KEY", "OMNIDIM_AGENT_ID", "ALERT_PHONE_NUMBER")}}
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
    i = httpx.post(base + "/api/v1/incidents", headers=HDR, json={"title": "Cutting near the test ridge", "geometry": POLY, "evidence": {"kind": "cutting"}}, timeout=30).json()
    httpx.post(f"{base}/api/v1/incidents/{i['id']}/review", headers=HDR, json={"decision": "approve", "notes": ""}, timeout=30)
    pt = httpx.post(base + "/api/v1/incidents", headers=HDR, json={"title": "Fire seen at the test village", "geometry": {"type": "Point", "coordinates": [81.02, 21.72]}, "evidence": {"kind": "fire"}}, timeout=30).json()
    yield {"base": base, "poly": i, "point": pt}
    proc.kill()


def stub_routes(page):
    page.route("**/api/v1/navigation/routes", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(PLAN)))


def page_for(browser, w=1400, h=950, **ctx_kw):  # noqa: F811
    ctx = browser.new_context(viewport={"width": w, "height": h}, **ctx_kw)
    ctx.add_init_script("localStorage.setItem('rk.role','admin')")
    page = ctx.new_page()
    problems: list[str] = []
    page.on("pageerror", lambda e: problems.append(str(e)) if "setItem" not in str(e) else None)
    return ctx, page, problems


def test_live_map_incident_opens_ranger_ar_navigation(srv, browser):  # noqa: F811
    ctx, page, problems = page_for(browser)
    stub_routes(page)
    page.goto(srv["base"] + "/live", wait_until="load")
    page.click("#rtabs [data-t=inc]")
    page.wait_for_selector(f'#rightBody .row-item[data-id="{srv["poly"]["id"]}"]')
    page.click(f'#rightBody .row-item[data-id="{srv["poly"]["id"]}"]')
    page.wait_for_selector("#arNav")
    assert "Open Ranger AR Navigation" in page.inner_text("#arNav") and page.get_attribute("#arNav", "href") == f"/app#/ar-navigation/{srv['poly']['id']}"
    assert page.locator("#sim3d").count() == 1          # the 3D simulation button stays above it
    page.click("#arNav")
    page.wait_for_url(f"**/app#/ar-navigation/{srv['poly']['id']}")
    page.wait_for_selector("#inc")
    assert page.input_value("#inc") == srv["poly"]["id"]
    t = page.inner_text("#incInfo")
    assert "Cutting near the test ridge" in t and "Polygon" in t and "Illegal cutting" in t
    assert problems == []
    ctx.close()


def test_navigation_draws_the_polygon_the_route_the_station_and_the_qr(srv, browser):  # noqa: F811
    ctx, page, problems = page_for(browser, permissions=["clipboard-read", "clipboard-write"])
    stub_routes(page)
    page.goto(srv["base"] + f"/app#/ar-navigation/{srv['poly']['id']}", wait_until="load")
    page.wait_for_selector("#qrImg svg", timeout=30000)
    assert page.evaluate("() => { const m = document.querySelector('#view').__navMap; let polys = 0, lines = 0; m.eachLayer((l) => { if (l instanceof L.Polygon) polys++; else if (l instanceof L.Polyline) lines++; }); return [polys, lines]; }")[0] >= 1
    assert page.evaluate("() => { const m = document.querySelector('#view').__navMap; let lines = 0; m.eachLayer((l) => { if (l instanceof L.Polyline && !(l instanceof L.Polygon)) lines++; }); return lines; }") >= 1
    assert "Test Range Office" in page.inner_text("#station")
    txt = page.inner_text("#routes")
    assert "Route 1" in txt
    url = page.inner_text("#qrUrl").strip()
    assert "/mobile/ar/" in url and page.inner_text("#qrPanel").count("Open on ranger phone") == 1
    assert "localhost" in page.inner_text("#qrWarn").lower() or "127.0.0.1" in url
    assert "minutes" in page.inner_text("#qrExp") and "Privacy" in page.inner_text("#qrPanel")
    page.click("#qrCopy")
    assert page.evaluate("() => navigator.clipboard.readText()") == url
    assert page.get_attribute("#qrOpen", "href") == url
    assert page.locator("#qrImg svg path").count() == 1 and len(page.inner_html("#qrImg")) > 2000      # a real, dense QR drawing
    assert problems == []
    ctx.close()


def test_a_point_incident_is_a_marker_and_an_unapproved_one_cannot_make_a_phone_link(srv, browser):  # noqa: F811
    ctx, page, problems = page_for(browser)
    stub_routes(page)
    page.goto(srv["base"] + f"/app#/ar-navigation/{srv['point']['id']}", wait_until="load")
    page.wait_for_selector("#approve")
    assert "Point" in page.inner_text("#incInfo") and "not approved" in page.inner_text("#approveBox").lower()
    page.wait_for_function("document.querySelector('#qrBody').innerText.includes('Approve the incident')")
    assert page.locator("#qrImg").count() == 0
    page.click("#approve")                                  # a reviewer can approve it right here, and the QR appears
    page.wait_for_selector("#qrImg svg", timeout=30000)
    ctx.close()


def test_routing_unavailable_keeps_the_incident_on_the_map(srv, browser):  # noqa: F811
    ctx, page, problems = page_for(browser)
    page.goto(srv["base"] + f"/app#/ar-navigation/{srv['poly']['id']}", wait_until="load")   # no stub: the server has no routing provider
    page.wait_for_selector("#routes .warn-box")
    assert "inc" and page.locator("#inc").count() == 1 and page.locator("#qrImg").count() == 0
    assert page.evaluate("() => { let n = 0; document.querySelector('#view').__navMap.eachLayer((l) => { if (l instanceof L.Polygon) n++; }); return n; }") >= 1
    ctx.close()


def mobile_url(srv, route=0):
    return httpx.post(srv["base"] + "/api/v1/navigation/mobile-links", headers=HDR, json={"incident_id": srv["poly"]["id"], "route_index": route}, timeout=30).json()["url"]


def stub_phone_api(page, sink):
    def handle(route, request):
        u = request.url
        if u.endswith("/session"):
            body = {"session_id": "s1", "route": ROUTE, "evaluation": PLAN["evaluation"], "destination": PLAN["destination"], "privacy": "x"}
        elif u.endswith("/position"):
            body = INFO
        elif u.endswith("/reroute"):
            body = {"rerouted": True, "route": ROUTE, "evaluation": PLAN["evaluation"]}
        elif u.endswith("/end"):
            sink.append(u)
            body = {"session_id": "s1", "summary": {}}
        else:
            return route.continue_()
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/v1/mobile/ar/*/session*", handle)
    page.route("**/api/v1/mobile/ar/*/sessions/**", handle)


def test_phone_page_guides_with_a_working_arrow_and_ends_the_session(srv, browser):  # noqa: F811
    ctx, page, problems = page_for(browser, 390, 800, geolocation={"latitude": 21.69, "longitude": 80.99, "accuracy": 45}, permissions=["geolocation"])
    ended: list[str] = []
    stub_phone_api(page, ended)
    page.goto(mobile_url(srv), wait_until="load")
    page.wait_for_selector("#s-start:not([hidden])")
    assert "Cutting near the test ridge" in page.inner_text("#incTitle") and "An area (polygon)" in page.inner_text("#incGeom")
    assert page.is_disabled("#go")
    page.check("#consent")
    page.click("#go")
    page.wait_for_selector("#s-guide:not([hidden])")
    page.wait_for_function("document.querySelector('#hInstr').innerText.includes('Head to the incident')")
    assert "Camera unavailable" in page.inner_text("#hMode")               # headless has no camera: the fallback shows
    assert "Compass unavailable" in page.inner_text("#hCompass")
    t = page.inner_text("#s-guide")
    assert "to the incident" in t and "bearing" in t and "GPS accuracy" in t and "Route progress 40%" in t and "GPS accuracy is poor" in t       # accuracy 45 m is poor
    assert "Browser guidance can be inaccurate under canopy" in t
    assert page.evaluate("() => getComputedStyle(document.querySelector('#arrow')).transform") != "none"
    sw = page.evaluate("() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert sw[0] <= sw[1], sw
    for sel in ("#hRe", "#hExit"):
        assert page.locator(sel).bounding_box()["height"] >= 44
    page.click("#hRe")
    page.click("#hExit")
    page.wait_for_selector("#s-end:not([hidden])")
    assert len(ended) == 1 and "Your last position was deleted" in page.inner_text("#s-end")
    ctx.close()


def test_phone_page_without_gps_offers_manual_position_and_no_horizontal_scroll(srv, browser):  # noqa: F811
    ctx, page, problems = page_for(browser, 390, 800)      # no geolocation permission granted
    page.goto(mobile_url(srv), wait_until="load")
    page.wait_for_selector("#s-start:not([hidden])")
    page.check("#consent")
    page.click("#go")
    page.wait_for_function("document.querySelector('#startMsg').innerText.length > 0 && !document.querySelector('#manualBox').hidden", timeout=30000)
    assert any(w in page.inner_text("#startMsg").lower() for w in ("denied", "unavailable", "no gps"))
    sw = page.evaluate("() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert sw[0] <= sw[1], sw
    ctx.close()


def test_phone_page_refuses_a_bad_link(srv, browser):  # noqa: F811
    ctx, page, problems = page_for(browser, 390, 800)
    page.goto(srv["base"] + "/mobile/ar/not-a-valid-token", wait_until="load")
    page.wait_for_selector("#s-error:not([hidden])")
    assert "cannot be used" in page.inner_text("#s-error") and page.is_hidden("#s-start")
    ctx.close()


def test_existing_pages_still_render(srv, browser):  # noqa: F811
    ctx, page, problems = page_for(browser)
    for hash_ in ("#/overview", "#/ar-explorer", "#/investigations", f"#/investigations/{srv['poly']['id']}", f"#/ar-explorer/{srv['poly']['id']}"):
        page.goto(srv["base"] + "/app" + hash_)
        page.wait_for_selector("#view > *", timeout=30000)
    assert page.locator("#openAr").count() == 1 and "Open Ranger AR Navigation" in page.inner_text("#openAr")
    page.goto(srv["base"] + f"/app#/investigations/{srv['poly']['id']}")
    page.wait_for_selector("#openAr")
    assert page.get_attribute("#openAr", "href") == f"#/ar-navigation/{srv['poly']['id']}" and page.get_attribute("#openSim", "href") == f"#/ar-explorer/{srv['poly']['id']}"
    assert problems == []
    ctx.close()
