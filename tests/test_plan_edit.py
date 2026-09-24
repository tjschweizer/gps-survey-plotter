"""Editing a shot plan: the rules behind the readings table and the map.

These used to be exercised by driving Qt widgets - typing into table cells,
calling a marker's drag handler. The rules now live in `gpsrtk.app.plan_edit`
and are tested here directly; what is genuinely about the browser (dragging,
clicking, the tool buttons) is in `test_web_browser.py`.
"""

import numpy as np
import pytest

from gpsrtk.app import plan_edit as PE
from gpsrtk.model import pointset as P
from gpsrtk.plan import Tie
from gpsrtk.units import ft_to_m, m_to_ft

E0, N0 = 449712.0, 4604565.0


def _seed(state):
    plan = state.plan
    plan.add_point(E0, N0)                                   # 1
    plan.add_point(E0 + 4, N0 + 2)                           # 2
    plan.add_line("swale-1", [(E0 - 4, N0 - 4), (E0, N0 - 3),
                              (E0 + 4, N0 - 2)])             # 3, 4, 5
    state.plan_changed()
    return plan


def _payload(state):
    return PE.plan_payload(state.plan, state.site)


def _drawn(state, number):
    return next(p for p in _payload(state)["points"] if p["number"] == number)


def _edit(state, number, field, text):
    PE.edit_cell(state.plan, number, field, text)
    state.plan_changed()


def _measure(state, number, de=1.5, dn=-0.8):
    """Give a point an RTK position, offset from where it was planned."""
    point = state.plan.by_number(number)
    point.observed_e = point.planned_e + de
    point.observed_n = point.planned_n + dn
    point.method, point.fix = "rtk", 4
    state.plan_changed()
    return point


# --- what the map draws --------------------------------------------------------

def test_markers_and_curves_follow_the_plan(state):
    plan = _seed(state)
    payload = _payload(state)
    assert len(payload["points"]) == len(plan.points) == 5
    assert [ln["line_id"] for ln in payload["lines"]] == ["swale-1"]
    assert len(payload["lines"][0]["xy"]) == 3


def test_positions_are_drawn_in_local_metres(state):
    _seed(state)
    x, y = state.site.to_local(E0, N0)
    assert (_drawn(state, 1)["x"], _drawn(state, 1)["y"]) == pytest.approx((x, y))


def test_dragging_a_marker_moves_the_planned_position(state):
    _seed(state)
    assert PE.move_point(state.plan, 1, E0 + 2.0, N0 - 1.0)
    point = state.plan.by_number(1)
    assert point.planned_e == pytest.approx(E0 + 2.0)
    assert point.planned_n == pytest.approx(N0 - 1.0)


def test_a_stray_click_is_not_a_line(state):
    """One vertex is a misclick, not a break line."""
    assert PE.add_line(state.plan, [(E0, N0)]) is None
    assert state.plan.lines == []


def test_drawn_lines_are_numbered_and_named_in_order(state):
    first = PE.add_line(state.plan, [(E0, N0), (E0 + 3, N0)])
    second = PE.add_line(state.plan, [(E0, N0 + 3), (E0 + 3, N0 + 3)])
    assert (first.line_id, second.line_id) == ("line-1", "line-2")
    assert first.numbers == [1, 2] and second.numbers == [3, 4]
    assert all(state.plan.by_number(n).purpose == "breakline" for n in (1, 2, 3, 4))


def test_laser_setups_are_lettered_in_order(state):
    a = PE.add_setup(state.plan, E0, N0)
    b = PE.add_setup(state.plan, E0 + 10, N0)
    assert (a.name, b.name) == ("A", "B")
    assert [s["name"] for s in _payload(state)["setups"]] == ["A", "B"]


def test_removing_a_point_removes_its_marker(state):
    _seed(state)
    PE.delete_points(state.plan, [1])
    numbers = [p["number"] for p in _payload(state)["points"]]
    assert 1 not in numbers and len(numbers) == 4


# --- the readings table ----------------------------------------------------------

