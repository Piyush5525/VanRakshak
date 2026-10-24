"""Browser smoke test: boots a private server on a temp database and opens every console page.

Skipped automatically when Playwright or Google Chrome is not installed. Pages that need live external
data at load (Data Sources, AR Forest Explorer) are exercised manually, not here.
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[2]
PAGES = ["overview", "monitoring", "change", "alerts", "forecasts", "investigations", "wildfire", "rangers", "environment", "industrial", "reports", "settings", "ar-navigation"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    tmp = tempfile.mkdtemp(prefix="rakshak-e2e-")
    port = free_port()
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp}/e2e.db", "REPORT_OUTPUT_DIR": f"{tmp}/r", "RASTER_DIR": f"{tmp}/ras", "MODEL_DIR": f"{tmp}/m", "EVIDENCE_DIR": f"{tmp}/ev", "ENVIRONMENT": "test", "AUTH_MODE": "dev_header", "RATE_LIMIT_PER_MINUTE": "0", "PYTHONUTF8": "1", **{k: "" for k in ("MAPBOX_API", "SERP_API", "FIRMS_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "OSRM_URL", "OMNIDIM_API_KEY", "OMNIDIM_AGENT_ID", "OMNIDIM_FROM_NUMBER_ID", "IVR_RESPONSE_NUMBER", "ALERT_PHONE_NUMBER")}, "ROUTING_PROVIDER": "unconfigured"}
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


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chrome not available: {exc}")
        yield b
        b.close()


def test_every_page_renders_without_script_errors_on_an_empty_database(server, browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    ctx.add_init_script("localStorage.setItem('rk.role','admin')")
    page = ctx.new_page()
    problems: list[str] = []
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.on("response", lambda r: problems.append(f"{r.status} {r.url}") if r.status >= 500 or (r.status >= 400 and "/api/" in r.url) else None)
    for name in PAGES:
        page.goto(f"{server}/app#/{name}", wait_until="networkidle")
        page.wait_for_timeout(600)
        assert page.inner_text("#pageTitle").strip(), name
        assert "Something went wrong" not in page.inner_text("#view"), f"{name}: {page.inner_text('#view')[:200]}"
    assert problems == []
    # empty states must be honest rather than showing sample data
    page.goto(f"{server}/app#/overview", wait_until="networkidle")
    page.wait_for_timeout(500)
    text = page.inner_text("#view")
    assert "Not available" in text  # spatial risk model must read as unavailable, not as a placeholder
    assert "0 ha" in text and "1,930" not in text  # no stored or sample figures leak into an empty system
    ctx.close()


def test_landing_and_console_routes(server, browser):
    page = browser.new_page()
    page.goto(server + "/", wait_until="load")
    assert "VanRakshak" in page.title() and page.locator("a.primary").count() == 1 and page.locator("a.ghost").count() == 1
    page.click("a.ghost")
    page.wait_for_url("**/app")
    page.wait_for_selector("#nav .nav-item")
    assert page.locator("#nav .nav-item").count() == 16  # 15 pages + the Live map link
    page.close()


def test_mobile_layout_has_menu_and_no_horizontal_scroll(server, browser):
    ctx = browser.new_context(viewport={"width": 390, "height": 800})
    page = ctx.new_page()
    page.goto(f"{server}/app#/overview", wait_until="networkidle")
    assert page.locator("#menuBtn").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2")
    page.click("#menuBtn")
    assert "open" in page.get_attribute("#sidebar", "class")
    ctx.close()
