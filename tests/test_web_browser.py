"""The page itself, driven in a real browser.

What a map and a table do with a mouse cannot be checked from Python alone:
that a marker drags, that a click on a marker picks its row, that the tool
buttons behave as one exclusive set, that Escape puts a tool down. These run
the real server on a free port with the synthetic export loaded and drive
Chromium with Playwright.

They need a Chromium that Playwright can launch. `uv run playwright install
chromium` provides one; YARDSURVEY_CHROMIUM can point at an existing binary
instead. Without either they skip, saying so.
"""

import os
import socket
import threading
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("playwright.sync_api")

from gpsrtk.app import AppState                          # noqa: E402
from gpsrtk.plan import Plan                             # noqa: E402
from gpsrtk.site import example_site                     # noqa: E402
from gpsrtk.web import create_app                        # noqa: E402

pytestmark = pytest.mark.browser

E0, N0 = 449712.0, 4604565.0          # local (12, 15) on the example site


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            b = p.chromium.launch(
                executable_path=os.environ.get("YARDSURVEY_CHROMIUM") or None,
                # Software WebGL, so the 3D view renders on machines and CI
                # runners with no GPU.
                args=["--use-gl=swiftshader", "--enable-webgl",
                      "--ignore-gpu-blocklist"])
        except Exception as exc:                        # noqa: BLE001
            pytest.skip("no Chromium for Playwright - run `uv run playwright "
                        "install chromium` or set YARDSURVEY_CHROMIUM "
                        f"({str(exc).splitlines()[0]})")
        yield b
        b.close()


@pytest.fixture(scope="module")
def live(synthetic_zip, tmp_path_factory):
    import uvicorn

    state = AppState(site=example_site(),
                     cache_dir=tmp_path_factory.mktemp("cache"))
    state.load(synthetic_zip)
    app = create_app(state)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started:
        assert time.time() < deadline, "server did not start"
        time.sleep(0.05)
    yield SimpleNamespace(url=f"http://127.0.0.1:{port}/", state=state,
                          server=app.state.server, export=synthetic_zip)
    server.should_exit = True
    thread.join(10)


def _reset(live):
    with live.server.acting():
        live.state.plan = Plan()
        live.state.vertical = None
        live.state.hidden_sessions = set()
        live.state.surface_from_shown = False
        live.state.plan_changed()
        live.state.recompute()


@pytest.fixture
def page(browser, live):
    _reset(live)
    context = browser.new_context(viewport={"width": 1500, "height": 900})
    pg = context.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(live.url)
    pg.wait_for_function("() => window.yardsurvey?.store.state?.has_data")
    pg.wait_for_timeout(400)            # the view fits itself to the lot
    yield pg
    context.close()
    assert not errors, errors


# --- helpers ------------------------------------------------------------------------

def seed(live, page, *points, measured=()):
    """Plan points (projected), then show them in the page."""
    with live.server.acting():
        for e, n in points:
            live.state.plan.add_point(e, n)
        for number in measured:
            p = live.state.plan.by_number(number)
            p.observed_e, p.observed_n = p.planned_e + 1.5, p.planned_n - 0.8
            p.method, p.fix = "rtk", 4
        live.state.plan_changed()
    page.evaluate("() => window.yardsurvey.refresh()")
    page.wait_for_timeout(300)


def to_screen(page, x, y):
    """Local metres -> page pixels."""
    return page.evaluate("""([x, y]) => {
        const map = window.yardsurvey.map;
        const px = map.getPixelFromCoordinate([x, y]);
        const box = map.getTargetElement().getBoundingClientRect();
        return [box.left + px[0], box.top + px[1]];
    }""", [x, y])


def to_local(page, sx, sy):
    return page.evaluate("""([sx, sy]) => {
        const map = window.yardsurvey.map;
        const box = map.getTargetElement().getBoundingClientRect();
        return map.getCoordinateFromPixel([sx - box.left, sy - box.top]);
    }""", [sx, sy])