def test_typing_a_rod_reading_records_it(state):
    _seed(state)
    _edit(state, 1, "rod", "45.5")
    assert state.plan.by_number(1).rod_in == pytest.approx(45.5)
    assert "1 of 5 shot" in _payload(state)["coverage"]


def test_a_non_numeric_rod_reading_is_rejected(state):
    _seed(state)
    with pytest.raises(PE.PlanEditError, match="not a number of inches"):
        _edit(state, 1, "rod", "forty five")
    assert state.plan.by_number(1).rod_in is None


def test_clearing_a_rod_reading_marks_it_outstanding(state):
    _seed(state)
    _edit(state, 1, "rod", "45.5")
    _edit(state, 1, "rod", "")
    assert state.plan.by_number(1).rod_in is None
    assert state.plan.coverage()["observed"] == 0


def test_ticking_gnss_promotes_the_position_method(state):
    """Only a FIXED solution beats the planned click; anything else clears."""
    _seed(state)
    _edit(state, 1, "fix", "yes")
    point = state.plan.by_number(1)
    assert point.fix == 4 and point.method == "rtk"

    _edit(state, 1, "fix", "")
    point = state.plan.by_number(1)
    assert point.fix is None and point.method == "planned"


def test_inserting_a_vertex_puts_it_in_the_run_not_at_the_end(state):
    """Numbers are assigned in order of creation; the LINE is ordered by shape.
    Those are different orderings and both have to be respected."""
    plan = _seed(state)
    before = list(plan.line("swale-1").numbers)
    PE.insert_vertex(plan, before[0])

    after = plan.line("swale-1").numbers
    assert len(after) == len(before) + 1
    assert after[0] == before[0] and after[2:] == before[1:]
    assert after[1] == max(p.number for p in plan.points)


def test_inserting_on_a_loose_shot_explains_itself(state):
    _seed(state)
    with pytest.raises(PE.PlanEditError, match="Loose shots"):
        PE.insert_vertex(state.plan, 1)


def test_inserting_after_the_last_vertex_explains_itself(state):
    plan = _seed(state)
    with pytest.raises(PE.PlanEditError, match="last vertex"):
        PE.insert_vertex(plan, plan.line("swale-1").numbers[-1])


# --- derived positions --------------------------------------------------------------

def test_taped_position_solves(state):
    plan = _seed(state)
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

    message = PE.apply_tape(
        plan, 3, "1", m_to_ft(float(np.hypot(*(truth - [E0, N0])))),
        "2", m_to_ft(float(np.hypot(*(truth - [E0 + 4, N0 + 2])))))

    e, n, method, _ = plan.resolve(3)
    assert method == "taped"
    assert e == pytest.approx(truth[0], abs=0.01)
    assert n == pytest.approx(truth[1], abs=0.01)
    assert "exactly determined" in message


def test_a_tape_that_cannot_solve_changes_nothing(state):
    """A failed solve used to leave the point half-edited: method "taped"
    with ties to a point that does not exist, which then broke every redraw."""
    plan = _seed(state)
    with pytest.raises(PE.PlanEditError):
        PE.apply_tape(plan, 3, "1", 10.0, "99", 12.0)
    point = plan.by_number(3)
    assert point.method == "planned" and point.ties == []


def test_station_offset_applies(state):
    plan = _seed(state)
    PE.apply_station_offset(plan, 1, "swale-1", 10.0, 0.0)
    _, _, method, _ = plan.resolve(1)
    assert method == "station_offset"
    assert plan.by_number(1).station_m == pytest.approx(ft_to_m(10.0))


def test_a_point_whose_tie_was_deleted_still_draws(state):
    """Deleting a tape reference makes the taped position unresolvable. The
    point must still be listed and drawn - at its planned mark, saying why."""
    plan = _seed(state)
    for number in (1, 2):
        _measure(state, number, de=0.0, dn=0.0)
    p = plan.by_number(3)
    p.ties = [Tie(1, 4.0), Tie(2, 5.0)]
    p.method = "taped"
    PE.delete_points(plan, [1])

    drawn = _drawn(state, 3)
    assert not drawn["locked"]
    assert (drawn["x"], drawn["y"]) == pytest.approx((drawn["px"], drawn["py"]))
    assert "Could not use its taped position" in drawn["detail"]


