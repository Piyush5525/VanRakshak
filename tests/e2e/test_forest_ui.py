"""Browser test of the tree-loss panel on a private server whose throwaway database is seeded with one scan and one spot.

The seeded geometry only exists to drive the interface; nothing here is shown outside this test database.
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
import json, uuid
from datetime import datetime, timedelta, timezone
from app.db.database import init_db, insert, now
from app.services import forestwatch as fw
init_db()
today = datetime.now(timezone.utc).date()
def sq(x, y, s): return {"type": "Polygon", "coordinates": [[[x, y], [x + s, y], [x + s, y + s], [x, y + s], [x, y]]]}
forest = lambda s: sq(81.0, 21.7, s)
snaps = [{"days_ago": d, "date": (today - timedelta(days=d)).isoformat(), "forest_ha": f, "lost_ha": l, "forest": forest(0.04 - 0.002 * i), "lost": sq(81.0, 21.7, 0.002 * i) if l else None} for i, (d, f, l) in enumerate([(90, 100.0, 0.0), (30, 97.0, 3.0), (7, 95.0, 5.0), (0, 93.0, 7.0)])]
run = str(uuid.uuid4())
insert("fc_runs", {"id": run, "status": "COMPLETE", "progress": 1.0, "bbox": [81.0, 21.7, 81.05, 21.75], "window_start": (today - timedelta(days=90)).isoformat(), "window_end": today.isoformat(), "window_days": 90, "params": {}, "result": {"steps": [], "summary": {"loss_ha": 7.0, "patches": 1, "forest_ha": 100.0, "warnings": []}, "timeline": snaps}, "created_by": "seed", "created_at": now(), "finished_at": now()})
fw._ingest(run, [{"geometry": sq(81.01, 21.71, 0.004), "area_ha": 3.4, "est_date": (today - timedelta(days=12)).isoformat(), "date_uncertainty_days": 5.0, "issue_date": today.isoformat(), "evidence": "optical_radar_agree", "score": 2.0, "evidence_mix": {}, "pixels": [[0], [0]]}], "seed")
'''


@pytest.fixture()   # a fresh server and database for each test: one test removes the spot the other needs
def seeded():
    tmp = tempfile.mkdtemp(prefix="rakshak-forest-e2e-")
    port = free_port()
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp}/e2e.db", "REPORT_OUTPUT_DIR": f"{tmp}/r", "RASTER_DIR": f"{tmp}/ras", "MODEL_DIR": f"{tmp}/m", "EVIDENCE_DIR": f"{tmp}/ev", "ENVIRONMENT": "test", "AUTH_MODE": "dev_header", "RATE_LIMIT_PER_MINUTE": "0", "ENABLE_EXAMPLES": "true", "OSM_DISABLED": "true", "PYTHONUTF8": "1", **{k: "" for k in ("MAPBOX_API", "SERP_API", "FIRMS_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "OSRM_URL", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "OMNIDIM_API_KEY", "OMNIDIM_AGENT_ID", "OMNIDIM_FROM_NUMBER_ID", "IVR_RESPONSE_NUMBER", "ALERT_PHONE_NUMBER")}, "ROUTING_PROVIDER": "unconfigured"}
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


def test_one_decision_per_spot_permits_with_exact_details_and_everything_can_be_undone(seeded, browser):  # noqa: F811
    ctx = browser.new_context(viewport={"width": 1500, "height": 950})
    ctx.add_init_script("localStorage.setItem('rk.role','admin')")
    page = ctx.new_page()
    problems: list[str] = []
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.goto(seeded + "/live", wait_until="load")
    # opening the tab shows only the list: no history block, no pins, nothing drawn
    page.wait_for_selector("#forestBody .row-item")
    body = page.inner_text("#forestBody").lower()
    assert "tree cover over time" not in body and "check a new area" in body
    assert not page.is_checked("#showCover") and page.locator(".leaflet-marker-icon").count() == 0
    for d in ("7", "30", "90"):
        page.click(f"#win button[data-d='{d}']")
    page.click("#win button[data-d='c']")
    assert page.locator("#cFrom").count() == 1
    page.click("#win button[data-d='90']")
    # choosing the spot puts it on the map and shows its permit status, the history and ONE decision
    page.click("#forestBody .row-item")
    page.wait_for_selector("#fcEsc")
    assert page.locator(".pin.tree").count() == 1
    text = page.inner_text("#forestBody")
    assert "No permit in the register covers this spot" in text and "not proof that the work is illegal" in text
    assert "tree cover over time" in text.lower() and text.lower().count("illegal") == 1
    assert page.locator("#fcEsc").count() == 1 and page.locator("#fcFalse").count() == 1
    assert page.locator("#cat, #stat, #send, .chn").count() == 0       # the old multi-option controls are gone
    page.click("#snap button[data-s='90']")
    page.wait_for_function("document.querySelector('.stat3').innerText.includes('100.0 ha')")
    assert page.is_checked("#showCover")
    # a permit someone knows about is shown with its exact details, and can be taken away again
    page.click("#pAdd")
    page.fill("#pAuth", "State Forest Department")
    page.fill("#pNo", "FC/2026/114")
    page.fill("#pName", "Quarry access road")
    page.fill("#pFrom", "2026-01-01")
    page.fill("#pTo", "2027-01-01")
    page.fill("#pUrl", "https://example.gov.in/permits/114")
    page.click("#pSave")
    page.wait_for_selector(".permit")
    permit = page.inner_text(".permit")
    assert "FC/2026/114" in permit and "State Forest Department" in permit and "Quarry access road" in permit and "Issued" in permit and "Open the source document" in permit
    assert page.get_attribute(".permit a", "href") == "https://example.gov.in/permits/114"
    page.click("[data-rm]")
    page.wait_for_selector(".banner2.red")
    assert page.locator(".permit").count() == 0
    # Escalate asks once more, then runs the whole routing with a live card; nothing is configured here, so it says so
    page.click("#fcEsc")
    assert "Tap again" in page.inner_text("#fcEsc")
    page.click("#fcEsc")
    page.wait_for_selector(".pop.bad", timeout=60000)
    card = page.inner_text(".pop")
    assert "Escalating FC-" in card and "Telegram is not set up" in card and "Phone calls are not set up" in card
    page.wait_for_function("document.querySelector('#forestBody').innerText.includes('Escalated.')")
    page.click("#fcUndo")
    page.wait_for_selector("#fcFalse")
    # False alarm closes it; undo brings it back; remove deletes it
    page.click("#fcFalse")
    page.wait_for_function("document.querySelector('#forestBody').innerText.includes('Marked as a false alarm')")
    page.click("#fcUndo")
    page.wait_for_selector("#fcEsc")
    page.click("#forestBody details.more summary")
    page.click("#fcDel")
    page.click("#fcDel")
    page.wait_for_function("document.querySelector('#forestBody').innerText.includes('No loss spots')")
    assert problems == []
    ctx.close()


def test_an_area_shows_only_its_own_spots_and_a_grid_of_cells_with_7_30_90_day_loss(seeded, browser):  # noqa: F811
    ctx = browser.new_context(viewport={"width": 1500, "height": 950})
    ctx.add_init_script("localStorage.setItem('rk.role','admin')")
    page = ctx.new_page()
    problems: list[str] = []
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.goto(seeded + "/live", wait_until="load")
    page.wait_for_selector("#areaSel")
    page.click("#win button[data-d='90']")
    value = page.eval_on_selector_all("#areaSel option", "o => o[1].value")
    page.select_option("#areaSel", value)                         # look at one checked area only
    page.wait_for_selector("#forestBody .row-item[data-id]")
    assert "in this area" in page.inner_text("#forestBody").lower()
    page.click("#viewTabs button[data-v=grid]")                   # the same area cut into labelled cells
    page.wait_for_selector("[data-cell]")
    assert "grid cells with loss" in page.inner_text("#forestBody").lower() and page.is_checked("#showCover")
    cell = page.get_attribute("[data-cell]", "data-cell")
    page.click("[data-cell]")
    page.wait_for_selector("#cellBack")
    text = page.inner_text("#forestBody")
    assert f"Cell {cell}" in text and "Last 7 days" in text and "Last 30 days" in text and "Last 90 days" in text and "Tree cover left" in text
    page.click("#forestBody .row-item[data-id]")                  # a spot inside the cell opens, and Back returns to the cell
    page.wait_for_selector("#fcEsc")
    assert f"Back to cell {cell}" in page.inner_text("#fcBack")
    page.click("#fcBack")
    page.wait_for_selector("#cellBack")
    page.click("#cellBack")
    page.wait_for_selector("[data-cell]")
    assert problems == []
    ctx.close()