def marker(page, number):
    p = next(q for q in page.evaluate("() => window.yardsurvey.store.state.plan.points")
             if q["number"] == number)
    return to_screen(page, p["x"], p["y"])


def pressed(page):
    return page.locator("#panel-plan .tools button[aria-pressed=true]").inner_text()


def tool(page, mode):
    page.click(f"#panel-plan .tools button[data-mode={mode}]")


def hint(page):
    return page.locator("#panel-plan .group .muted").first.inner_text()


def row_cell(page, number, field):
    return page.locator(
        f"#plan-table .tabulator-row:has(.tabulator-cell[tabulator-field=number]:text-is('{number}')) "
        f".tabulator-cell[tabulator-field={field}]")


def drag(page, start, end):
    page.mouse.move(*start)
    page.mouse.down()
    page.mouse.move(*end, steps=8)
    page.mouse.up()
    page.wait_for_timeout(500)


# --- the page ----------------------------------------------------------------------------

def test_the_page_loads_with_its_menus_and_panels(page):
    assert page.title() == "Yard Survey"
    menus = page.locator("#menubar .menu-root > button").all_inner_texts()
    assert menus == ["File", "Data", "Datum", "Plan", "Export", "View"]
    assert "crossover residuals" in page.locator("#qc").inner_text()
    assert page.locator("#panel-layers input[type=radio]:checked").count() == 1


def test_the_page_fits_a_laptop_screen(browser, live):
    """The smallest screen this needs to run on, with the map still usable."""
    context = browser.new_context(viewport={"width": 1366, "height": 768})
    pg = context.new_page()
    pg.goto(live.url)
    pg.wait_for_function("() => window.yardsurvey?.store.state?.has_data")
    width = pg.evaluate("() => document.documentElement.scrollWidth")
    box = pg.locator("#map").bounding_box()
    context.close()
    assert width <= 1366
    assert box["width"] > 450 and box["height"] > 300


def test_a_dock_can_be_dragged_narrow_and_its_contents_scroll(page):
    splitter = page.locator('.splitter[data-side="left"]').bounding_box()
    drag(page, (splitter["x"] + 2, splitter["y"] + 200), (40, splitter["y"] + 200))
    left = page.locator("#left").bounding_box()
    assert left["width"] == pytest.approx(210, abs=2), "stops at the minimum"
    assert page.locator("#left").evaluate("el => getComputedStyle(el).overflowY") == "auto"


# --- the drawing tools ------------------------------------------------------------------------

def test_tools_are_exclusive_and_navigate_is_the_default(page):
    assert pressed(page) == "Navigate"
    tool(page, "add_point")
    assert pressed(page) == "+ Point"
    assert page.locator("#panel-plan .tools button[aria-pressed=true]").count() == 1


def test_clicking_the_active_tool_puts_it_down(page):
    """There was no way to leave a placing mode except to hunt for Navigate."""
    tool(page, "add_point")
    tool(page, "add_point")
    assert pressed(page) == "Navigate"


def test_the_point_tool_places_shots_where_clicked(page, live):
    tool(page, "add_point")
    for target in ((10.0, 12.0), (20.0, 18.0)):
        page.mouse.click(*to_screen(page, *target))
        page.wait_for_timeout(400)
    placed = [live.state.site.to_local(p.planned_e, p.planned_n)
              for p in live.state.plan.points]
    assert placed == [pytest.approx((10.0, 12.0), abs=0.05),
                      pytest.approx((20.0, 18.0), abs=0.05)]
    assert hint(page).startswith("Point 2"), "the new shot is selected"


def test_a_double_click_finishes_a_line(page, live):
    tool(page, "add_line")
    page.mouse.click(*to_screen(page, 5.0, 5.0))
    page.mouse.click(*to_screen(page, 15.0, 7.0))
    page.mouse.dblclick(*to_screen(page, 25.0, 5.0))
    page.wait_for_timeout(600)
    lines = live.state.plan.lines
    assert len(lines) == 1 and len(lines[0].numbers) == 3


