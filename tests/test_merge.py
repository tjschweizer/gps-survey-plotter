"""Merging outings.

The property these guard is not "the points got added up". It is that a merge
never quietly produces a data set whose vertical datum is unknowable. Two
outings that share no ground have an offset nothing in the data determines, and
the only fix is to go back out - so the merge has to say so while that is still
possible, not after the mower is put away.
"""

import os

import numpy as np
import pandas as pd
import pytest

from gpsrtk import merge as M
from gpsrtk.model import pointset as P
from gpsrtk.model.pointset import PointSet

E0, N0 = 449710.0, 4604565.0
DAY = pd.Timestamp("2026-08-27 13:00:00")


def _set(session, n=60, *, e0=E0, offset=0.0, day=DAY, fix=4):
    """A short pass of points, optionally shifted in height.

    Latitude and longitude are derived from the projected coordinates rather
    than invented, because the CRS check compares the two against each other -
    points whose lat/lon disagreed with their own E/N would look like a zone
    mismatch that is not there.

    Timestamps are a minute apart so that every cross-session pair clears the
    60 s separation a crossover requires.
    """
    from pyproj import Transformer

    i = np.arange(n)
    e = e0 + i * 0.25
    nn = np.full(n, N0, dtype=float)
    tx = Transformer.from_crs("EPSG:32615", "EPSG:4326", always_xy=True)
    lon, lat = tx.transform(e, nn)
    return pd.DataFrame({
        P.E: e, P.N: nn,
        P.Z: 262.0 + i * 0.001 + offset,
        P.LAT: lat, P.LON: lon,
        P.FIX: np.full(n, fix), P.TIME: day + pd.to_timedelta(i * 61, "s"),
        P.SESSION: session, P.SOURCE: f"{session}.zip",
    })


def _ps(*frames):
    return PointSet(df=pd.concat(frames, ignore_index=True), layer="track_points")


# --- merging layers -------------------------------------------------------

def test_layers_of_the_same_name_are_combined():
    a = _ps(_set("A"))
    b = _ps(_set("B"))
    merged, counts = M.merge_layers({"track_points": a}, {"track_points": b})
    assert len(merged["track_points"]) == 120
    assert counts["track_points"] == (60, 120)


def test_a_layer_only_one_side_has_is_carried_through():
    merged, counts = M.merge_layers(
        {"track_points": _ps(_set("A"))}, {"spots": _ps(_set("B", n=3))})
    assert set(merged) == {"track_points", "spots"}
    assert counts == {"spots": (0, 3)}


def test_columns_are_unioned_not_intersected():
    """A newer format carries extra columns; the old data must not lose its
    own, and the new columns must not vanish."""
    a = _set("A")
    b = _set("B")
    b[P.BASELINE] = 7378.0
    merged, _ = M.merge_layers({"t": _ps(a)}, {"t": _ps(b)})
    d = merged["t"].df
    assert P.BASELINE in d.columns
    assert d[P.BASELINE].notna().sum() == 60
    assert d[P.BASELINE].isna().sum() == 60


# --- diagnosis ------------------------------------------------------------

def test_sessions_are_summarised():
    info = M.describe_sessions(_ps(_set("A"), _set("B", n=20, fix=5)))
    assert [s.name for s in info] == ["A", "B"]
    assert info[0].points == 60 and info[0].fixed == 60
    assert info[1].floated == 20 and info[1].fixed_fraction == 0.0


def test_overlapping_sessions_are_counted():
    """Same ground, different days: every pair is evidence of the offset."""
    counts = M.session_overlap(
        _ps(_set("A"), _set("B", day=DAY + pd.Timedelta(days=7))))
    assert counts[("A", "B")] > 0


def test_sessions_on_separate_ground_report_no_overlap():
    counts = M.session_overlap(_ps(_set("A"), _set("B", e0=E0 + 500)))
    assert counts == {}


