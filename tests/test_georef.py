"""Aligning imagery to the survey.

The imagery is the thing that moves. Every test here is written so that a sign
error - shifting the points instead of the photo, or shifting either the wrong
way - fails rather than merely looking odd.
"""

import pytest

from gpsrtk.georef import (PHOTO_IDENTIFIABLE, ImageryOffset,
                           offset_candidates, residual_report,
                           solve_imagery_offset)
from gpsrtk.plan import Plan
from gpsrtk.units import m_to_ft

E0, N0 = 449712.0, 4604565.0

# The imagery is a foot east and half a foot south of truth, in metres.
TRUE_DE, TRUE_DN = 0.30, -0.15


def _plan(*, n=4, purpose="building corner", noise=None):
    """Corners clicked on the photo, then shot.

    The click lands where the photo puts the corner, so `planned` is the truth
    MINUS the shift; the shot lands on truth.
    """
    plan = Plan()
    noise = noise or [(0.0, 0.0)] * n
    for i in range(n):
        truth_e, truth_n = E0 + 6.0 * i, N0 + 4.0 * i
        p = plan.add_point(truth_e - TRUE_DE + noise[i][0],
                           truth_n - TRUE_DN + noise[i][1], purpose=purpose)
        p.observed_e, p.observed_n = truth_e, truth_n
        p.method, p.fix = "rtk", 4
    return plan


def test_the_shift_is_the_offset_between_click_and_shot():
    offset = solve_imagery_offset(_plan())
    assert offset.de == pytest.approx(TRUE_DE)
    assert offset.dn == pytest.approx(TRUE_DN)
    assert offset.n == 4
    assert offset.rms_m == pytest.approx(0.0, abs=1e-9)


def test_the_shift_moves_the_photo_onto_the_truth():
    """The whole sign convention in one assertion.

    A feature drawn at its photo position, plus the offset, must land on where
    the feature actually is.
    """
    plan = _plan()
    offset = solve_imagery_offset(plan)
    for p in plan.points:
        assert p.planned_e + offset.de == pytest.approx(p.observed_e)
        assert p.planned_n + offset.dn == pytest.approx(p.observed_n)


def test_scatter_is_reported_and_is_not_absorbed_into_the_shift():
    """Sloppy clicks must show up as residuals, not vanish into the mean."""
    noise = [(0.20, 0.0), (-0.20, 0.0), (0.0, 0.20), (0.0, -0.20)]
    offset = solve_imagery_offset(_plan(noise=noise))

    # Symmetric noise leaves the mean alone...
    assert offset.de == pytest.approx(TRUE_DE)
    assert offset.dn == pytest.approx(TRUE_DN)
    # ...and shows up in full in the spread. The click sign is flipped
    # relative to the residual, but the magnitude is what matters.
    assert offset.rms_m == pytest.approx(0.20, abs=1e-9)
    assert offset.max_residual_m == pytest.approx(0.20, abs=1e-9)
    assert offset.dof == 6


def test_lawn_spots_cannot_define_the_offset():
    """There is nothing identifiable in open grass to have clicked."""
    plan = _plan(purpose="spot")
    assert offset_candidates(plan) == []
    with pytest.raises(ValueError, match="see in the photo"):
        solve_imagery_offset(plan)


def test_selecting_rows_overrides_the_purpose_filter():
    """A user who picked the rows has said something the column cannot."""
    plan = _plan(purpose="spot")
    offset = solve_imagery_offset(plan, numbers=[1, 2])
    assert offset.n == 2 and offset.used == [1, 2]
    assert offset.de == pytest.approx(TRUE_DE)


def test_an_unmeasured_point_contributes_nothing():
    """Only the click exists, so it says nothing about where the photo is."""
    plan = _plan()
    loose = plan.add_point(E0 + 30, N0, purpose="building corner")
    assert loose.number not in [p.number for p in offset_candidates(plan)]
    assert solve_imagery_offset(plan).n == 4


def test_a_single_point_says_so_rather_than_implying_a_check():
    offset = solve_imagery_offset(_plan(n=1))
    assert offset.n == 1 and offset.dof == 0
    assert "no check on it" in offset.describe()


def test_wide_scatter_warns_that_a_shift_is_the_wrong_model():
    noise = [(1.5, 0.0), (-1.5, 0.0), (0.0, 1.5), (0.0, -1.5)]
    offset = solve_imagery_offset(_plan(noise=noise))
    assert "not a pure translation" in offset.describe()


def test_a_hand_typed_offset_carries_no_statistics():
    """Nothing was solved, so nothing may be claimed about accuracy."""
    manual = ImageryOffset(de=0.3, dn=-0.1)
    assert manual.n == 0 and not manual.residuals_m
    text = manual.describe()
    assert "by hand" in text and "RMS" not in text


def test_zero_and_magnitude():
    assert ImageryOffset().zero
    assert not ImageryOffset(de=0.01).zero
    assert ImageryOffset(de=3.0, dn=4.0).magnitude_m == pytest.approx(5.0)


def test_the_offset_round_trips_as_a_pair_of_numbers():
    back = ImageryOffset.from_list(ImageryOffset(de=0.3, dn=-0.15).to_list())
    assert (back.de, back.dn) == pytest.approx((0.3, -0.15))
    assert ImageryOffset.from_list(None).zero


def test_residuals_are_reported_worst_first():
    noise = [(0.05, 0.0), (0.60, 0.0), (0.0, 0.20), (-0.65, -0.20)]
    lines = residual_report(solve_imagery_offset(_plan(noise=noise)))
    values = [float(line.split(":")[1].split("in")[0]) for line in lines]
    assert values == sorted(values, reverse=True)
    assert len(lines) == 4


def test_purposes_used_are_things_you_can_actually_see():
    from gpsrtk.plan import PURPOSES

    assert PHOTO_IDENTIFIABLE <= set(PURPOSES), "unknown purpose in the filter"
    assert "building corner" in PHOTO_IDENTIFIABLE
    assert "driveway edge" in PHOTO_IDENTIFIABLE
    # Ground shots are what the imagery cannot pin down.
    for hidden in ("spot", "swale bottom", "crown", "check"):
        assert hidden not in PHOTO_IDENTIFIABLE


def test_feet_conversion_in_the_description_is_right():
    text = ImageryOffset(de=0.3048, dn=0.0, n=0).describe()
    assert "+1.00" in text and m_to_ft(0.3048) == pytest.approx(1.0)
