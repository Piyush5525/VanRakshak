"""Browser test of the factory check: upload a PDF, see the details found, place it, run the check and step through 7 / 30 / 90 days."""
import io

import httpx
import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from tests.e2e.test_ui_smoke import browser, server  # noqa: F401  (fixtures)

LINES = ["Project: Hilltop Cement Unit", "Cement clinker grinding unit, capacity 1 MTPA", "Total plot area 20 hectares", "Stack height 80 m", "Water requirement 900 KLD", "SO2 60 mg/Nm3 at the stack", "Latitude: 11.6050, Longitude: 76.0200", "Environmental Clearance is pending."]


def _pdf() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for ln in LINES:
        c.drawString(50, y, ln)
        y -= 18
    c.save()
    return buf.getvalue()


def _network_ok() -> bool:
    try:
        return httpx.get("https://planetarycomputer.microsoft.com/api/stac/v1/", timeout=10).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.mark.skipif(not _network_ok(), reason="needs network for land cover and wind")
def test_pdf_fills_the_form_and_the_check_shows_the_first_90_days(server, browser, tmp_path):  # noqa: F811
    f = tmp_path / "plan.pdf"
    f.write_bytes(_pdf())
    ctx = browser.new_context(viewport={"width": 1500, "height": 950})
    ctx.add_init_script("localStorage.setItem('rk.role','admin')")
    page = ctx.new_page()
    problems: list[str] = []
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.goto(server + "/live", wait_until="load")
    page.click("#rtabs button[data-t=inc]")
    page.click("#facNew")
    page.wait_for_selector("#fName")
    assert page.is_disabled("#facRun")                      # nothing can be run before a place and a name exist
    page.click("#fMode button[data-m=pdf]")
    page.set_input_files("#fFile", str(f))
    page.wait_for_function("document.querySelector('#fName').value.includes('Hilltop')", timeout=20000)
    assert page.input_value("#fKind") == "cement" and page.input_value("#fStack") == "80" and page.input_value("#fWater") == "900"
    assert "from document" in page.inner_text("#rightBody")
    page.click("#fUseLoc")                                   # the document names its own coordinates
    page.wait_for_function("!document.querySelector('#facRun').disabled")
    page.click("#facRun")
    page.wait_for_selector("#fday", timeout=240000)
    text = page.inner_text("#rightBody")
    assert "Hilltop Cement Unit" in text and "what this does to the surroundings" in text.lower() and "which way the wind blows" in text.lower()
    for d in ("7", "30", "90"):
        page.click(f"#fday button[data-d='{d}']")
        assert f"After {d} days" in page.inner_text("#rightBody")
    assert page.locator("#fpdf").count() == 1 and page.locator("#ftrack").count() == 1
    assert problems == []
    ctx.close()
