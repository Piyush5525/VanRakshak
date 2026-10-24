"""Browser test of the fire-incident flow on a private server and temp database (no FIRMS/Telegram/Twilio/Mapbox keys)."""
import httpx
import pytest

from tests.e2e.test_ui_smoke import browser, server  # noqa: F401  (fixtures)


def _network_ok() -> bool:
    try:
        return httpx.get("https://planetarycomputer.microsoft.com/api/stac/v1/", timeout=10).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.mark.skipif(not _network_ok(), reason="needs network for the DEM and land-cover inputs")
def test_example_incidents_are_chosen_one_by_one_and_the_map_stays_clear_until_then(server, browser):  # noqa: F811
    ctx = browser.new_context(viewport={"width": 1500, "height": 950})
    ctx.add_init_script("localStorage.setItem('rk.role','admin')")
    page = ctx.new_page()
    problems: list[str] = []
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.goto(server + "/live", wait_until="load")
    # nothing is drawn on a fresh map: all layers are off and there are no pins
    assert not page.is_checked("#showFires") and not page.is_checked("#showHeat") and not page.is_checked("#showCover")
    assert page.locator(".leaflet-marker-icon").count() == 0
    page.click("#rtabs button[data-t=fire]")
    page.wait_for_selector("#loadScn")
    page.click("#loadScn")
    page.wait_for_function("document.querySelectorAll('#fireBody .row-item').length >= 5", timeout=30000)
    assert page.locator(".leaflet-marker-icon").count() == 0   # listing incidents does not put them on the map
    text = page.inner_text("#fireBody").lower()
    assert "demo" not in text and "simulat" not in text and "synthetic" not in text
    # choosing one shows it on the map, once its forecast is ready
    page.click("#fireBody .row-item >> nth=0")
    page.wait_for_selector("#stabs", timeout=30000)
    page.wait_for_function("document.querySelector('#fireBody').innerText.includes('Confirmed')", timeout=240000)
    assert page.locator(".leaflet-marker-icon").count() >= 1
    page.click("#stabs button[data-k=forecast]")
    page.wait_for_selector("#hz")
    txt = page.inner_text("#pane")
    assert "PREDICTED RISK FOOTPRINT, not a fire perimeter" in txt and "ha" in txt
    page.click("#hz button[data-h='3']")
    assert page.locator("#hz button.on").inner_text() == "3 h"
    # alert tab: the call text is written out, and what is missing for real sending is said plainly
    page.click("#stabs button[data-k=comms]")
    page.wait_for_function("document.querySelector('#fireBody').innerText.includes('What the call says')")
    page.click("#pane details summary")
    page.wait_for_function("document.querySelector('#script').innerText.includes('forest fire has been detected')", timeout=20000)
    script = page.inner_text("#script")
    assert "degrees north" in script and "hectares" in script
    assert page.locator("#tgreal").is_disabled() and page.locator("#callnow").is_disabled()   # no Telegram / Twilio keys on this server
    assert "Phone calls are not set up yet" in page.inner_text("#pane")
    assert page.locator("#hear").count() == 1
    assert page.locator("#logBtn, #dock").count() == 0
    # back to the list: the map is clear again, and the examples can be removed without touching real incidents
    page.click("#fback")
    assert page.locator(".leaflet-marker-icon").count() == 0
    page.click("#rmScn")
    page.click("#sheet [data-a='0']")
    page.wait_for_function("!document.querySelector('#fireBody').innerText.includes('Scenario')", timeout=10000)
    assert problems == []
    ctx.close()