# --- feeding the pipeline --------------------------------------------------------------

def test_plan_observations_become_a_layer_and_reach_the_level_network(state):
    _seed(state)
    for num in (1, 2):
        _edit(state, num, "rod", "40.0")

    assert state.PLAN_LAYER in state.layers
    assert len(state.layers[state.PLAN_LAYER]) == 2

    # spots must combine the imported spot layer with the plan's rod shots,
    # because they are the same measurement against the same laser plane.
    spots = state.spots
    assert spots is not None
    assert P.ROD_IN in spots.df.columns
    assert len(spots) > 2


def test_a_plan_with_no_readings_contributes_no_layer(state):
    _seed(state)
    assert state.PLAN_LAYER not in state.layers


def test_plan_round_trips_through_its_file(state, tmp_path):
    from gpsrtk.plan import Plan

    plan = _seed(state)
    _edit(state, 1, "rod", "41.25")
    saved = state.save_plan(tmp_path / "p.yardplan")

    back = Plan.load(saved)
    assert back.epsg == state.site.epsg
    assert [p.number for p in back.points] == [p.number for p in plan.points]
    assert back.by_number(1).rod_in == pytest.approx(41.25)
    assert back.line("swale-1").numbers == plan.line("swale-1").numbers


def test_a_plan_from_another_crs_is_refused(state, tmp_path):
    from gpsrtk.plan import Plan

    other = Plan(epsg=32616)
    other.add_point(E0, N0)
    path = other.save(tmp_path / "elsewhere.yardplan")
    with pytest.raises(ValueError, match="EPSG:32616"):
        state.open_plan(path)
    assert state.plan.points == []


def test_letter_setup_labels_reach_the_level_network(state):
    """SW Maps numbers its setups; a shot plan labels them 'A', 'B'. Forcing a
    float conversion broke the plan -> level network handoff entirely."""
    from gpsrtk import vertical as V

    plan = _seed(state)
    for p in plan.points:
        p.setup = "A"
    for number in (1, 2, 3):
        _edit(state, number, "rod", "42.0")

    setups = V.resolve_setups(state.layers[state.PLAN_LAYER])
    assert set(setups) == {"A"}

    # And the whole solve must go through without raising.
    state.solve_vertical("local")
    assert state.vertical is not None


def test_numeric_setups_still_normalise(spots):
    """0 and 0.0 must remain the same setup, not two."""
    from gpsrtk import filters as F, vertical as V

    lawn = F.KindSelect(names=["lawn"]).apply(spots)
    assert set(V.resolve_setups(lawn)) == {"0"}


def test_plan_layer_without_gnss_height_describes_itself_honestly(state):
    _seed(state)
    _edit(state, 1, "rod", "45.5")
    described = state.layers[state.PLAN_LAYER].describe()
    assert "nan" not in described
    assert "no height yet" in described


def test_plan_shots_do_not_appear_as_unresolved_sessions(state):
    """Laser rod shots carry no GNSS height, so they cannot take part in a
    crossover solve. Including them had them reported as 'unresolved' for want
    of an overlap they could never have."""
    plan = _seed(state)
    for p in plan.points:
        p.setup = "A"
    for number in (1, 2, 3):
        _edit(state, number, "rod", "42.0")

    state.solve_vertical("local")
    model = state.vertical
    assert model.sessions is not None
    assert "plan" not in model.sessions.offsets
    assert "plan" not in model.sessions.unresolved
    assert not any("plan" in note for note in model.notes)


def test_a_reading_re_solves_a_solved_datum(state):
    """Rod readings feed the level network, so typing one can move the datum."""
    _seed(state)
    for p in state.plan.points:
        p.setup = "A"
    _edit(state, 1, "rod", "42.0")
    _edit(state, 2, "rod", "42.0")
    state.solve_vertical("local")
    first = state.vertical

    _edit(state, 2, "rod", "44.0")
    assert state.vertical is not first