def test_a_session_with_no_overlap_is_named_as_unrecoverable():
    ps = _ps(_set("A"), _set("B", day=DAY + pd.Timedelta(days=7)),
             _set("C", e0=E0 + 500))
    report = M.diagnose(ps)
    assert report.unlinked == ["C"]
    assert not report.reconcilable
    text = report.describe()
    assert "CANNOT BE RECONCILED" in text
    assert "re-walk" in text


def test_a_chain_of_overlap_is_enough():
    """A tied to B and B to C puts C on A's datum without A and C ever
    sharing ground. Requiring every pair to overlap would reject good data."""
    a = _set("A")
    b = _set("B", e0=E0 + 7.0, day=DAY + pd.Timedelta(days=7))
    c = _set("C", e0=E0 + 14.0, day=DAY + pd.Timedelta(days=14))
    report = M.diagnose(_ps(a, b, c))
    assert report.overlaps, "the staggered passes should overlap in sequence"
    assert report.unlinked == []
    assert report.reconcilable


def test_thin_overlap_is_flagged_without_being_rejected():
    counts = {("A", "B"): 3}
    report = M.MergeReport(sessions=[M.SessionInfo("A", 1, 1, 0),
                                     M.SessionInfo("B", 1, 1, 0)],
                           overlaps=counts,
                           thin=[("A", "B", 3)])
    assert "WARNING" in report.describe()
    assert report.reconcilable, "thin is not the same as unrecoverable"


def test_rod_shots_do_not_count_as_overlap():
    """A laser shot reaches the datum through the level network, so it can
    never tie two sessions by GNSS height. Counting it would make a session
    look better connected than it is."""
    laser = _set("B", n=60, day=DAY + pd.Timedelta(days=7))
    laser[P.Z] = np.nan
    assert M.session_overlap(_ps(_set("A"), laser)) == {}


def test_one_session_needs_no_reconciling():
    report = M.diagnose(_ps(_set("A")))
    assert report.reconcilable and report.overlaps == {}


# --- coordinate frames ----------------------------------------------------

def test_matching_coordinates_are_not_disturbed():
    ps = _ps(_set("A"))
    assert M.crs_disagreement_m(ps, 32615) < 0.01


def test_a_different_zone_is_detected_and_repaired():
    """A reader is handed a file, not a site, so the .swmz reader picks the
    zone from the data. Mixing zones silently would put layers kilometres
    apart while both looked plausible."""
    ps = _ps(_set("A"))
    wrong = ps.df.copy()
    wrong[P.E] += 500_000.0
    broken = PointSet(df=wrong, layer="t")

    assert M.crs_disagreement_m(broken, 32615) > 1.0
    fixed = M.reproject(broken, 32615)
    assert M.crs_disagreement_m(fixed, 32615) < 0.01
    assert fixed.df[P.E].iloc[0] == pytest.approx(ps.df[P.E].iloc[0], abs=0.01)


def test_reprojecting_without_lat_lon_says_why():
    d = _set("A").drop(columns=[P.LAT, P.LON])
    with pytest.raises(ValueError, match="no lat/lon"):
        M.reproject(PointSet(df=d, layer="t"), 32615)


# --- through the application state ---------------------------------------
#
# Two synthetic outings over the same ground a month apart, the second on a
# mount 5 cm higher - the situation merging exists for. `synthetic.py` says
# what they contain.

@pytest.fixture
def fresh(tmp_path):
    from gpsrtk.app import AppState
    from gpsrtk.site import example_site

    return AppState(site=example_site(), cache_dir=tmp_path / "cache")


def test_adding_an_export_keeps_the_first(fresh, synthetic_zip, synthetic_outing2):
    fresh.load(synthetic_zip)
    before = len(fresh.layers["track_points"])

    result = fresh.add_export(synthetic_outing2)

    after = len(fresh.layers["track_points"])
    assert after > before
    assert result.layers["track_points"] == (before, after)
    assert result.added == after - before
    assert len(fresh.sources) == 2
    assert len(result.sessions) == 2
    assert result.reconcilable


def test_the_same_file_twice_is_refused(fresh, synthetic_zip):
    """Loading it again would double every point and make the duplicates look
    like a perfect crossover."""
    fresh.load(synthetic_zip)
    with pytest.raises(ValueError, match="already loaded"):
        fresh.add_export(synthetic_zip)