def test_escape_puts_the_tool_down_and_drops_the_line(page, live):
    tool(page, "add_line")
    page.mouse.click(*to_screen(page, 5.0, 5.0))
    page.mouse.click(*to_screen(page, 15.0, 7.0))
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    assert pressed(page) == "Navigate"
    assert live.state.plan.lines == []


def test_leaving_the_line_tool_keeps_what_was_drawn(page, live):
    """Switching tools commits the line in progress, as long as it is one."""
    tool(page, "add_line")
    page.mouse.click(*to_screen(page, 5.0, 5.0))
    page.mouse.click(*to_screen(page, 15.0, 7.0))
    tool(page, "navigate")
    page.wait_for_timeout(500)
    assert [len(ln.numbers) for ln in live.state.plan.lines] == [2]


def test_the_laser_tool_places_one_setup_and_goes_back_to_navigate(page, live):
    tool(page, "add_setup")
    page.mouse.click(*to_screen(page, 30.0, 25.0))
    page.wait_for_timeout(400)
    assert [s.name for s in live.state.plan.setups] == ["A"]
    assert pressed(page) == "Navigate"


# --- markers ----------------------------------------------------------------------------------

def test_dragging_a_marker_moves_its_planned_position(page, live):
    seed(live, page, (E0, N0))
    start = marker(page, 1)
    end = (start[0] + 60, start[1] - 30)
    drag(page, start, end)
    x, y = to_local(page, *end)
    point = live.state.plan.by_number(1)
    assert live.state.site.to_local(point.planned_e, point.planned_n) == \
        pytest.approx((x, y), abs=0.1)
    assert hint(page).startswith("Point 1"), "a moved marker is selected"


def test_a_measured_marker_cannot_be_dragged(page, live):
    """Dragging would edit the planned position, which is not what is drawn."""
    seed(live, page, (E0, N0), measured=(1,))
    start = marker(page, 1)
    drag(page, start, (start[0] + 60, start[1] - 30))
    point = live.state.plan.by_number(1)
    assert (point.planned_e, point.planned_n) == pytest.approx((E0, N0))


def test_markers_only_drag_in_navigate_mode(page, live):
    """Dragging markers while placing new ones is maddening."""
    seed(live, page, (E0, N0))
    tool(page, "add_point")
    start = marker(page, 1)
    drag(page, start, (start[0] + 60, start[1] - 30))
    point = live.state.plan.by_number(1)
    assert (point.planned_e, point.planned_n) == pytest.approx((E0, N0))


def test_clicking_a_marker_selects_its_row(page, live):
    seed(live, page, (E0, N0), (E0 + 6, N0))
    page.mouse.click(*marker(page, 2))
    page.wait_for_timeout(300)
    assert hint(page).startswith("Point 2")
    selected = page.locator("#plan-table .tabulator-row.tabulator-selected")
    assert selected.count() == 1
    assert selected.locator(".tabulator-cell[tabulator-field=number]").inner_text() == "2"


def test_clicking_a_locked_marker_still_selects_it(page, live):
    """It cannot be dragged; it must still be pickable."""
    seed(live, page, (E0, N0), (E0 + 6, N0), measured=(2,))
    page.mouse.click(*marker(page, 2))
    page.wait_for_timeout(300)
    assert hint(page).startswith("Point 2")


def test_markers_are_not_pickable_while_a_tool_is_placing(page, live):
    """A click on an existing marker has to reach the map, or you could not
    put a new shot beside one you already have."""
    seed(live, page, (E0, N0))
    tool(page, "add_point")
    page.mouse.click(*marker(page, 1))
    page.wait_for_timeout(400)
    assert len(live.state.plan.points) == 2
    assert hint(page).startswith("Point 2")


# --- the readings table -------------------------------------------------------------------------

