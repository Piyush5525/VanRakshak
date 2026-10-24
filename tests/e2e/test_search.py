"""Browser test of the place search. The geocoding service is intercepted in the browser, so the test never calls it."""
import json

from tests.e2e.test_ui_smoke import browser, server  # noqa: F401  (fixtures)

JAIPUR = [{"name": "Jaipur", "display_name": "Jaipur, Rajasthan, India", "lat": "26.9154", "lon": "75.8189", "boundingbox": ["26.78", "27.05", "75.65", "75.95"], "addresstype": "city"},
          {"name": "Jaipur mandal", "display_name": "Jaipur mandal, Mancherial, Telangana, India", "lat": "18.82", "lon": "79.51", "boundingbox": ["18.7", "18.9", "79.4", "79.6"], "addresstype": "county"}]


def test_typing_the_first_letters_suggests_places_and_the_map_jumps_to_the_choice(server, browser):  # noqa: F811
    ctx = browser.new_context(viewport={"width": 1500, "height": 900})
    page = ctx.new_page()
    problems: list[str] = []
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    seen: list[str] = []

    def fake(route):
        seen.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(JAIPUR if "jaip" in route.request.url.lower() else []))

    page.route("**/nominatim.openstreetmap.org/search**", fake)
    page.goto(server + "/live", wait_until="load")
    page.wait_for_selector("#q")
    page.click("#q")
    assert "Use my location" in page.inner_text("#sugg")                 # empty box: offer where you are
    page.fill("#q", "jaip")
    page.wait_for_selector("#sugg li >> text=Jaipur")                    # suggestions appear as you type
    assert page.locator("#sugg li").count() == 2 and "Rajasthan" in page.inner_text("#sugg")
    assert page.locator("#sugg li.on").count() == 1                      # the first is ready for Enter
    page.keyboard.press("ArrowDown")
    assert "Telangana" in page.inner_text("#sugg li.on")
    page.keyboard.press("ArrowUp")
    page.keyboard.press("Enter")                                         # Enter picks the highlighted one
    page.wait_for_function("Math.abs(rkMap.getCenter().lat - 26.9) < 0.3 && Math.abs(rkMap.getCenter().lng - 75.8) < 0.3", timeout=10000)
    assert page.locator(".leaflet-popup").count() == 1 and "Jaipur" in page.inner_text(".leaflet-popup")
    assert page.input_value("#q") == "Jaipur" and not page.is_hidden("#qClear")
    page.click("#pinX")                                                  # the pin can be taken away again
    page.wait_for_function("document.querySelectorAll('.leaflet-popup').length === 0")
    assert page.input_value("#q") == ""
    # coordinates work too, and a search with no match says so instead of failing silently
    page.fill("#q", "11.6, 76.0")
    page.wait_for_selector("#sugg li >> text=coordinates")
    page.keyboard.press("Enter")
    page.wait_for_function("Math.abs(rkMap.getCenter().lat - 11.6) < 0.05 && Math.abs(rkMap.getCenter().lng - 76.0) < 0.05", timeout=10000)
    page.fill("#q", "zzzz")
    page.wait_for_selector("#sugg li >> text=No place found")
    page.keyboard.press("Escape")
    assert page.is_hidden("#sugg")
    page.keyboard.press("Escape")
    page.click("#views")                                                 # leave the box, then "/" brings the cursor back to it
    page.keyboard.press("/")
    assert page.evaluate("document.activeElement.id") == "q"
    assert all("countrycodes=in" in u for u in seen) and problems == []
    ctx.close()
