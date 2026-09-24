"""Shot planning: numbering, the three position methods, and the handoff."""

import json

import numpy as np
import pytest

from gpsrtk.plan import SIGMA_M, Plan, PlannedSetup, Tie
from gpsrtk.units import ft_to_m, m_to_ft

# A convenient local frame: numbers are small and easy to reason about.
E0, N0 = 449710.0, 4604565.0


@pytest.fixture
def plan():
    p = Plan(epsg=32615, name="test")
    p.add_point(E0, N0)                       # 1
    p.add_point(E0 + 10, N0)                  # 2
    p.add_point(E0, N0 + 10)                  # 3
    return p


# --- numbering -----------------------------------------------------------

def test_numbers_start_at_one_and_increment(plan):
    assert [p.number for p in plan.points] == [1, 2, 3]


def test_numbers_are_never_reused_after_a_deletion(plan):
    """A printed sheet in a pocket is the source of truth. Renumbering under it
    would mis-file every reading taken after the edit."""
    plan.remove_point(2)
    new = plan.add_point(E0 + 5, N0 + 5)
    assert new.number == 4
    assert plan.by_number(2) is None


def test_removing_a_point_removes_it_from_its_line(plan):
    line = plan.add_line("swale", [(E0, N0 - 5), (E0 + 5, N0 - 5)])
    victim = line.numbers[0]
    plan.remove_point(victim)
    assert victim not in plan.line("swale").numbers


def test_a_line_numbers_its_own_vertices(plan):
    line = plan.add_line("crown", [(E0, N0), (E0 + 3, N0), (E0 + 6, N0)])
    assert len(line.numbers) == 3
    assert all(plan.by_number(n).purpose == "breakline" for n in line.numbers)


def test_duplicate_line_ids_are_refused(plan):
    plan.add_line("swale", [(E0, N0), (E0 + 1, N0)])
    with pytest.raises(ValueError, match="already exists"):
        plan.add_line("swale", [(E0, N0), (E0 + 1, N0)])


# --- position method 1: the planned click --------------------------------

def test_unobserved_points_fall_back_to_the_planned_position(plan):
    e, n, method, sigma = plan.resolve(1)
    assert (e, n) == (E0, N0)
    assert method == "planned"
    assert sigma == SIGMA_M["planned"]


def test_an_rtk_observation_supersedes_the_plan(plan):
    p = plan.by_number(1)
    p.observed_e, p.observed_n, p.fix, p.method = E0 + 0.4, N0 - 0.2, 4, "rtk"
    e, n, method, sigma = plan.resolve(1)
    assert (e, n) == (E0 + 0.4, N0 - 0.2)
    assert method == "rtk"
    assert sigma < SIGMA_M["planned"]


def test_a_float_observation_is_labelled_as_such(plan):
    """Float positions are used, but must not be mistaken for fixed ones."""
    p = plan.by_number(1)
    p.observed_e, p.observed_n, p.fix = E0 + 1.0, N0, 5
    _, _, method, sigma = plan.resolve(1)
    assert method == "rtk_float"
    assert sigma > SIGMA_M["rtk"]


# --- position method 2: taped distances ----------------------------------

def test_trilateration_recovers_a_known_position(plan):
    """Two references shot with RTK, two taped distances, exact geometry."""
    for num, (e, n) in ((1, (E0, N0)), (2, (E0 + 10, N0))):
        p = plan.by_number(num)
        p.observed_e, p.observed_n, p.fix, p.method = e, n, 4, "rtk"

    truth = np.array([E0 + 4.0, N0 + 3.0])
    target = plan.add_point(E0 + 3.0, N0 + 2.0)     # a deliberately rough click
    target.method = "taped"
    target.ties = [Tie(1, float(np.hypot(*(truth - [E0, N0])))),
                   Tie(2, float(np.hypot(*(truth - [E0 + 10, N0]))))]

    e, n, method, _ = plan.resolve(target.number)
    assert method == "taped"
    assert e == pytest.approx(truth[0], abs=1e-3)
    assert n == pytest.approx(truth[1], abs=1e-3)


def test_trilateration_picks_the_side_the_plan_says(plan):
    """Two distances admit two solutions, mirrored across the reference line.
    The planned click is what disambiguates - no extra bookkeeping needed."""
    for num, (e, n) in ((1, (E0, N0)), (2, (E0 + 10, N0))):
        p = plan.by_number(num)
        p.observed_e, p.observed_n, p.fix, p.method = e, n, 4, "rtk"

    d1 = d2 = float(np.hypot(5.0, 4.0))
    for guess_n, expected_sign in ((N0 + 2.0, +1), (N0 - 2.0, -1)):
        t = plan.add_point(E0 + 5.0, guess_n)
        t.method, t.ties = "taped", [Tie(1, d1), Tie(2, d2)]
        _, n, _, _ = plan.resolve(t.number)
        assert np.sign(n - N0) == expected_sign