def test_a_click_on_a_selected_row_edits_the_cell(page, live):
    """The first click selects; a click on a selected row edits. Without the
    second, cells look permanently read-only."""
    seed(live, page, (E0, N0))
    cell = row_cell(page, 1, "rod")
    cell.click()
    assert page.locator("#plan-table .tabulator-cell.tabulator-editing").count() == 0
    cell.click()
    assert page.locator("#plan-table .tabulator-cell.tabulator-editing").count() == 1
    page.keyboard.type("45.5")
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)
    assert live.state.plan.by_number(1).rod_in == pytest.approx(45.5)
    assert "1 of 1 shot" in page.locator("#panel-plan .coverage").inner_text()


def test_a_refused_edit_says_why_and_puts_the_cell_back(page, live):
    seed(live, page, (E0, N0))
    cell = row_cell(page, 1, "rod")
    cell.click()
    cell.click()
    page.keyboard.type("forty")
    page.keyboard.press("Enter")
    dialog = page.locator("dialog[open]")
    dialog.wait_for()
    assert "Rod reading" in dialog.inner_text()
    dialog.locator("button").last.click()
    page.wait_for_timeout(300)
    assert cell.inner_text().strip() == ""
    assert live.state.plan.by_number(1).rod_in is None


def test_several_rows_can_be_selected_and_deleted_once_confirmed(page, live):
    seed(live, page, (E0, N0), (E0 + 4, N0), (E0 + 8, N0))
    row_cell(page, 1, "number").click()
    row_cell(page, 3, "number").click(modifiers=["Shift"])
    assert "3 points selected" in hint(page)
    assert page.locator("#panel-plan button:has-text('Delete 3')").count() == 1

    page.locator("#plan-table").press("Delete")
    dialog = page.locator("dialog[open]")
    dialog.wait_for()
    dialog.locator("button:has-text('No')").click()
    page.wait_for_timeout(300)
    assert len(live.state.plan.points) == 3, "refusing keeps them"

    page.locator("#plan-table").press("Delete")
    page.locator("dialog[open] button:has-text('Yes')").click()
    page.wait_for_timeout(500)
    assert live.state.plan.points == []


def test_deleting_one_row_is_not_confirmed(page, live):
    seed(live, page, (E0, N0), (E0 + 4, N0))
    row_cell(page, 1, "number").click()
    page.click("#panel-plan button:has-text('Delete')")
    page.wait_for_timeout(400)
    assert page.locator("dialog[open]").count() == 0
    assert [p.number for p in live.state.plan.points] == [2]


def test_the_coordinate_editor_only_shows_for_rtk(page, live):
    seed(live, page, (E0, N0))
    row_cell(page, 1, "number").click()
    assert not page.locator("#panel-plan .group input[aria-label='first coordinate']").is_visible()

    cell = row_cell(page, 1, "method")
    cell.click()
    page.locator(".tabulator-edit-list-item:text-is('rtk')").click()
    page.wait_for_timeout(500)
    assert page.locator("#panel-plan .group input[aria-label='first coordinate']").is_visible()


def test_a_typed_position_moves_the_marker_and_locks_it(page, live):
    seed(live, page, (E0, N0))
    with live.server.acting():
        live.state.plan.by_number(1).method = "rtk"
        live.state.plan_changed()
    page.evaluate("() => window.yardsurvey.refresh()")
    row_cell(page, 1, "number").click()
    page.select_option("#panel-plan select[aria-label='coordinate frame']", "UTM m")
    page.fill("#panel-plan input[aria-label='first coordinate']", str(E0 + 1.25))
    page.fill("#panel-plan input[aria-label='second coordinate']", str(N0))
    page.click("#panel-plan button:has-text('Set measured position')")
    page.wait_for_timeout(500)
    assert "Measured position set" in page.locator("#panel-plan .detail-result").inner_text()
    drawn = page.evaluate("() => window.yardsurvey.store.state.plan.points[0]")
    assert drawn["locked"] and drawn["x"] == pytest.approx(13.25)


def test_the_number_column_stays_in_view(page, live):
    """The number links a row to its marker and to the printed sheet, so it
    must not scroll away when a wide cell is edited."""
    seed(live, page, (E0, N0))
    assert "tabulator-frozen" in row_cell(page, 1, "number").get_attribute("class")