def test_a_renamed_export_keeps_its_layer_names(tmp_path, synthetic_zip):
    """A browser saves a second download as "X (1).zip"; the files inside
    still carry the project's own name, and the layers are the same layers."""
    import shutil

    from gpsrtk.io import read_any

    renamed = tmp_path / "Synthetic Yard (1).zip"
    shutil.copy(synthetic_zip, renamed)
    exp = read_any(renamed)
    assert set(exp.layers) == {"track_points", "spots", "feature_points"}
    # Sessions are still named after the file, as before.
    assert exp["track_points"].df[P.SESSION].iloc[0].startswith("Synthetic Yard (1)/")


def test_the_type_attribute_is_normalised(tmp_path):
    """`type` is typed by hand, and everything downstream matches it
    exactly. "Lawn " must arrive as "lawn", or the level network never sees
    the shot."""
    import zipfile

    from synthetic import spot_rows, track_points

    from gpsrtk.io import read_any

    spots = spot_rows()
    spots["type"] = ["Lawn ", " LAWN", "lawn", "Bldg"] + ["lawn"] * (len(spots) - 4)
    path = tmp_path / "Typed.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Typed_TRACK_POINTS.csv", track_points().head(50).to_csv(index=False))
        z.writestr("Typed_spots.csv", spots.to_csv(index=False))
    kinds = read_any(path)["spots"].df[P.KIND]
    assert list(kinds[:4]) == ["lawn", "lawn", "lawn", "bldg"]


def test_a_rod_reading_written_as_text_is_read(tmp_path):
    """SW Maps' number field pushed readings into text such as "63 1/4",
    which used to become NaN and silently drop out of the level network."""
    import zipfile

    from synthetic import spot_rows, track_points

    from gpsrtk.io import read_any

    spots = spot_rows()
    spots["height number"] = spots["height number"].astype(object)
    spots.loc[0, "height number"] = "63 1/4"
    spots.loc[1, "height number"] = "5' 3 1/4\""
    spots.loc[2, "height number"] = "5-3-1/4"
    spots.loc[3, "height number"] = "forty"
    path = tmp_path / "Rods.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Rods_TRACK_POINTS.csv", track_points().head(50).to_csv(index=False))
        z.writestr("Rods_spots.csv", spots.to_csv(index=False))
    rods = read_any(path)["spots"].df[P.ROD_IN]
    assert list(rods[:3]) == pytest.approx([63.25] * 3)
    assert np.isnan(rods[3])
    assert rods[4] == pytest.approx(spot_rows()["height number"][4])


def _export_with_instrument_ht(path, heights):
    import zipfile

    from synthetic import track_points

    tracks = track_points().head(400)
    tracks["Instrument Ht"] = heights
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{path.stem}_TRACK_POINTS.csv", tracks.to_csv(index=False))
    return path


def test_a_set_instrument_height_is_reported_and_not_applied(fresh, tmp_path):
    """SW Maps' Instrument Ht is never applied. If it was ever set, the
    heights would mix conventions with other exports unless it is said."""
    path = _export_with_instrument_ht(tmp_path / "Pole.zip", 1.8)
    report = fresh.load(path)
    text = "\n".join(report.notes)
    assert "Instrument Ht is set in Pole.zip" in text
    assert "1.8 m" in text and "not applied" in text
    assert "changes within a session" not in text
    # Not applied: the heights are exactly what the receiver reported.
    from gpsrtk.model.pointset import elevation_column
    ps = fresh.layers["track_points"]
    assert elevation_column(ps) == P.Z


def test_an_instrument_height_that_changes_mid_session_is_reported(fresh, tmp_path):
    import numpy as np

    path = _export_with_instrument_ht(tmp_path / "Mixed.zip",
                                      np.r_[np.zeros(200), np.full(200, 1.8)])
    text = "\n".join(fresh.load(path).notes)
    assert "changes within a session" in text
    assert "Mixed/2026-08-27" in text