def test_an_edit_the_network_cannot_see_does_not_re_solve(state):
    """Placing or moving a shot with no reading changes nothing the level
    network uses. Re-solving for it would stall every click on a big data set."""
    _seed(state)
    state.solve_vertical("local")
    first = state.vertical

    PE.add_point(state.plan, E0 + 6, N0)
    state.plan_changed()
    PE.move_point(state.plan, 1, E0 + 0.5, N0)
    state.plan_changed()
    assert state.vertical is first


# --- purposes, lines and methods from the table ------------------------------------------

def test_purpose_can_be_changed_from_the_table(state):
    """The old table hard-coded Purpose as read-only, so a building corner
    could never be recorded as anything but a generic spot."""
    _seed(state)
    _edit(state, 1, "purpose", "building corner")
    assert state.plan.by_number(1).purpose == "building corner"
    assert _drawn(state, 1)["group"] == "feature"


def test_an_unknown_purpose_is_refused(state):
    _seed(state)
    with pytest.raises(PE.PlanEditError, match="not a known purpose"):
        _edit(state, 1, "purpose", "nonsense")
    assert state.plan.by_number(1).purpose == "spot"


def test_purposes_cover_the_features_that_matter():
    from gpsrtk.plan import PURPOSES

    for wanted in ("building corner", "plat reference", "monument",
                   "driveway edge", "sidewalk edge", "swale bottom",
                   "curb flowline", "benchmark"):
        assert wanted in PURPOSES, wanted


def test_only_terrain_purposes_reach_the_ground_surface(state):
    """A building corner is an accurate point that would be a lie if the
    interpolator treated it as lawn."""
    from gpsrtk.plan import is_terrain

    plan = _seed(state)
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


def test_a_point_can_be_moved_onto_and_off_a_line(state):
    plan = _seed(state)
    _edit(state, 1, "line", "swale-1")
    assert 1 in plan.line("swale-1").numbers
    assert _drawn(state, 1)["line"] == "swale-1"

    _edit(state, 1, "line", "")
    assert 1 not in plan.line("swale-1").numbers


def test_an_unknown_line_is_refused_with_its_name(state):
    _seed(state)
    with pytest.raises(PE.PlanEditError) as raised:
        _edit(state, 1, "line", "swale-9")
    assert raised.value.message == "no line named 'swale-9'"


def test_position_method_can_be_changed_from_the_table(state):
    _seed(state)
    _edit(state, 1, "method", "taped")
    assert state.plan.by_number(1).method == "taped"


def test_an_unknown_method_is_refused(state):
    _seed(state)
    with pytest.raises(PE.PlanEditError, match="not a method"):
        _edit(state, 1, "method", "guessed")


# --- the field sheet's locating columns ---------------------------------------------------

def test_field_sheet_has_somewhere_to_write_the_position(state, tmp_path):
    """Without these columns the locating information has nowhere to go but
    the notes, which then has to be re-read and guessed at."""
    from gpsrtk.io.fieldsheet import write_field_sheet

    _seed(state)
    out = write_field_sheet(state.plan, tmp_path / "s.html", site=state.site)
    doc = out.read_text(encoding="utf-8")

    assert "Tie A" in doc and "Tie B" in doc
    assert "GNSS pt" in doc
    assert "Rod (in)" in doc
    # Each row needs its own boxes, not one shared note field.
    assert doc.count('class="blank tie"') == 2 * len(state.plan.points)
    # And it has to print landscape, or the columns do not fit.
    assert "size: landscape" in doc


def test_field_sheet_shows_the_purpose_of_every_shot(state, tmp_path):
    from gpsrtk.io.fieldsheet import write_field_sheet

    plan = _seed(state)
    plan.by_number(1).purpose = "plat reference"
    out = write_field_sheet(plan, tmp_path / "s.html", site=state.site)
    assert "plat reference" in out.read_text(encoding="utf-8")


# --- entering a measured coordinate ---------------------------------------------------------