# --- other views --------------------------------------------------------------------------------

def test_the_3d_view_draws_the_surface(page):
    page.click("#tabs button[data-tab='3d']")
    page.wait_for_selector("#plot3d .main-svg", timeout=20000)
    traces = page.evaluate("() => document.getElementById('plot3d').data.map(t => t.type)")
    assert traces == ["surface"]
    ratio = page.evaluate(
        "() => document.getElementById('plot3d').layout.scene.aspectratio.z")
    page.fill("#ve", "16")
    page.locator("#ve").dispatch_event("input")
    page.wait_for_timeout(500)
    doubled = page.evaluate(
        "() => document.getElementById('plot3d').layout.scene.aspectratio.z")
    assert doubled == pytest.approx(2 * ratio), "exaggeration is a view setting"


def test_the_busy_overlay_is_gone_after_a_failed_action(page, live):
    seed(live, page, (E0, N0))
    cell = row_cell(page, 1, "rod")
    cell.click()
    cell.click()
    page.keyboard.type("x")
    page.keyboard.press("Enter")
    page.locator("dialog[open]").wait_for()
    assert page.locator("#busy").is_hidden()


# --- comparing sessions ----------------------------------------------------------------

@pytest.fixture
def two_sessions(live, page, synthetic_outing2):
    """The live server with a second outing merged in, put back afterwards."""
    with live.server.acting():
        live.state.add_export(synthetic_outing2)
    page.evaluate("() => window.yardsurvey.refresh()")
    page.wait_for_timeout(500)
    yield live.state.sessions
    with live.server.acting():
        live.state.load(live.export)


def session_row(page, name):
    return page.locator("#panel-sessions .session", has_text=name)


def test_every_session_is_listed_with_its_repeatability(page, two_sessions):
    rows = page.locator("#panel-sessions .session")
    assert rows.count() == 2
    for name in two_sessions:
        assert "repeatability" in session_row(page, name).inner_text()
    assert "Between sessions" in page.locator("#panel-sessions").inner_text()


def test_hiding_a_session_takes_its_points_off_the_map(page, live, two_sessions):
    first = two_sessions[0]
    session_row(page, first).locator("input[type=checkbox]").uncheck()
    page.wait_for_timeout(600)
    assert live.state.hiding == {first}
    assert "1 session hidden" in page.locator("#map-info").inner_text()

    page.click("#panel-sessions button:has-text('Show all')")
    page.wait_for_timeout(600)
    assert live.state.hiding == set()


def test_only_shows_one_session(page, live, two_sessions):
    second = two_sessions[1]
    session_row(page, second).locator("button:has-text('only')").click()
    page.wait_for_timeout(600)
    assert live.state.hiding == {two_sessions[0]}


def test_the_surface_can_follow_the_shown_sessions(page, live, two_sessions):
    session_row(page, two_sessions[0]).locator("input[type=checkbox]").uncheck()
    page.check("#panel-sessions label:has-text('shown sessions only') input")
    page.wait_for_timeout(1000)
    assert live.state.surface_from_shown
    assert "from 1 of 2 sessions" in page.locator("#qc").inner_text()


def test_colour_by_session_matches_the_swatches(page, two_sessions):
    page.click("#panel-sessions button:has-text('Colour by session')")
    assert page.locator("#color-by").input_value() == "session"


# --- slope, contours and drainage ---------------------------------------------------------

def test_slope_shading_brings_its_own_scale(page):
    with page.expect_request(lambda r: "/api/surface.png" in r.url and "mode=slope" in r.url):
        page.select_option("#surface-mode", "slope")
    legend = page.locator(".map-legend")
    assert "Slope (%)" in legend.inner_text()
    assert page.locator("#slope-max-label").is_visible()
    with page.expect_request(lambda r: "slope_max=6" in r.url):
        page.fill("#slope-max", "6")
        page.locator("#slope-max").dispatch_event("change")
    assert "≥ 6" in legend.inner_text()