def test_an_unset_instrument_height_says_nothing(fresh, synthetic_zip):
    assert not [n for n in fresh.load(synthetic_zip).notes if "Instrument" in n]


def test_opening_replaces_rather_than_merges(fresh, synthetic_zip, synthetic_outing2):
    fresh.load(synthetic_zip)
    n = len(fresh.layers["track_points"])
    fresh.load(synthetic_outing2)
    assert len(fresh.layers["track_points"]) == n
    assert len(fresh.sources) == 1


def test_a_merge_clears_a_stale_vertical_model(fresh, synthetic_zip, synthetic_outing2):
    """The model was solved for a set of sessions that no longer exists, so
    re-applying it would put the new points on a datum nothing measured."""
    fresh.load(synthetic_zip)
    fresh.solve_vertical("local")
    assert fresh.vertical is not None

    result = fresh.add_export(synthetic_outing2)
    assert fresh.vertical is None
    assert any("cleared" in n for n in result.notes)


def test_merged_sessions_can_be_solved(fresh, synthetic_zip, synthetic_outing2):
    """The point of merging: one datum across both outings."""
    fresh.load(synthetic_zip)
    fresh.add_export(synthetic_outing2)
    model = fresh.solve_vertical("ellipsoidal")

    assert model.sessions is not None
    assert len(model.sessions.offsets) == 2
    assert not model.sessions.unresolved
    # The second outing was written 5 cm high; the solve should say so.
    second = next(v for k, v in model.sessions.offsets.items()
                  if k.startswith("Outing 2"))
    assert second == pytest.approx(0.05, abs=0.01)


def test_an_outing_on_other_ground_cannot_be_reconciled(
        fresh, synthetic_zip, synthetic_outing2, synthetic_elsewhere):
    """The one outcome worth a warning at merge time: its offset is not
    determined by anything, and only a return visit recovers it."""
    fresh.load(synthetic_zip)
    fresh.add_export(synthetic_outing2)
    result = fresh.add_export(synthetic_elsewhere)
    assert not result.reconcilable
    assert [name.split("/")[0] for name in result.unlinked] == ["Elsewhere"]
    assert "CANNOT BE RECONCILED" in result.describe()


def test_a_merged_project_round_trips(fresh, synthetic_zip, synthetic_outing2, tmp_path):
    """A project referencing two exports used to load only the last one."""
    from gpsrtk.app import AppState
    from gpsrtk.site import example_site

    fresh.load(synthetic_zip)
    fresh.add_export(synthetic_outing2)
    total = len(fresh.layers["track_points"])
    saved = fresh.save_project(tmp_path / "merged.yardproj")

    other = AppState(site=example_site(), cache_dir=fresh.cache_dir)
    project, warnings = other.load_project(saved)
    assert len(project.sources) == 2
    assert not warnings, warnings
    assert len(other.layers["track_points"]) == total
    assert len(other.sources) == 2


def test_a_layer_spanning_sessions_says_so(fresh, synthetic_zip, synthetic_outing2):
    from gpsrtk.app import report

    fresh.load(synthetic_zip)
    fresh.add_export(synthetic_outing2)
    assert "2 sessions" in fresh.layers["track_points"].describe()
    assert "SPANS 2 SESSIONS" in report.vertical_text(fresh)


@pytest.mark.skipif(
    not (os.path.exists("archive/Project_1_-_2nd.swmz")),
    reason="real .swmz not present")
def test_the_two_real_formats_merge(fresh, export):
    """A CSV export and a .swmz of different outings, in one data set."""
    fresh.load(str(export.source_path))
    result = fresh.add_export("archive/Project_1_-_2nd.swmz")

    assert len(fresh.layers["track_points"]) == 8_194 + 35_317
    assert result.reconcilable
    assert sum(result.overlaps.values()) > 1_000
    assert result.reprojected == [], "both sit in UTM 15N already"

    model = fresh.solve_vertical("ellipsoidal")
    offsets = model.sessions.offsets
    assert len(offsets) == 3          # 08-22 spots, 08-27 walk, 09-23 walk
    assert not model.sessions.unresolved
