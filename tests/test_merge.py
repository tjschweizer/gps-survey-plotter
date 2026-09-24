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

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox      # noqa: E402

from gpsrtk import merge as M                                # noqa: E402
from gpsrtk.model import pointset as P                       # noqa: E402
from gpsrtk.model.pointset import PointSet                   # noqa: E402

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

@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, export, monkeypatch, tmp_path):
    from gpsrtk.ui.main import MainWindow

    shown = []
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(
            QMessageBox, name,
            staticmethod(lambda *a, **k: (shown.append(a), QMessageBox.Ok)[1]))
    w = MainWindow()
    w.state.cache_dir = tmp_path / "cache"
    w.dialogs = shown
    yield w
    w.view3d.close_plotter()


def _second_export(tmp_path, export, shift_days=30):
    """A copy of the reference export dated a month later.

    Built from the real file so the merge is exercised against real geometry
    rather than a synthetic pass that happens to overlap itself.
    """
    import shutil
    import zipfile

    src = export.source_path
    out = tmp_path / "Outing 2.zip"
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(out, "w") as zout:
        for name in zin.namelist():
            data = zin.read(name).decode("utf-8", "replace")
            if name.endswith("TRACK_POINTS.csv"):
                lines = data.splitlines()
                head = lines[0].split(",")
                col = head.index("Time")
                rows = [lines[0]]
                for line in lines[1:]:
                    bits = line.split(",")
                    if len(bits) > col:
                        stamp = pd.to_datetime(
                            bits[col].rsplit(" ", 1)[0],
                            format="%m/%d/%Y %H:%M:%S.%f", errors="coerce")
                        if pd.notna(stamp):
                            stamp += pd.Timedelta(days=shift_days)
                            bits[col] = (stamp.strftime("%m/%d/%Y %H:%M:%S.%f")[:-3]
                                         + " CDT")
                    rows.append(",".join(bits))
                data = "\n".join(rows)
            zout.writestr(name.replace("Project 1", "Outing 2"), data)
    shutil.copystat(src, out)
    return out


def test_adding_an_export_keeps_the_first(win, export, tmp_path):
    win.state.load(str(export.source_path))
    before = len(win.state.layers["track_points"])

    report = win.state.add_export(str(_second_export(tmp_path, export)))

    assert len(win.state.layers["track_points"]) == before * 2
    # `added` counts every layer the merge touched, not just the track.
    assert report.layers["track_points"] == (before, before * 2)
    assert report.added > before
    assert len(win.state.sources) == 2
    assert len(report.sessions) >= 2


def test_the_same_file_twice_is_refused(win, export):
    """Loading it again would double every point and make the duplicates look
    like a perfect crossover."""
    win.state.load(str(export.source_path))
    with pytest.raises(ValueError, match="already loaded"):
        win.state.add_export(str(export.source_path))


def test_opening_replaces_rather_than_merges(win, export, tmp_path):
    win.state.load(str(export.source_path))
    n = len(win.state.layers["track_points"])
    win.state.load(str(_second_export(tmp_path, export)))
    assert len(win.state.layers["track_points"]) == n
    assert len(win.state.sources) == 1


def test_a_merge_clears_a_stale_vertical_model(win, export, tmp_path):
    """The model was solved for a set of sessions that no longer exists, so
    re-applying it would put the new points on a datum nothing measured."""
    win.state.load(str(export.source_path))
    win.state.solve_vertical("local")
    assert win.state.vertical is not None

    report = win.state.add_export(str(_second_export(tmp_path, export)))
    assert win.state.vertical is None
    assert any("cleared" in n for n in report.notes)


def test_merged_sessions_can_be_solved(win, export, tmp_path):
    """The point of merging: one datum across both outings."""
    win.state.load(str(export.source_path))
    win.state.add_export(str(_second_export(tmp_path, export)))
    model = win.state.solve_vertical("ellipsoidal")

    assert model.sessions is not None
    assert len(model.sessions.offsets) >= 2
    assert not model.sessions.unresolved


def test_a_merged_project_round_trips(win, export, tmp_path):
    """A project referencing two exports used to load only the last one."""
    win.state.load(str(export.source_path))
    win.state.add_export(str(_second_export(tmp_path, export)))
    total = len(win.state.layers["track_points"])
    saved = win.state.save_project(tmp_path / "merged.yardproj")

    from gpsrtk.ui.main import MainWindow
    other = MainWindow()
    other.state.cache_dir = win.state.cache_dir
    try:
        project, warnings = other.state.load_project(saved)
        assert len(project.sources) == 2
        assert not warnings, warnings
        assert len(other.state.layers["track_points"]) == total
        assert len(other.state.sources) == 2
    finally:
        other.view3d.close_plotter()


def test_a_layer_spanning_sessions_says_so(win, export, tmp_path):
    win.state.load(str(export.source_path))
    win.state.add_export(str(_second_export(tmp_path, export)))
    assert "2 sessions" in win.state.layers["track_points"].describe()

    win.vertical_panel.refresh()
    assert "SPANS 2 SESSIONS" in win.vertical_panel.body.text()


@pytest.mark.skipif(
    not (os.path.exists("archive/Project_1_-_2nd.swmz")),
    reason="real .swmz not present")
def test_the_two_real_formats_merge(win, export):
    """A CSV export and a .swmz of different outings, in one data set."""
    win.state.load(str(export.source_path))
    report = win.state.add_export("archive/Project_1_-_2nd.swmz")

    assert len(win.state.layers["track_points"]) == 8_194 + 35_317
    assert report.reconcilable
    assert sum(report.overlaps.values()) > 1_000
    assert report.reprojected == [], "both sit in UTM 15N already"

    model = win.state.solve_vertical("ellipsoidal")
    offsets = model.sessions.offsets
    assert len(offsets) == 3          # 08-22 spots, 08-27 walk, 09-23 walk
    assert not model.sessions.unresolved
