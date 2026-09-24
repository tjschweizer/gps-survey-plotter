"""Slope, drainage direction and contours, checked against ground with a known answer.

A tilted plane has one slope and one downhill direction everywhere, and its
contours are straight lines at predictable places. If any of that comes out
wrong - a transposed gradient, a sign error, contours on the wrong datum - it
shows here as a number that is simply not what the plane says.
"""

import numpy as np
import pytest

from gpsrtk import terrain as T
from gpsrtk.surface import Extent, Surface

GE, GN = 0.02, -0.01          # the plane rises 2 cm per m east, falls 1 cm per m north


def plane(size=101, span=40.0, mask=None, base=100.0):
    """z = base + GE*x + GN*y over a square of `span` metres."""
    x = np.linspace(0.0, span, size)
    y = np.linspace(0.0, span, size)
    X, Y = np.meshgrid(x, y)
    z = base + GE * X + GN * Y
    m = np.zeros_like(z, bool) if mask is None else mask
    return Surface(z=z, mask=m, extent=Extent(0.0, span, 0.0, span),
                   px=span / size, n_points=1, n_cells=1)


def test_the_grid_axes_are_the_nodes_the_surface_was_built_on():
    s = plane(size=11, span=10.0)
    x, y = T.grid_axes(s)
    assert x[0] == 0.0 and x[-1] == 10.0 and len(x) == 11
    assert T.node_spacing(s) == pytest.approx((1.0, 1.0))


def test_slope_of_a_plane_is_its_grade_everywhere():
    field = T.slope(plane())
    expected = 100.0 * np.hypot(GE, GN)                 # 2.236 %
    assert np.nanmedian(field.slope_pct) == pytest.approx(expected, rel=1e-3)
    assert np.nanmax(np.abs(field.slope_pct - expected)) < 1e-6


def test_arrows_point_downhill_not_uphill():
    """Water runs down the gradient. A sign error here sends every arrow
    uphill and the map looks just as plausible."""
    field = T.slope(plane())
    down = -np.array([GE, GN]) / np.hypot(GE, GN)
    assert np.nanmedian(field.down_e) == pytest.approx(down[0], abs=1e-6)
    assert np.nanmedian(field.down_n) == pytest.approx(down[1], abs=1e-6)


def test_rows_run_north_so_a_north_slope_is_not_read_as_east():
    """Row 0 is the south edge. Mixing up which axis is which transposes the
    slope field without making it look any less real."""
    x = np.linspace(0, 20, 41)
    X, Y = np.meshgrid(x, x)
    s = Surface(z=0.05 * Y, mask=np.zeros_like(X, bool),
                extent=Extent(0, 20, 0, 20), px=0.5, n_points=1, n_cells=1)
    field = T.slope(s)
    assert np.nanmedian(field.down_n) == pytest.approx(-1.0)
    assert np.nanmedian(np.abs(field.down_e)) == pytest.approx(0.0, abs=1e-9)


def test_unmeasured_ground_has_no_slope_and_no_arrow():
    mask = np.zeros((101, 101), bool)
    mask[40:60, 40:60] = True                            # the house
    field = T.slope(plane(mask=mask))
    assert np.isnan(field.slope_pct[mask]).all()
    assert np.isnan(field.down_e[mask]).all()
    assert np.isfinite(field.slope_pct[~mask]).all()


def test_slope_does_not_depend_on_the_grid_resolution():
    """Smoothing is in metres, so the on-screen map and a printed one agree."""
    rng = np.random.default_rng(3)

    def noisy(size):
        s = plane(size=size)
        s.z = s.z + rng.normal(0, 0.01, s.z.shape)
        return s

    coarse = T.slope(noisy(81)).stats()
    fine = T.slope(noisy(321)).stats()
    assert fine["median"] == pytest.approx(coarse["median"], rel=0.1)


def test_flat_ground_has_no_direction():
    s = plane()
    s.z = np.full_like(s.z, 100.0)
    assert T.drainage_arrows(s, 2.0) == []


def test_drainage_arrows_sit_on_a_lattice_at_the_spacing():
    arrows = T.drainage_arrows(plane(), 4.0)
    es = sorted({round(a.e, 6) for a in arrows})
    assert np.diff(es) == pytest.approx(4.0, abs=0.01)
    assert all(np.hypot(a.down_e, a.down_n) == pytest.approx(1.0) for a in arrows)


def test_drainage_arrows_skip_unmeasured_ground():
    mask = np.zeros((101, 101), bool)
    mask[:, 50:] = True                                  # east half unmeasured
    arrows = T.drainage_arrows(plane(mask=mask), 2.0)
    assert arrows and max(a.e for a in arrows) < 20.0


def test_mapped_area_counts_only_measured_ground():
    mask = np.zeros((101, 101), bool)
    mask[:, 50:] = True
    s = plane(mask=mask)
    dx, dy = T.node_spacing(s)
    assert T.mapped_area_m2(s) == pytest.approx((~mask).sum() * dx * dy)


def test_contours_are_measured_from_the_lowest_point():
    s = plane()
    low, high = T.relief(s)
    levels = T.contours(s, 0.05)
    assert [round(lv.above_low_m, 6) for lv in levels[:3]] == [0.05, 0.10, 0.15]
    assert levels[0].height_m == pytest.approx(low + 0.05)
    assert all(lv.height_m < high for lv in levels)
    # Every second level carries a label, as on the printed map.
    assert [lv.major for lv in levels[:4]] == [False, True, False, True]


def test_a_contour_lies_where_the_plane_has_that_height():
    s = plane()
    level = T.contours(s, 0.10)[2]                      # 30 cm above the low point
    for line in level.lines:
        z = 100.0 + GE * line[:, 0] + GN * line[:, 1]
        assert z == pytest.approx(level.height_m, abs=1e-6)


def test_contours_stop_at_the_edge_of_the_survey():
    """A contour must not run across the house footprint."""
    mask = np.zeros((101, 101), bool)
    mask[:, 45:55] = True
    s = plane(mask=mask)
    x0, x1 = T.grid_axes(s)[0][[45, 54]]
    for level in T.contours(s, 0.05):
        for line in level.lines:
            inside = (line[:, 0] > x0 + 0.5) & (line[:, 0] < x1 - 0.5)
            assert not inside.any()


def test_a_bad_interval_is_refused():
    with pytest.raises(ValueError):
        T.contours(plane(), 0.0)


def test_real_ground_matches_the_recorded_slope(tracks, site):
    """plan.py records a median surface slope of 6.1% and p90 of 13% for the
    reference lot. The slope map has to agree with what was measured before."""
    from gpsrtk import filters as F
    from gpsrtk.surface import build_surface

    s = build_surface(F.default_chain().run(tracks), site, size=320)
    stats = T.slope(s).stats()
    assert 3.0 < stats["median"] < 9.0
    assert stats["p90"] > stats["median"]