def test_three_ties_give_redundancy_and_residuals(plan):
    for num, (e, n) in ((1, (E0, N0)), (2, (E0 + 10, N0)), (3, (E0, N0 + 10))):
        p = plan.by_number(num)
        p.observed_e, p.observed_n, p.fix, p.method = e, n, 4, "rtk"

    truth = np.array([E0 + 4.0, N0 + 3.0])
    t = plan.add_point(E0 + 3.5, N0 + 3.5)
    t.method = "taped"
    t.ties = [Tie(num, float(np.hypot(*(truth - ref))))
              for num, ref in ((1, [E0, N0]), (2, [E0 + 10, N0]),
                               (3, [E0, N0 + 10]))]

    _, _, stats = plan.trilaterate(t)
    assert stats["dof"] == 1
    assert stats["max_residual_m"] < 1e-3


def test_a_mistyped_tape_shows_up_as_a_residual(plan):
    for num, (e, n) in ((1, (E0, N0)), (2, (E0 + 10, N0)), (3, (E0, N0 + 10))):
        p = plan.by_number(num)
        p.observed_e, p.observed_n, p.fix, p.method = e, n, 4, "rtk"

    truth = np.array([E0 + 4.0, N0 + 3.0])
    t = plan.add_point(E0 + 4.0, N0 + 3.0)
    t.method = "taped"
    t.ties = [Tie(num, float(np.hypot(*(truth - ref))))
              for num, ref in ((1, [E0, N0]), (2, [E0 + 10, N0]),
                               (3, [E0, N0 + 10]))]
    t.ties[1].distance_m += ft_to_m(1.0)         # one foot mis-read

    _, _, stats = plan.trilaterate(t)
    assert stats["max_residual_m"] > ft_to_m(0.2)


def test_fewer_than_two_ties_is_refused(plan):
    t = plan.add_point(E0, N0)
    t.method, t.ties = "taped", [Tie(1, 5.0)]
    with pytest.raises(ValueError, match="at least two"):
        plan.trilaterate(t)


def test_tape_references_do_not_chain(plan):
    """A reference should be something you actually shot. Chaining tape ties
    compounds error invisibly."""
    a = plan.by_number(1)
    a.method, a.ties = "taped", [Tie(2, 3.0), Tie(3, 4.0)]
    # reference_position must give the plan/observed position, not re-solve.
    assert plan.reference_position(1) == (E0, N0)


# --- position method 3: station and offset -------------------------------

def test_station_and_offset_along_a_line(plan):
    line = plan.add_line("fence", [(E0, N0), (E0 + 20.0, N0)])
    for num in line.numbers:
        p = plan.by_number(num)
        p.observed_e, p.observed_n, p.fix, p.method = (
            p.planned_e, p.planned_n, 4, "rtk")

    t = plan.add_point(E0, N0)
    t.method, t.ref_line = "station_offset", "fence"
    t.station_m, t.offset_m = 8.0, 3.0

    e, n, method, _ = plan.resolve(t.number)
    assert method == "station_offset"
    assert e == pytest.approx(E0 + 8.0)
    # Positive offset is LEFT of travel from first vertex to last; travelling
    # east, left is north.
    assert n == pytest.approx(N0 + 3.0)


def test_negative_offset_goes_the_other_way(plan):
    plan.add_line("fence", [(E0, N0), (E0 + 20.0, N0)])
    t = plan.add_point(E0, N0)
    t.method, t.ref_line = "station_offset", "fence"
    t.station_m, t.offset_m = 8.0, -3.0
    _, n, _, _ = plan.resolve(t.number)
    assert n == pytest.approx(N0 - 3.0)


def test_station_offset_needs_a_real_line(plan):
    t = plan.add_point(E0, N0)
    t.method, t.ref_line = "station_offset", "nope"
    t.station_m, t.offset_m = 1.0, 1.0
    with pytest.raises(ValueError, match="at least two vertices"):
        plan.station_offset(t)


def test_station_offset_needs_both_values(plan):
    plan.add_line("fence", [(E0, N0), (E0 + 20.0, N0)])
    t = plan.add_point(E0, N0)
    t.method, t.ref_line, t.station_m = "station_offset", "fence", 4.0
    with pytest.raises(ValueError, match="station and an offset"):
        plan.station_offset(t)


