"""Browser test of the live map on a private server and temp database (no FIRMS key, so live fires must read as unavailable)."""
import pytest

from tests.e2e.test_ui_smoke import browser, server  # noqa: F401  (fixtures)


def test_live_map_marks_an_incident_adds_factory_info_and_downloads_a_report(server, browser):  # noqa: F811
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
    page = ctx.new_page()
    problems: list[str] = []
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.goto(server + "/live", wait_until="load")
    page.wait_for_selector(".brand img")
    assert page.inner_text(".brand strong") == "VanRakshak"
    assert not page.is_checked("#showFires")   # the NASA layer is off until asked for
    page.click("label.layer:has(#showFires)")
    page.wait_for_selector("#fireList .empty, #fireList .row-item")
    assert "unavailable" in page.inner_text("#fireList").lower()  # no FIRMS key here: say so, show no sample fires
    assert page.inner_text("#fireCount").strip() == "–"
    assert page.locator("#logBtn").count() == 0   # the global event-log dock is gone
    page.click("#rtabs button[data-t=inc]")
    page.click("#mark")
    page.mouse.click(700, 450)
    page.wait_for_selector("#nt")
    page.select_option("#nk", "pollution")
    page.fill("#nt", "Smoke near the river")
    page.click("#save")
    page.wait_for_selector("#rep")
    page.click("#siteAdd")
    page.fill("#sn", "Test Works")
    page.fill(".em .p", "SO2")
    page.fill(".em .v", "50")
    page.fill(".em .u", "mg/Nm3")
    page.fill(".em .s", "Consent order")
    page.click("#ss")
    page.wait_for_selector("#siteEdit")
    assert "not verified" in page.inner_text("#siteBox")
    with page.expect_download() as dl:
        page.click("#rep")
    assert dl.value.suggested_filename.endswith(".pdf")
    # the same two decisions as everywhere else, and both can be undone
    assert page.locator("#incEsc").count() == 1 and page.locator("#incFalse").count() == 1
    page.click("#incFalse")
    page.wait_for_function("document.querySelector('#rightBody').innerText.includes('Marked as a false alarm')")
    page.click("#incUndo")
    page.wait_for_selector("#incEsc")
    page.click("#incBack")
    assert page.locator("#rightBody .row-item").count() == 1
    assert problems == []
    ctx.close()


def test_landing_leads_to_the_live_map(server, browser):  # noqa: F811
    page = browser.new_page()
    page.goto(server + "/", wait_until="load")
    page.click("a.primary")
    page.wait_for_url("**/live")
    assert page.locator("#map").count() == 1
    page.close()