def test_measured_position_can_be_typed_in_utm(state):
    """The field sheet has a column for the receiver's coordinates, so there
    has to be somewhere to type them back in."""
    plan = _seed(state)
    PE.set_measured_position(plan, state.site, 1, "UTM m",
                             "449713.250", "4604566.500")

    point = plan.by_number(1)
    assert point.observed_e == pytest.approx(449713.250)
    assert point.observed_n == pytest.approx(4604566.500)
    assert point.method == "rtk" and point.fix == 4
    e, n, method, _ = plan.resolve(1)
    assert method == "rtk" and e == pytest.approx(449713.250)


def test_measured_position_accepts_local_feet(state):
    plan = _seed(state)
    PE.set_measured_position(plan, state.site, 1, "local ft", "45.00", "55.00")
    le, ln = state.site.to_local(plan.by_number(1).observed_e,
                                 plan.by_number(1).observed_n)
    assert m_to_ft(le) == pytest.approx(45.0, abs=0.01)
    assert m_to_ft(ln) == pytest.approx(55.0, abs=0.01)


def test_measured_position_accepts_lat_lon(state):
    """A receiver reads out degrees, not UTM."""
    plan = _seed(state)
    PE.set_measured_position(plan, state.site, 1, "lat/lon",
                             "-93.603278000", "41.591087000")
    point = plan.by_number(1)
    assert point.observed_e == pytest.approx(449719.251, abs=0.05)
    assert point.observed_n == pytest.approx(4604552.424, abs=0.05)


def test_coordinates_round_trip_through_every_frame(state):
    plan = _seed(state)
    point = plan.by_number(1)
    point.method = "rtk"
    point.observed_e, point.observed_n = 449719.251, 4604552.424

    shown = _drawn(state, 1)["observed"]
    assert set(shown) == set(PE.COORD_FRAMES)
    for frame in PE.COORD_FRAMES:
        x, y = shown[frame]
        assert x and y
        PE.set_measured_position(plan, state.site, 1, frame, x, y)
        assert plan.by_number(1).observed_e == pytest.approx(449719.251, abs=0.05)
        assert plan.by_number(1).observed_n == pytest.approx(4604552.424, abs=0.05)


def test_swapped_lat_lon_is_caught(state):
    """Latitude in the longitude box is the classic slip, and it would place
    the point on the other side of the planet."""
    plan = _seed(state)
    with pytest.raises(PE.PlanEditError, match="wrong way round"):
        PE.set_measured_position(plan, state.site, 1, "lat/lon",
                                 "41.591087", "-93.603278")
    assert plan.by_number(1).observed_e is None


def test_non_numeric_coordinates_are_refused(state):
    plan = _seed(state)
    with pytest.raises(PE.PlanEditError, match="must be numbers"):
        PE.set_measured_position(plan, state.site, 1, "UTM m", "north-ish", "")
    assert plan.by_number(1).observed_e is None


def test_a_position_far_from_the_plan_must_be_confirmed(state):
    """A kilometre from the planned mark is a mistyped frame far more often
    than a real shot."""
    plan = _seed(state)
    with pytest.raises(PE.NeedsConfirmation, match="frame is wrong"):
        PE.set_measured_position(plan, state.site, 1, "UTM m",
                                 str(E0 + 1000), str(N0))
    assert plan.by_number(1).observed_e is None

    PE.set_measured_position(plan, state.site, 1, "UTM m",
                             str(E0 + 1000), str(N0), confirm=True)
    assert plan.by_number(1).observed_e == pytest.approx(E0 + 1000)


def test_clearing_a_measured_position_falls_back_to_the_plan(state):
    plan = _seed(state)
    point = plan.by_number(1)
    point.method, point.fix = "rtk", 4
    point.observed_e, point.observed_n = 449719.251, 4604552.424

    PE.clear_measured_position(plan, 1)
    point = plan.by_number(1)
    assert point.observed_e is None and point.fix is None
    assert point.method == "planned"
    _, _, method, _ = plan.resolve(1)
    assert method == "planned"


# --- a measured point stops being a proposal ------------------------------------------------