# --- handoff -------------------------------------------------------------

def test_only_points_with_readings_reach_the_pipeline(plan):
    plan.by_number(1).rod_in = 45.5
    frame = plan.to_frame()
    assert list(frame["point_id"]) == [1]
    assert frame["position_method"].iloc[0] == "planned"


def test_the_frame_carries_position_provenance(plan):
    p1, p2 = plan.by_number(1), plan.by_number(2)
    p1.rod_in, p1.method = 40.0, "rtk"
    p1.observed_e, p1.observed_n, p1.fix = E0 + 0.1, N0, 4
    p2.rod_in = 38.0
    frame = plan.to_frame().set_index("point_id")

    assert frame.loc[1, "position_method"] == "rtk"
    assert frame.loc[2, "position_method"] == "planned"
    assert frame.loc[1, "position_sigma_m"] < frame.loc[2, "position_sigma_m"]


def test_coverage_counts_what_is_outstanding(plan):
    plan.by_number(1).rod_in = 45.5
    cov = plan.coverage()
    assert cov == {"planned": 3, "observed": 1, "outstanding": 2,
                   "by_method": {"planned": 1}}


# --- persistence ---------------------------------------------------------

def test_round_trips_through_json(tmp_path, plan):
    plan.add_line("swale", [(E0, N0 - 4), (E0 + 6, N0 - 4)])
    plan.setups.append(PlannedSetup("A", E0 + 2, N0 + 2, note="by the shed"))
    t = plan.by_number(1)
    t.method, t.ties, t.rod_in = "taped", [Tie(2, 4.0), Tie(3, 5.0)], 41.25

    saved = plan.save(tmp_path / "p")
    back = Plan.load(saved)

    assert saved.suffix == ".yardplan"
    assert [p.number for p in back.points] == [p.number for p in plan.points]
    assert back.by_number(1).ties[0].ref_number == 2
    assert isinstance(back.by_number(1).ties[0], Tie)
    assert back.by_number(1).rod_in == 41.25
    assert back.line("swale").numbers == plan.line("swale").numbers
    assert back.setups[0].note == "by the shed"


def test_a_newer_plan_format_is_refused(tmp_path, plan):
    payload = plan.to_dict()
    payload["version"] = 99
    path = tmp_path / "future.yardplan"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="newer"):
        Plan.load(path)


def test_tie_reports_distance_in_feet():
    """The tape reads feet; storage is metres. The conversion has one home."""
    assert Tie(1, ft_to_m(8.25)).distance_ft == pytest.approx(8.25)


# --- field sheet ---------------------------------------------------------

def test_field_sheet_is_self_contained_and_lists_every_point(tmp_path, plan):
    """It goes on a clipboard with no network. Every planned point must have a
    row, or a shot gets taken and has nowhere to be written down."""
    from gpsrtk.io.fieldsheet import write_field_sheet

    plan.add_line("swale", [(E0, N0 - 4), (E0 + 6, N0 - 4)])
    out = write_field_sheet(plan, tmp_path / "sheet.html", title="test sheet")
    doc = out.read_text(encoding="utf-8")

    assert out.suffix == ".html"
    assert "test sheet" in doc
    for p in plan.points:
        assert f">{p.number}</td>" in doc, f"point {p.number} missing from table"
        assert f">{p.number}</text>" in doc, f"point {p.number} missing from map"
    # No external references: it has to work with no network. The SVG
    # namespace is an inert identifier, not something the browser fetches.
    without_ns = doc.replace("http://www.w3.org/2000/svg", "")
    assert "http://" not in without_ns
    assert "https://" not in without_ns


def test_field_sheet_inlines_the_basemap(tmp_path, plan):
    import numpy as np

    from gpsrtk.io.fieldsheet import write_field_sheet
    from gpsrtk.io.imagery import RasterLayer
    from gpsrtk.surface import Extent

    layer = RasterLayer(
        image=np.random.default_rng(0).integers(0, 255, (64, 64, 3), np.uint8),
        extent=Extent(E0 - 20, E0 + 20, N0 - 20, N0 + 20), epsg=32615,
        source="stub")
    out = write_field_sheet(plan, tmp_path / "s.html", basemap=layer)
    assert "data:image/png;base64," in out.read_text(encoding="utf-8")


def test_field_sheet_survives_an_empty_plan(tmp_path):
    from gpsrtk.io.fieldsheet import write_field_sheet

    out = write_field_sheet(Plan(name="nothing yet"), tmp_path / "e.html")
    assert out.exists()
