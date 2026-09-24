"""Regression tests against the findings recorded in CLAUDE.md.

These are the acceptance criteria for the refactor. The archive scripts
produced these numbers; anything that changes them is a regression, not an
improvement, unless the change is deliberate and CLAUDE.md is updated with it.
"""

import numpy as np
import pytest

from gpsrtk import qc
from gpsrtk.model import pointset as P
from gpsrtk.site import Site
from gpsrtk.surface import build_surface

# A representative coordinate on the example site.
SEED_LAT, SEED_LON = 41.591087000, -93.603278000


# --- site ----------------------------------------------------------------

def test_origin_snaps_and_is_reproducible():
    """A site seeded anywhere on a lot must land on the same origin.

    The local origin must never move or previously exported surfaces stop
    aligning, so this pins the derivation rule: snap to the 25 m grid below
    the seed, whichever point of the property the seed happened to be.
    """
    s = Site.from_seed("derived", SEED_LAT, SEED_LON)
    assert s.epsg == 32615
    assert s.origin_e == 449700.0
    assert s.origin_n == 4604550.0
    assert s.origin_e % 25 == 0 and s.origin_n % 25 == 0

    # Snapping is to the gridline at or below the seed, so any seed in the
    # same 25 m cell gives the same origin. Two seeds either side of a
    # gridline legitimately do not, which is why the origin is stored in the
    # project rather than re-derived from whichever point comes first.
    inside = Site.from_seed("nearby", SEED_LAT, SEED_LON + 0.00002)
    assert (inside.origin_e, inside.origin_n) == (s.origin_e, s.origin_n)

    # Idempotent: re-seeding from the origin itself does not walk it.
    from pyproj import Transformer

    tx = Transformer.from_crs("EPSG:32615", "EPSG:4326", always_xy=True)
    lon, lat = tx.transform(s.origin_e + 1.0, s.origin_n + 1.0)
    again = Site.from_seed("again", lat, lon)
    assert (again.origin_e, again.origin_n) == (s.origin_e, s.origin_n)


def test_grid_convergence_is_in_degrees_not_radians(site):
    """pyproj already converts convergence to degrees.

    Converting again gave -24.45 deg where the truth was -0.43, which is the
    kind of error that looks like a real rotation and quietly ruins every plat
    bearing drawn in UTM. Anywhere in a zone's interior the convergence is a
    fraction of a degree, so the magnitude alone catches the mistake.
    """
    conv, scale = site.factors(SEED_LAT, SEED_LON)
    assert abs(conv) < 2.0, "convergence looks like radians converted twice"
    assert conv < 0, "west of the central meridian, convergence is negative"
    # Enough to throw a plat bearing roughly 0.7 ft over 100 ft: small, and
    # the same order as the building-diagonal disagreements worth chasing.
    assert 0.1 < abs(conv) < 1.0

    # Combined scale factor near 0.9996 across a UTM zone's usable width.
    assert scale == pytest.approx(0.9996, abs=0.0005)
    assert 0.999 < scale < 1.001


# --- reader --------------------------------------------------------------

def test_reader_normalises_both_latlon_spellings(tracks, spots):
    """Tracks use Lat/Lon, spots use Latitude/Longitude, for the same quantity."""
    assert tracks.has(P.LAT, P.LON)
    assert spots.has(P.LAT, P.LON)


def test_spot_custom_attributes_survive(spots):
    assert spots.has(P.ROD_IN, P.SETUP, P.KIND)
    assert set(spots.df[P.KIND].dropna().unique()) == {"lawn", "bldg"}


def test_timestamps_all_parse(tracks, spots):
    assert tracks.df[P.TIME].isna().sum() == 0
    assert spots.df[P.TIME].isna().sum() == 0
    assert (tracks.df[P.TZ] == "CDT").all()


def test_antenna_height_is_zero_not_missing(tracks):
    """Z is antenna height, not ground. If this ever stops being true the
    per-session offset assumptions change, so it must fail loudly."""
    assert (tracks.df[P.ANT_HT] == 0.0).all()


def test_float_fraction_matches_canopy_finding(tracks):
    # CLAUDE.md: canopy on the west/northwest drives ~48% float.
    frac = (tracks.df[P.FIX] == P.FIX_FLOAT).mean()
    assert frac == pytest.approx(0.48, abs=0.02)


# --- measured QC ---------------------------------------------------------

def test_crossover_accuracy_fixed_only(tracks):
    """CLAUDE.md: 5.77 cm RMS / 3.20 cm median, pairs within 30 cm, >60 s apart."""
    s = qc.crossover_stats(tracks.fixed, radius_m=0.30, min_seconds=60.0)
    assert s["rms"] * 100 == pytest.approx(5.77, abs=0.15)
    assert s["median_abs"] * 100 == pytest.approx(3.20, abs=0.15)


@pytest.mark.parametrize("min_speed,exp_rms,exp_kept", [
    (0.9, 4.13, 0.62),
    (1.1, 3.32, 0.40),
])
def test_speed_filtering(tracks, min_speed, exp_rms, exp_kept):
    """CLAUDE.md: >0.9 m/s -> 4.13 cm keeping 62%; >1.1 m/s -> 3.32 cm keeping 40%."""
    fixed = tracks.fixed
    fast = fixed.select(fixed.df[P.SPEED] > min_speed)
    assert len(fast) / len(fixed) == pytest.approx(exp_kept, abs=0.02)
    s = qc.crossover_stats(fast, radius_m=0.30, min_seconds=60.0)
    assert s["rms"] * 100 == pytest.approx(exp_rms, abs=0.25)


def test_float_is_much_worse_than_fixed(tracks):
    """Sanity: the fix filter must actually be doing work."""
    all_s = qc.crossover_stats(tracks)
    fix_s = qc.crossover_stats(tracks.fixed)
    assert all_s["rms"] > 5 * fix_s["rms"]


# --- surfacing -----------------------------------------------------------

def test_measured_fraction(tracks, site):
    """CLAUDE.md: current best is ~42% measured."""
    fixed = tracks.fixed
    z = fixed.df[P.Z]
    lo, hi = np.percentile(z, site.surface.despike_percentiles)
    clean = fixed.select((z >= lo) & (z <= hi))

    s = build_surface(clean, site)
    assert s.measured_fraction == pytest.approx(0.42, abs=0.02)
    assert s.z.shape == (site.surface.raster_size,) * 2
    assert not np.isnan(s.z).any(), "filled grid must contain no NaN"
    assert s.mask.any(), "something must be masked as unmeasured"


def test_binning_is_anchored_to_site_origin(tracks, site):
    """Two subsets of the same data must land on the same bin grid, or
    differencing sessions is meaningless."""
    from gpsrtk.surface import bin_cells

    fixed = tracks.fixed
    half = fixed.select(fixed.df.index < len(fixed) // 2)
    a = bin_cells(fixed, 0.5, site)
    b = bin_cells(half, 0.5, site)
    shared = set(zip(a.ix, a.iy)) & set(zip(b.ix, b.iy))
    assert len(shared) > 100
