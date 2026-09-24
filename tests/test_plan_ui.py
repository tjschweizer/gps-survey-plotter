"""The drawing overlay and the readings table."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QItemSelectionModel               # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox      # noqa: E402

from gpsrtk.model import pointset as P                       # noqa: E402
from gpsrtk.plan import Tie                                  # noqa: E402
from gpsrtk.units import ft_to_m, m_to_ft                    # noqa: E402

E0, N0 = 449712.0, 4604565.0
COL_ROD, COL_FIX, COL_NOTE = 4, 5, 7


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, export, monkeypatch, tmp_path):
    from gpsrtk.ui.main import MainWindow

    raised = []
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(
            QMessageBox, name,
            staticmethod(lambda *a, **k: (raised.append(a), QMessageBox.Ok)[1]))

    w = MainWindow()
    w.state.cache_dir = tmp_path / "cache"
    w.state.load(str(export.source_path))
    w.dialogs = raised
    yield w
    w.view3d.close_plotter()


def _seed(win):
    plan = win.state.plan
    plan.add_point(E0, N0)                                   # 1
    plan.add_point(E0 + 4, N0 + 2)                           # 2
    plan.add_line("swale-1", [(E0 - 4, N0 - 4), (E0, N0 - 3),
                              (E0 + 4, N0 - 2)])             # 3, 4, 5
    win._on_plan_changed()
    return plan


# --- the map overlay -----------------------------------------------------

def test_markers_and_curves_follow_the_plan(win):
    plan = _seed(win)
    assert len(win.plan_overlay._targets) == len(plan.points) == 5
    assert set(win.plan_overlay._curves) == {"swale-1"}


def test_dragging_a_marker_moves_the_planned_position(win):
    _seed(win)
    target = win.plan_overlay._targets[1]
    target.setPos(target.pos().x() + 2.0, target.pos().y() - 1.0)
    win.plan_overlay._on_moved(1, target)

    point = win.state.plan.by_number(1)
    assert point.planned_e == pytest.approx(E0 + 2.0)
    assert point.planned_n == pytest.approx(N0 - 1.0)


def test_markers_only_drag_in_navigate_mode(win):
    """Dragging markers while placing new ones is maddening."""
    _seed(win)
    win._set_plan_mode("add_point")
    assert not any(t.movable for t in win.plan_overlay._targets.values())
    win._set_plan_mode("navigate")
    assert all(t.movable for t in win.plan_overlay._targets.values())


def test_selection_does_not_recurse(win):
    """Table selection highlights the map, and the map must not push it back.
    A two-way binding here recurses until the stack blows."""
    _seed(win)
    win.plan_panel.table.selectRow(0)
    assert win.plan_overlay.selection == win.plan_panel.current_number()
    win.plan_overlay.select(win.plan_overlay.selection)      # must be a no-op
    win.plan_panel.table.selectRow(2)
    assert win.plan_overlay.selection == win.plan_panel.current_number()


def test_a_stray_click_is_not_a_line(win):
    """One vertex is a misclick, not a break line."""
    overlay = win.plan_overlay
    win._set_plan_mode("add_line")
    overlay._pending = [(0.0, 0.0)]
    overlay.finish_line()
    assert win.state.plan.lines == []


def test_removing_a_point_removes_its_marker(win):
    _seed(win)
    win.plan_panel.table.selectRow(0)
    win.plan_panel._delete_selected()
    assert 1 not in win.plan_overlay._targets
    assert len(win.plan_overlay._targets) == 4


# --- the readings table --------------------------------------------------

def test_typing_a_rod_reading_records_it(win):
    _seed(win)
    win.plan_panel.table.item(0, COL_ROD).setText("45.5")
    assert win.state.plan.by_number(1).rod_in == pytest.approx(45.5)
    assert "1 of 5 shot" in win.plan_panel.coverage.text()


def test_a_non_numeric_rod_reading_is_rejected(win):
    _seed(win)
    win.plan_panel.table.item(0, COL_ROD).setText("forty five")
    assert win.state.plan.by_number(1).rod_in is None
    assert win.dialogs, "the user must be told, not silently ignored"


def test_clearing_a_rod_reading_marks_it_outstanding(win):
    _seed(win)
    win.plan_panel.table.item(0, COL_ROD).setText("45.5")
    win.plan_panel.table.item(0, COL_ROD).setText("")
    assert win.state.plan.by_number(1).rod_in is None
    assert win.state.plan.coverage()["observed"] == 0


def test_ticking_gnss_promotes_the_position_method(win):
    """Only a FIXED solution beats the planned click; anything else clears."""
    _seed(win)
    win.plan_panel.table.item(0, COL_FIX).setText("yes")
    point = win.state.plan.by_number(1)
    assert point.fix == 4 and point.method == "rtk"

    win.plan_panel.table.item(0, COL_FIX).setText("")
    point = win.state.plan.by_number(1)
    assert point.fix is None and point.method == "planned"


def test_inserting_a_vertex_puts_it_in_the_run_not_at_the_end(win):
    """Numbers are assigned in order of creation; the LINE is ordered by shape.
    Those are different orderings and both have to be respected."""
    plan = _seed(win)
    before = list(plan.line("swale-1").numbers)
    win.plan_panel.table.selectRow(2)                        # first vertex
    win.plan_panel._insert_vertex()

    after = plan.line("swale-1").numbers
    assert len(after) == len(before) + 1
    assert after[0] == before[0] and after[2:] == before[1:]
    assert after[1] == max(p.number for p in plan.points)


def test_inserting_on_a_loose_shot_explains_itself(win):
    _seed(win)
    win.plan_panel.table.selectRow(0)                        # not on a line
    win.plan_panel._insert_vertex()
    assert win.dialogs


# --- position methods through the panel ----------------------------------

def test_taped_position_solves_from_the_panel(win):
    import numpy as np

    plan = _seed(win)
    for num, (e, n) in ((1, (E0, N0)), (2, (E0 + 4, N0 + 2))):
        p = plan.by_number(num)
        p.observed_e, p.observed_n, p.fix, p.method = e, n, 4, "rtk"

    truth = np.array([E0 + 1.0, N0 + 3.0])
    target = plan.by_number(3)
    target.method = "taped"
    # The planned click is what picks between the two mirror solutions, so it
    # has to be on the correct side of the line joining the two references.
    # Seeding it on the far side would - correctly - converge on the mirror.
    target.planned_e, target.planned_n = truth[0] - 0.8, truth[1] + 0.6
    win.plan_panel.refresh()
    win.plan_panel.table.selectRow(2)

    win.plan_panel.tie_a.setText("1")
    win.plan_panel.dist_a.setValue(m_to_ft(float(np.hypot(*(truth - [E0, N0])))))
    win.plan_panel.tie_b.setText("2")
    win.plan_panel.dist_b.setValue(
        m_to_ft(float(np.hypot(*(truth - [E0 + 4, N0 + 2])))))
    win.plan_panel._apply_tape()

    e, n, method, _ = plan.resolve(3)
    assert method == "taped"
    assert e == pytest.approx(truth[0], abs=0.01)
    assert n == pytest.approx(truth[1], abs=0.01)
    assert "exactly determined" in win.plan_panel.detail_result.text()


def test_station_offset_applies_from_the_panel(win):
    plan = _seed(win)
    point = plan.by_number(1)
    point.method = "station_offset"
    win.plan_panel.refresh()
    win.plan_panel.table.selectRow(0)
    win.plan_panel.ref_line.setCurrentText("swale-1")
    win.plan_panel.station.setValue(10.0)
    win.plan_panel.offset.setValue(0.0)
    win.plan_panel._apply_offset()

    _, _, method, _ = plan.resolve(1)
    assert method == "station_offset"
    assert plan.by_number(1).station_m == pytest.approx(ft_to_m(10.0))


# --- feeding the pipeline ------------------------------------------------

def test_plan_observations_become_a_layer_and_reach_the_level_network(win):
    plan = _seed(win)
    for num in (1, 2):
        win.plan_panel.table.item(num - 1, COL_ROD).setText("40.0")

    assert win.state.PLAN_LAYER in win.state.layers
    assert len(win.state.layers[win.state.PLAN_LAYER]) == 2

    # spots must combine the imported spot layer with the plan's rod shots,
    # because they are the same measurement against the same laser plane.
    spots = win.state.spots
    assert spots is not None
    assert P.ROD_IN in spots.df.columns
    assert len(spots) > 2


def test_a_plan_with_no_readings_contributes_no_layer(win):
    _seed(win)
    assert win.state.PLAN_LAYER not in win.state.layers


def test_plan_round_trips_through_its_file(win, tmp_path):
    from gpsrtk.plan import Plan

    plan = _seed(win)
    win.plan_panel.table.item(0, COL_ROD).setText("41.25")
    plan.epsg = win.state.site.epsg
    saved = plan.save(tmp_path / "p.yardplan")

    back = Plan.load(saved)
    assert [p.number for p in back.points] == [p.number for p in plan.points]
    assert back.by_number(1).rod_in == pytest.approx(41.25)
    assert back.line("swale-1").numbers == plan.line("swale-1").numbers


def test_letter_setup_labels_reach_the_level_network(win):
    """SW Maps numbers its setups; a shot plan labels them 'A', 'B'. Forcing a
    float conversion broke the plan -> level network handoff entirely."""
    from gpsrtk import vertical as V

    plan = _seed(win)
    for p in plan.points:
        p.setup = "A"
    for row in range(3):
        win.plan_panel.table.item(row, COL_ROD).setText("42.0")

    setups = V.resolve_setups(win.state.layers[win.state.PLAN_LAYER])
    assert set(setups) == {"A"}

    # And the whole solve must go through without raising.
    win.state.solve_vertical("local")
    assert win.state.vertical is not None


def test_numeric_setups_still_normalise(spots):
    """0 and 0.0 must remain the same setup, not two."""
    from gpsrtk import filters as F, vertical as V

    lawn = F.KindSelect(names=["lawn"]).apply(spots)
    assert set(V.resolve_setups(lawn)) == {"0"}


def test_plan_layer_without_gnss_height_describes_itself_honestly(win):
    _seed(win)
    win.plan_panel.table.item(0, COL_ROD).setText("45.5")
    described = win.state.layers[win.state.PLAN_LAYER].describe()
    assert "nan" not in described
    assert "no height yet" in described


def test_plan_shots_do_not_appear_as_unresolved_sessions(win):
    """Laser rod shots carry no GNSS height, so they cannot take part in a
    crossover solve. Including them had them reported as 'unresolved' for want
    of an overlap they could never have."""
    plan = _seed(win)
    for p in plan.points:
        p.setup = "A"
    for row in range(3):
        win.plan_panel.table.item(row, COL_ROD).setText("42.0")

    win.state.solve_vertical("local")
    model = win.state.vertical
    assert model.sessions is not None
    assert "plan" not in model.sessions.offsets
    assert "plan" not in model.sessions.unresolved
    assert not any("plan" in note for note in model.notes)


# --- purposes, editability and tools -------------------------------------

COL_PURPOSE, COL_LINE, COL_SETUP, COL_METHOD = 1, 2, 3, 6


def test_purpose_can_be_changed_from_the_table(win):
    """The old table hard-coded Purpose as read-only, so a building corner
    could never be recorded as anything but a generic spot."""
    _seed(win)
    win.plan_panel.table.item(0, COL_PURPOSE).setText("building corner")
    assert win.state.plan.by_number(1).purpose == "building corner"


def test_an_unknown_purpose_is_refused(win):
    _seed(win)
    win.plan_panel.table.item(0, COL_PURPOSE).setText("nonsense")
    assert win.state.plan.by_number(1).purpose == "spot"
    assert win.dialogs


def test_purposes_cover_the_features_that_matter():
    from gpsrtk.plan import PURPOSES

    for wanted in ("building corner", "plat reference", "monument",
                   "driveway edge", "sidewalk edge", "swale bottom",
                   "curb flowline", "benchmark"):
        assert wanted in PURPOSES, wanted


def test_only_terrain_purposes_reach_the_ground_surface(win):
    """A building corner is an accurate point that would be a lie if the
    interpolator treated it as lawn."""
    from gpsrtk.plan import is_terrain

    plan = _seed(win)
    plan.by_number(1).purpose = "building corner"
    plan.by_number(1).rod_in = 40.0
    plan.by_number(2).purpose = "swale bottom"
    plan.by_number(2).rod_in = 41.0

    frame = plan.to_frame().set_index("point_id")
    assert not bool(frame.loc[1, "terrain"])
    assert bool(frame.loc[2, "terrain"])
    assert frame.loc[1, "kind"] == "building corner"
    assert frame.loc[2, "kind"] == "lawn"
    assert not is_terrain("plat reference")


def test_a_point_can_be_moved_onto_and_off_a_line(win):
    plan = _seed(win)
    win.plan_panel.table.item(0, COL_LINE).setText("swale-1")
    assert 1 in plan.line("swale-1").numbers

    win.plan_panel.table.item(0, COL_LINE).setText("")
    assert 1 not in plan.line("swale-1").numbers


def test_position_method_can_be_changed_from_the_table(win):
    _seed(win)
    win.plan_panel.table.item(0, COL_METHOD).setText("taped")
    assert win.state.plan.by_number(1).method == "taped"


def test_cells_are_editable_by_single_click_on_a_selected_row(win):
    """SelectRows means the first click only selects. Without SelectedClicked
    the cells look permanently read-only."""
    from PySide6.QtWidgets import QAbstractItemView

    triggers = win.plan_panel.table.editTriggers()
    assert triggers & QAbstractItemView.SelectedClicked
    assert triggers & QAbstractItemView.DoubleClicked


def test_tools_are_exclusive_and_navigate_is_the_default(win):
    panel = win.plan_panel
    assert panel.tool_group.exclusive()
    assert panel.btn_navigate.isChecked()

    win._set_plan_mode("add_point")
    assert panel.btn_add_point.isChecked()
    assert not panel.btn_navigate.isChecked()


def test_clicking_the_active_tool_puts_it_down(win):
    """There was no way to leave a placing mode except to hunt for Navigate."""
    win._set_plan_mode("add_point")
    win.plan_panel._tool_clicked("add_point")
    assert win.plan_overlay.mode == "navigate"
    assert win.plan_panel.btn_navigate.isChecked()


def test_escape_puts_the_tool_down(win):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent

    win._set_plan_mode("add_line")
    win.plan_overlay._pending = [(0.0, 0.0), (1.0, 1.0)]
    win.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape,
                                Qt.NoModifier))
    assert win.plan_overlay.mode == "navigate"
    assert win.plan_overlay._pending == []


# --- the field sheet's locating columns ----------------------------------

def test_field_sheet_has_somewhere_to_write_the_position(win, tmp_path):
    """Without these columns the locating information has nowhere to go but
    the notes, which then has to be re-read and guessed at."""
    from gpsrtk.io.fieldsheet import write_field_sheet

    _seed(win)
    out = write_field_sheet(win.state.plan, tmp_path / "s.html",
                            site=win.state.site)
    doc = out.read_text(encoding="utf-8")

    assert "Tie A" in doc and "Tie B" in doc
    assert "GNSS pt" in doc
    assert "Rod (in)" in doc
    # Each row needs its own boxes, not one shared note field.
    assert doc.count('class="blank tie"') == 2 * len(win.state.plan.points)
    # And it has to print landscape, or the columns do not fit.
    assert "size: landscape" in doc


def test_field_sheet_shows_the_purpose_of_every_shot(win, tmp_path):
    from gpsrtk.io.fieldsheet import write_field_sheet

    plan = _seed(win)
    plan.by_number(1).purpose = "plat reference"
    out = write_field_sheet(plan, tmp_path / "s.html", site=win.state.site)
    assert "plat reference" in out.read_text(encoding="utf-8")


# --- entering a measured coordinate --------------------------------------

def test_measured_position_can_be_typed_in_utm(win):
    """The field sheet has a column for the receiver's coordinates, so there
    has to be somewhere to type them back in."""
    plan = _seed(win)
    plan.by_number(1).method = "rtk"
    win.plan_panel.refresh()
    win.plan_panel.table.selectRow(0)

    win.plan_panel.coord_frame.setCurrentText("UTM m")
    win.plan_panel.coord_x.setText("449713.250")
    win.plan_panel.coord_y.setText("4604566.500")
    win.plan_panel._apply_coords()

    point = plan.by_number(1)
    assert point.observed_e == pytest.approx(449713.250)
    assert point.observed_n == pytest.approx(4604566.500)
    assert point.method == "rtk" and point.fix == 4
    e, n, method, _ = plan.resolve(1)
    assert method == "rtk" and e == pytest.approx(449713.250)


def test_measured_position_accepts_local_feet(win):
    plan = _seed(win)
    plan.by_number(1).method = "rtk"
    win.plan_panel.refresh(); win.plan_panel.table.selectRow(0)

    win.plan_panel.coord_frame.setCurrentText("local ft")
    win.plan_panel.coord_x.setText("45.00")
    win.plan_panel.coord_y.setText("55.00")
    win.plan_panel._apply_coords()

    le, ln = win.state.site.to_local(plan.by_number(1).observed_e,
                                     plan.by_number(1).observed_n)
    assert m_to_ft(le) == pytest.approx(45.0, abs=0.01)
    assert m_to_ft(ln) == pytest.approx(55.0, abs=0.01)


def test_measured_position_accepts_lat_lon(win):
    """A receiver reads out degrees, not UTM."""
    plan = _seed(win)
    plan.by_number(1).method = "rtk"
    win.plan_panel.refresh(); win.plan_panel.table.selectRow(0)

    win.plan_panel.coord_frame.setCurrentText("lat/lon")
    win.plan_panel.coord_x.setText("-93.603278000")
    win.plan_panel.coord_y.setText("41.591087000")
    win.plan_panel._apply_coords()

    # The same coordinate, projected into the site's CRS.
    point = plan.by_number(1)
    assert point.observed_e == pytest.approx(449719.251, abs=0.05)
    assert point.observed_n == pytest.approx(4604552.424, abs=0.05)


def test_coordinates_round_trip_through_every_frame(win):
    plan = _seed(win)
    point = plan.by_number(1)
    point.method = "rtk"
    point.observed_e, point.observed_n = 449719.251, 4604552.424
    win.plan_panel.refresh(); win.plan_panel.table.selectRow(0)

    for frame in ("UTM m", "local ft", "lat/lon"):
        win.plan_panel.coord_frame.setCurrentText(frame)
        win.plan_panel._fill_coords(point)
        assert win.plan_panel.coord_x.text()
        win.plan_panel._apply_coords()
        assert plan.by_number(1).observed_e == pytest.approx(449719.251, abs=0.05)
        assert plan.by_number(1).observed_n == pytest.approx(4604552.424, abs=0.05)


def test_swapped_lat_lon_is_caught(win):
    """Latitude in the longitude box is the classic slip, and it would place
    the point on the other side of the planet."""
    plan = _seed(win)
    plan.by_number(1).method = "rtk"
    win.plan_panel.refresh(); win.plan_panel.table.selectRow(0)

    win.plan_panel.coord_frame.setCurrentText("lat/lon")
    win.plan_panel.coord_x.setText("41.591087")      # latitude, wrong box
    win.plan_panel.coord_y.setText("-93.603278")
    win.plan_panel._apply_coords()

    assert win.dialogs
    assert plan.by_number(1).observed_e is None


def test_non_numeric_coordinates_are_refused(win):
    plan = _seed(win)
    plan.by_number(1).method = "rtk"
    win.plan_panel.refresh(); win.plan_panel.table.selectRow(0)
    win.plan_panel.coord_x.setText("north-ish")
    win.plan_panel.coord_y.setText("")
    win.plan_panel._apply_coords()
    assert win.dialogs
    assert plan.by_number(1).observed_e is None


def test_clearing_a_measured_position_falls_back_to_the_plan(win):
    plan = _seed(win)
    point = plan.by_number(1)
    point.method, point.fix = "rtk", 4
    point.observed_e, point.observed_n = 449719.251, 4604552.424
    win.plan_panel.refresh(); win.plan_panel.table.selectRow(0)

    win.plan_panel._clear_coords()
    point = plan.by_number(1)
    assert point.observed_e is None and point.fix is None
    assert point.method == "planned"
    _, _, method, _ = plan.resolve(1)
    assert method == "planned"


def test_the_coordinate_editor_only_shows_for_rtk(win):
    plan = _seed(win)
    win.plan_panel.table.selectRow(0)
    assert not win.plan_panel.rtk_row.isVisibleTo(win.plan_panel)

    plan.by_number(1).method = "rtk"
    win.plan_panel._show_detail(1)
    assert win.plan_panel.rtk_row.isVisibleTo(win.plan_panel)


# --- table column sizing -------------------------------------------------

def test_columns_have_workable_widths_and_notes_absorbs_the_slack(win):
    """resizeColumnsToContents left every column as narrow as its emptiest
    cell and dumped the leftover width to the right of the table."""
    from PySide6.QtWidgets import QHeaderView

    _seed(win)
    table = win.plan_panel.table
    for col in range(table.columnCount() - 1):
        assert table.columnWidth(col) >= 34, f"column {col} is too narrow"
    assert (table.horizontalHeader().sectionResizeMode(table.columnCount() - 1)
            == QHeaderView.Stretch)


# --- a measured point stops being a proposal ------------------------------

def _select_rows(win, *rows):
    """Add whole rows to the selection.

    Selecting a single cell is not enough: with SelectRows behaviour, only a
    fully selected row appears in `selectedRows()`.
    """
    table = win.plan_panel.table
    model = table.selectionModel()
    model.clearSelection()
    for row in rows:
        model.select(table.model().index(row, 0),
                     QItemSelectionModel.Select | QItemSelectionModel.Rows)
    return table


def _measure(win, number, de=1.5, dn=-0.8):
    """Give a point an RTK position, offset from where it was planned."""
    point = win.state.plan.by_number(number)
    point.observed_e = point.planned_e + de
    point.observed_n = point.planned_n + dn
    point.method, point.fix = "rtk", 4
    win._on_plan_changed()
    return point


def test_a_measured_marker_moves_to_the_measured_position(win):
    """The click is history once the shot exists."""
    _seed(win)
    before = win.plan_overlay._targets[1].pos()
    point = _measure(win, 1)

    after = win.plan_overlay._targets[1].pos()
    x, y = win.state.site.to_local(point.observed_e, point.observed_n)
    assert (after.x(), after.y()) == pytest.approx((x, y))
    assert (after.x(), after.y()) != pytest.approx((before.x(), before.y()))


def test_a_measured_marker_cannot_be_dragged(win):
    """Dragging would edit the planned position, which is not what is drawn -
    so the marker would appear to spring back."""
    _seed(win)
    _measure(win, 1)
    win._set_plan_mode("navigate")

    assert win.plan_overlay.is_locked(1)
    assert not win.plan_overlay._targets[1].movable
    assert win.plan_overlay._targets[2].movable, "unmeasured points still drag"


def test_a_locked_marker_ignores_a_programmatic_move(win):
    _seed(win)
    point = _measure(win, 1)
    planned = (point.planned_e, point.planned_n)

    target = win.plan_overlay._targets[1]
    target.setPos(target.pos().x() + 5.0, target.pos().y())
    win.plan_overlay._on_moved(1, target)

    assert (point.planned_e, point.planned_n) == pytest.approx(planned)


def test_a_taped_position_locks_the_marker_too(win):
    """Any derived position is a measurement, not a click."""
    plan = _seed(win)
    for number in (1, 2):
        _measure(win, number, de=0.0, dn=0.0)
    p = plan.by_number(3)
    p.ties = [Tie(1, 4.0), Tie(2, 5.0)]
    p.method = "taped"
    win._on_plan_changed()

    assert win.plan_overlay.is_locked(3)
    assert not win.plan_overlay._targets[3].movable


def test_a_leader_is_drawn_back_to_the_click(win):
    """The gap between plan and truth is worth seeing, not hiding."""
    _seed(win)
    assert len(win.plan_overlay._leaders.getData()[0]) == 0
    _measure(win, 1)
    xs, ys = win.plan_overlay._leaders.getData()
    assert len(xs) == 3, "one segment: planned, measured, NaN separator"


def test_no_leader_when_the_shot_landed_on_the_mark(win):
    """A leader shorter than the marker is a smudge, not information."""
    _seed(win)
    _measure(win, 1, de=0.02, dn=0.0)
    assert len(win.plan_overlay._leaders.getData()[0]) == 0


def test_clearing_the_measurement_unlocks_and_returns_the_marker(win):
    plan = _seed(win)
    _measure(win, 1)
    win.plan_panel.table.selectRow(0)
    win.plan_panel._clear_coords()
    win._on_plan_changed()

    point = plan.by_number(1)
    pos = win.plan_overlay._targets[1].pos()
    x, y = win.state.site.to_local(point.planned_e, point.planned_n)
    assert (pos.x(), pos.y()) == pytest.approx((x, y))
    assert win.plan_overlay._targets[1].movable
    assert len(win.plan_overlay._leaders.getData()[0]) == 0


# --- clicking the map picks the row ---------------------------------------

def test_clicking_a_marker_selects_its_row(win):
    _seed(win)
    win.plan_panel.table.selectRow(0)
    win.plan_overlay._on_target_clicked(4)

    assert win.plan_overlay.selection == 4
    assert win.plan_panel.current_number() == 4


def test_clicking_a_locked_marker_still_selects_it(win):
    """It cannot be dragged; it must still be pickable."""
    _seed(win)
    _measure(win, 2)
    win.plan_overlay._on_target_clicked(2)
    assert win.plan_panel.current_number() == 2


def test_markers_are_not_pickable_while_a_tool_is_placing(win):
    """A click on an existing marker has to reach the scene, or you could not
    put a new shot beside one you already have."""
    _seed(win)
    win._set_plan_mode("add_point")
    assert not any(t.selectable for t in win.plan_overlay._targets.values())

    win.plan_overlay._on_target_clicked(3)
    assert win.plan_overlay.selection != 3


# --- multi-select and delete ----------------------------------------------

def test_several_rows_can_be_selected_and_deleted(win, monkeypatch):
    plan = _seed(win)
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))
    _select_rows(win, 2, 3, 4)

    assert win.plan_panel.selected_numbers() == [3, 4, 5]
    win.plan_panel._delete_selected()
    assert [p.number for p in plan.points] == [1, 2]


def test_a_bulk_delete_is_confirmed_and_can_be_refused(win, monkeypatch):
    plan = _seed(win)
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.No))
    win.plan_panel.table.selectAll()
    win.plan_panel._delete_selected()
    assert len(plan.points) == 5


def test_deleting_one_row_is_not_confirmed(win, monkeypatch):
    """A single mistaken deletion is obvious and trivially redone."""
    plan = _seed(win)
    asked = []
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **k: (asked.append(a), QMessageBox.Yes)[1]))
    win.plan_panel.table.selectRow(0)
    win.plan_panel._delete_selected()

    assert asked == []
    assert len(plan.points) == 4


def test_the_detail_editor_goes_quiet_on_a_multi_selection(win):
    """With several rows there is no single point a typed coordinate belongs
    to, and applying it to whichever was first would be a silent wrong edit."""
    _seed(win)
    _select_rows(win, 0, 1)

    assert win.plan_panel.current_number() is None
    assert win.plan_overlay.selection is None
    assert "2 points selected" in win.plan_panel.detail_hint.text()