def test_a_measured_marker_moves_to_the_measured_position(state):
    """The click is history once the shot exists."""
    _seed(state)
    before = _drawn(state, 1)
    point = _measure(state, 1)

    after = _drawn(state, 1)
    x, y = state.site.to_local(point.observed_e, point.observed_n)
    assert (after["x"], after["y"]) == pytest.approx((x, y))
    assert (after["x"], after["y"]) != pytest.approx((before["x"], before["y"]))


def test_a_measured_marker_cannot_be_dragged(state):
    """Dragging would edit the planned position, which is not what is drawn -
    so the marker would appear to spring back."""
    _seed(state)
    _measure(state, 1)

    assert PE.is_locked(state.plan, 1)
    assert _drawn(state, 1)["locked"]
    assert not _drawn(state, 2)["locked"], "unmeasured points still drag"


def test_a_locked_marker_ignores_a_move(state):
    _seed(state)
    point = _measure(state, 1)
    planned = (point.planned_e, point.planned_n)

    assert not PE.move_point(state.plan, 1, E0 + 5.0, N0)
    assert (point.planned_e, point.planned_n) == pytest.approx(planned)


def test_a_taped_position_locks_the_marker_too(state):
    """Any derived position is a measurement, not a click."""
    plan = _seed(state)
    for number in (1, 2):
        _measure(state, number, de=0.0, dn=0.0)
    p = plan.by_number(3)
    p.ties = [Tie(1, 4.0), Tie(2, 5.0)]
    p.method = "taped"

    assert PE.is_locked(plan, 3)
    assert _drawn(state, 3)["locked"]


def test_a_leader_is_drawn_back_to_the_click(state):
    """The gap between plan and truth is worth seeing, not hiding."""
    _seed(state)
    assert _payload(state)["leaders"] == []
    point = _measure(state, 1)
    leaders = _payload(state)["leaders"]
    assert len(leaders) == 1, "one segment: planned, then measured"
    planned = state.site.to_local(point.planned_e, point.planned_n)
    assert leaders[0][0] == pytest.approx(list(planned))


def test_no_leader_when_the_shot_landed_on_the_mark(state):
    """A leader shorter than the marker is a smudge, not information."""
    _seed(state)
    _measure(state, 1, de=0.02, dn=0.0)
    assert _payload(state)["leaders"] == []


def test_clearing_the_measurement_unlocks_and_returns_the_marker(state):
    plan = _seed(state)
    _measure(state, 1)
    PE.clear_measured_position(plan, 1)

    point = plan.by_number(1)
    drawn = _drawn(state, 1)
    x, y = state.site.to_local(point.planned_e, point.planned_n)
    assert (drawn["x"], drawn["y"]) == pytest.approx((x, y))
    assert not drawn["locked"]
    assert _payload(state)["leaders"] == []


# --- deleting ---------------------------------------------------------------------------------

def test_several_points_can_be_deleted_once_confirmed(state):
    plan = _seed(state)
    PE.delete_points(plan, [3, 4, 5], confirm=True)
    assert [p.number for p in plan.points] == [1, 2]


def test_a_bulk_delete_asks_first(state):
    plan = _seed(state)
    with pytest.raises(PE.NeedsConfirmation, match="Delete 5 points"):
        PE.delete_points(plan, [1, 2, 3, 4, 5])
    assert len(plan.points) == 5


def test_deleting_one_point_is_not_confirmed(state):
    """A single mistaken deletion is obvious and trivially redone."""
    plan = _seed(state)
    PE.delete_points(plan, [1])
    assert len(plan.points) == 4


def test_deleting_never_renumbers_the_rest(state):
    """A printed sheet in a pocket is the source of truth; renumbering under
    it would mis-file every reading taken after the edit."""
    plan = _seed(state)
    PE.delete_points(plan, [2])
    assert [p.number for p in plan.points] == [1, 3, 4, 5]
    assert plan.line("swale-1").numbers == [3, 4, 5]


def test_the_hint_goes_quiet_on_a_multi_selection():
    """With several rows there is no single point a typed coordinate belongs
    to, and applying it to whichever was first would be a silent wrong edit."""
    assert "2 points selected" in PE.selection_hint(2)
    assert PE.selection_hint(0) == "Select a row."