def test_elevation_shading_says_what_the_colours_mean(page):
    legend = page.locator(".map-legend")
    assert "Elevation (ft, raw ellipsoidal)" in legend.inner_text()
    assert not page.locator("#slope-max-label").is_visible()


def test_contours_are_drawn_at_the_chosen_interval(page):
    with page.expect_response(lambda r: "/api/contours?interval_cm=5" in r.url):
        page.check("#show-contours")
    assert "contours every 5 cm" in page.locator(".map-legend").inner_text()
    with page.expect_response(lambda r: "/api/contours?interval_cm=25" in r.url):
        page.select_option("#contour-interval", "25")


def test_drainage_arrows_are_drawn(page):
    with page.expect_response(lambda r: "/api/drainage" in r.url) as info:
        page.check("#show-drainage")
    assert len(info.value.json()["arrows"]) > 100
    assert "points downhill" in page.locator(".map-legend").inner_text()


def test_the_printed_maps_follow_the_view_settings(page):
    page.select_option("#contour-interval", "10")
    page.click("#menubar .menu-root > button:has-text('Export')")
    with page.expect_download() as info:
        page.locator("#menubar .menu:visible button.item:has-text('Contour map')").click()
    assert info.value.suggested_filename == "contours_10cm.png"

    page.click("#menubar .menu-root > button:has-text('Export')")
    with page.expect_download() as info:
        page.locator("#menubar .menu:visible button.item:has-text('Slope map')").click()
    assert info.value.suggested_filename == "slope_map.png"


def test_control_marks_are_added_from_the_datum_menu(page, live):
    """Datum ▸ Control marks… adds a mark, which the site then carries."""
    page.click("#menubar .menu-root > button:has-text('Datum')")
    page.locator("#menubar .menu:visible button.item:has-text('Control marks')").click()
    dialog = page.locator("dialog[open]")
    dialog.wait_for()
    assert "fixed-height pole" in dialog.inner_text()
    dialog.locator("input[aria-label='Mark name']").first.fill("bm1")
    dialog.locator("input[aria-label='Note']").first.fill("mag nail")
    dialog.locator("button:has-text('OK')").click()
    page.wait_for_timeout(500)
    try:
        assert live.state.site.mark_names == ["BM1"]
        assert live.state.site.control[0].note == "mag nail"
        # With marks set, every session says whether it was checked.
        assert "no check shots" in page.locator("#panel-sessions").inner_text()
    finally:
        with live.server.acting():
            live.state.site.control = []


def test_tie_transects_are_drawn_but_not_listed(page, live):
    """Plan ▸ Add tie transects: lines on the map, no rows in the table."""
    page.click("#menubar .menu-root > button:has-text('Plan')")
    page.locator("#menubar .menu:visible button.item:has-text('Add tie transects')").click()
    dialog = page.locator("dialog[open]")
    dialog.wait_for()
    assert "Walk or mow these first" in dialog.inner_text()
    dialog.locator("button").last.click()
    page.wait_for_timeout(400)
    assert len(live.state.plan.lines) == 4
    assert page.locator("#plan-table .tabulator-row").count() == 0
    kinds = page.evaluate("() => window.yardsurvey.store.state.plan.lines.map((l) => l.kind)")
    assert kinds == ["transect"] * 4


def test_spot_labels_appear_when_zoomed_in(page, live):
    """The red squares say which station they are once they can be read."""
    labels = page.evaluate("""() => {
        const map = window.yardsurvey.map;
        const layer = map.getLayers().getArray().find((l) => l.getZIndex() === 130);
        const f = layer.getSource().getFeatures()[0];
        const style = layer.getStyle();
        return [style(f, 1.0).getText(), style(f, 0.1).getText()?.getText(),
                style(f, 0.02).getText()?.getText()];
    }""")
    assert labels[0] is None
    assert labels[1].isdigit()
    assert "lawn" in labels[2] and " in" in labels[2] and "2026-08-27" in labels[2]
