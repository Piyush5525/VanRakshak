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


@pytest.fixture(scope="module")
def seeded():
    tmp = tempfile.mkdtemp(prefix="rakshak-forest-e2e-")
    port = free_port()
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp}/e2e.db", "REPORT_OUTPUT_DIR": f"{tmp}/r", "RASTER_DIR": f"{tmp}/ras", "MODEL_DIR": f"{tmp}/m", "EVIDENCE_DIR": f"{tmp}/ev", "ENVIRONMENT": "test", "AUTH_MODE": "dev_header", "RATE_LIMIT_PER_MINUTE": "0", "PYTHONUTF8": "1", **{k: "" for k in ("MAPBOX_API", "SERP_API", "FIRMS_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "OSRM_URL", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN")}, "ROUTING_PROVIDER": "unconfigured"}
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


def test_tree_loss_panel_shows_history_lets_you_edit_a_spot_and_alerts_inline(seeded, browser):  # noqa: F811
    ctx = browser.new_context(viewport={"width": 1500, "height": 950})
    ctx.add_init_script("localStorage.setItem('rk.role','admin')")
    page = ctx.new_page()
    problems: list[str] = []
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.goto(seeded + "/live", wait_until="load")
    # the tree panel is the front door: three layer switches off, nothing drawn, no event-log clutter
    page.wait_for_selector("#forestBody .row-item")
    assert not page.is_checked("#showCover") and page.locator(".leaflet-marker-icon").count() == 0
    assert page.locator("#logBtn, #dock").count() == 0
    # step through the seeded history: choosing a step turns the history layer on
    page.click("#snap button[data-s='90']")
    page.wait_for_function("document.querySelector('.stat3').innerText.includes('100.0 ha')")
    assert page.is_checked("#showCover")
    page.click("#snap button[data-s='30']")
    page.wait_for_function("document.querySelector('.stat3').innerText.includes('97.0 ha')")
    page.click("#snap button[data-s='0']")
    page.wait_for_function("document.querySelector('.stat3').innerText.includes('93.0 ha')")
    for d in ("7", "30", "90"):
        page.click(f"#win button[data-d='{d}']")
    page.click("#win button[data-d='c']")
    assert page.locator("#cFrom").count() == 1
    page.click("#win button[data-d='90']")
    # open the spot: it appears on the map, and the permit check says "no record found" without calling it illegal
    assert page.locator(".pin.tree").count() == 0
    page.click("#forestBody .row-item")
    page.wait_for_selector("#stat")
    assert page.locator(".pin.tree").count() == 1
    text = page.inner_text("#forestBody")
    assert "No government record" in text and "NOT mean the work is illegal" in text
    assert text.lower().count("illegal") == 1   # only inside the "does NOT mean illegal" warning
    assert "First mark it" in text and "practice" not in text.lower() and "simulat" not in text.lower()
    page.select_option("#cat", "suspected_felling")
    page.click("#stat button[data-s='CONFIRMED']")
    page.wait_for_function("!document.querySelector('#forestBody').innerText.includes('First mark it')")
    # the phone message is written out in full
    page.click("#forestBody details summary")
    page.wait_for_function("document.querySelector('#script').innerText.includes('tree cover lost')", timeout=15000)
    # sending takes two taps; the result appears right under the button
    page.click("#send")
    assert "Tap again" in page.inner_text("#send")
    page.click("#send")
    page.wait_for_selector("#sendRes .res")
    assert "Dashboard" in page.inner_text("#sendRes") and "sent" in page.inner_text("#sendRes")
    page.wait_for_function("document.querySelector('#recAct').innerText.includes('dashboard')", timeout=15000)
    # dismiss it as not a loss: it leaves the list
    page.click("#stat button[data-s='FALSE_POSITIVE']")
    page.click("#back")
    page.wait_for_function("document.querySelector('#forestBody').innerText.includes('No loss spots')")
    assert problems == []
    ctx.close()
