"""Project files and the basemap stack.

The property that matters most: reopening a project must reproduce the exact
elevations it was saved with. "Close enough" is not good enough for a datum -
a re-solve against slightly different inputs would drift silently.
"""

import json
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox      # noqa: E402

from gpsrtk.io.imagery import NoCoverageError, RasterLayer   # noqa: E402
from gpsrtk.model import pointset as P                       # noqa: E402
from gpsrtk.project import SUFFIX, BasemapState, Project     # noqa: E402
from gpsrtk.site import example_site                            # noqa: E402
from gpsrtk.surface import Extent                            # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class StubProvider:
    """A basemap source that never touches the network."""

    def __init__(self, name, *, outcome="ok", terrain=False):
        self.name = name
        self.description = "stub"
        self.attribution = "stub credit"
        self._outcome = outcome
        self.terrain = terrain

    def available(self):
        return True, "ok"

    def fetch(self, extent, epsg, size=1024):
        if self._outcome == "blank":
            raise NoCoverageError("nothing here")
        if self._outcome == "boom":
            raise RuntimeError("service exploded")
        rng = np.random.default_rng(abs(hash(self.name)) % 2**32)
        return RasterLayer(
            image=rng.integers(0, 255, (16, 16, 3), dtype=np.uint8),
            extent=extent, epsg=epsg, source=self.name,
            attribution=self.attribution)


@pytest.fixture
def window(app, export, monkeypatch, tmp_path):
    from gpsrtk.ui.main import MainWindow

    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name,
                            staticmethod(lambda *a, **k: QMessageBox.Ok))

    stubs = {
        "photo-a": (StubProvider("photo-a"), False),
        "photo-b": (StubProvider("photo-b"), False),
        "blank-one": (StubProvider("blank-one", outcome="blank"), False),
        "broken-one": (StubProvider("broken-one", outcome="boom"), False),
        "terrain-a": (StubProvider("terrain-a", terrain=True), True),
    }

    win = MainWindow()
    win.state.cache_dir = tmp_path / "cache"
    monkeypatch.setattr(win.state, "all_providers", lambda: stubs)
    win.state.load(str(export.source_path))
    yield win
    win.view3d.close_plotter()


def _path_exists(s):
    from pathlib import Path
    return Path(s).exists()


# --- the file format -----------------------------------------------------

def test_round_trips_through_json(tmp_path):
    p = Project(site=example_site(), sources=[], active_layer="track_points",
                chain=[{"kind": "fix_select", "enabled": True, "values": [4]}],
                vertical={"mode": "local", "datum_shift_m": -232.4},
                basemaps=[BasemapState("x", True, 0.4)],
                vectors=["Story County parcels"], view={"colormap": "viridis"})
    saved = p.save(tmp_path / "a.yardproj")
    back = Project.load(saved)

    assert back.site.origin_e == p.site.origin_e
    assert back.chain == p.chain
    assert back.vertical == p.vertical
    assert back.basemaps[0].opacity == 0.4
    assert back.basemaps[0].visible is True
    assert back.vectors == p.vectors
    assert back.view["colormap"] == "viridis"


def test_suffix_is_applied(tmp_path):
    saved = Project(site=example_site()).save(tmp_path / "noext")
    assert saved.suffix == SUFFIX


def test_sources_under_the_project_folder_are_stored_relative(tmp_path):
    """So a project folder can be moved or copied without breaking."""
    data = tmp_path / "data"
    data.mkdir()
    zip_path = data / "survey.zip"
    zip_path.write_bytes(b"x")

    saved = Project(site=example_site(), sources=[str(zip_path)]).save(
        tmp_path / "p.yardproj")
    raw = json.loads(saved.read_text())
    assert raw["sources"] == ["data/survey.zip"], "should be relative"

    back = Project.load(saved)
    assert _path_exists(back.sources[0])


def test_sources_outside_the_project_folder_stay_absolute(tmp_path):
    other = tmp_path.parent / "elsewhere.zip"
    other.write_bytes(b"x")
    saved = Project(site=example_site(), sources=[str(other)]).save(
        tmp_path / "p.yardproj")
    raw = json.loads(saved.read_text())
    assert raw["sources"][0].endswith("elsewhere.zip")
    assert "/" in raw["sources"][0] and raw["sources"][0] != "elsewhere.zip"
    other.unlink()


def test_a_newer_format_is_refused_with_a_clear_message(tmp_path):
    path = tmp_path / "future.yardproj"
    payload = Project(site=example_site()).to_dict()
    payload["version"] = 999
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="newer version"):
        Project.load(path)


def test_missing_sources_are_reported_not_raised(tmp_path):
    p = Project(site=example_site(), sources=[str(tmp_path / "gone.zip")])
    assert p.missing_sources()


# --- the basemap stack ---------------------------------------------------

def test_fetch_all_reports_each_outcome(window):
    report = window.state.fetch_all_imagery()
    assert set(report.fetched) == {"photo-a", "photo-b", "terrain-a"}
    assert report.no_coverage == ["blank-one"]
    assert [n for n, _ in report.failed] == ["broken-one"]
    # A source that can only ever show an empty tile gets no checkbox.
    assert "blank-one" not in window.state.basemaps


def test_only_the_first_photo_is_visible_after_fetching_all(window):
    """Otherwise the map shows whichever raster happened to arrive last."""
    window.state.fetch_all_imagery()
    visible = [b.provider for b in window.state.visible_basemaps]
    assert visible == ["photo-a"]


def test_terrain_rasters_are_flagged(window):
    window.state.fetch_all_imagery()
    assert window.state.basemaps["terrain-a"].terrain
    assert not window.state.basemaps["photo-a"].terrain


def test_visibility_and_opacity_are_independent(window):
    window.state.fetch_all_imagery()
    window.state.set_basemap_visible("terrain-a", True)
    window.state.set_basemap_opacity("terrain-a", 0.35)
    assert window.state.basemaps["terrain-a"].visible
    assert window.state.basemaps["terrain-a"].opacity == pytest.approx(0.35)
    assert len(window.state.visible_basemaps) == 2


def test_opacity_is_clamped(window):
    window.state.fetch_all_imagery()
    window.state.set_basemap_opacity("photo-a", 5.0)
    assert window.state.basemaps["photo-a"].opacity == 1.0
    window.state.set_basemap_opacity("photo-a", -2.0)
    assert window.state.basemaps["photo-a"].opacity == 0.0


def test_imagery_extent_does_not_move_when_filters_change(window):
    """The cache key is built from the extent. If filtering moved it, every
    threshold tweak would silently re-hit a public service."""
    before = window.state.data_extent()
    window.state.chain.stages[2].enabled = True
    window.state.chain.stages[2].minimum = 1.3
    window.state.recompute()
    after = window.state.data_extent()

    assert len(window.state.result) < len(window.state.source)
    assert (before.xmin, before.ymin, before.xmax, before.ymax) == \
           (after.xmin, after.ymin, after.xmax, after.ymax)


# --- end to end ----------------------------------------------------------

def test_reopening_reproduces_identical_elevations(window, tmp_path):
    st = window.state
    st.fetch_all_imagery()
    st.set_basemap_visible("terrain-a", True)
    st.set_basemap_opacity("terrain-a", 0.4)
    st.chain.stages[2].enabled = True
    st.recompute()
    st.solve_vertical("local")

    before = st.result.df[P.ELEV].to_numpy().copy()
    shift = st.vertical.datum_shift_m
    saved = st.save_project(tmp_path / "s.yardproj", window._view_state())

    from gpsrtk.ui.main import MainWindow
    other = MainWindow()
    other.state.cache_dir = st.cache_dir
    import unittest.mock as mock
    with mock.patch.object(type(other.state), "all_providers",
                           lambda self: st.all_providers()):
        project, warnings = other.state.load_project(saved)

    try:
        assert not warnings, warnings
        after = other.state.result.df[P.ELEV].to_numpy()
        assert np.array_equal(before, after), "elevations must be identical"
        assert other.state.vertical.datum_shift_m == shift
        assert other.state.chain.stages[2].enabled
        assert other.state.basemaps["terrain-a"].opacity == pytest.approx(0.4)
        assert {b.provider for b in other.state.visible_basemaps} == \
               {"photo-a", "terrain-a"}
        assert project.view["colormap"] == window.map2d.cmap.currentText()
    finally:
        other.view3d.close_plotter()


def test_saving_without_a_vertical_model_is_fine(window, tmp_path):
    window.state.fetch_all_imagery()
    saved = window.state.save_project(tmp_path / "novert.yardproj")
    assert json.loads(saved.read_text())["vertical"] is None


def test_panel_checkboxes_follow_state_changed_elsewhere(window):
    """Visibility set by fetch-all or by opening a project must show up in the
    panel. Without a sync the checkboxes silently disagree with the map."""
    panel = window.basemap_panel
    window.state.fetch_all_imagery()
    panel.refresh()

    assert panel._rows["photo-a"].check.isChecked()
    assert not panel._rows["terrain-a"].check.isChecked()

    window.state.set_basemap_visible("terrain-a", True)
    window.state.set_basemap_opacity("terrain-a", 0.25)
    panel.refresh()

    assert panel._rows["terrain-a"].check.isChecked()
    assert panel._rows["terrain-a"].slider.value() == 25
    assert panel._rows["terrain-a"].pct.text() == "25%"


def test_panel_sync_does_not_feed_back_into_state(window):
    """Syncing widgets must not re-emit and overwrite what it is displaying."""
    panel = window.basemap_panel
    window.state.fetch_all_imagery()
    window.state.set_basemap_opacity("photo-a", 0.33)
    panel.refresh()
    panel.refresh()
    assert window.state.basemaps["photo-a"].opacity == pytest.approx(0.33)


# --- the plan travels with the project -----------------------------------
#
# It used not to. Typing measured coordinates into the plan table and then
# saving the project wrote a file that silently did not contain them, and the
# work was gone as soon as the window closed. These tests exist so that cannot
# come back.

def _seeded_plan(state):
    from gpsrtk.plan import PlannedSetup

    plan = state.plan
    a = plan.add_point(449712.0, 4604565.0, purpose="building corner")
    a.observed_e, a.observed_n = 449712.4, 4604565.3
    a.method, a.fix = "rtk", 4
    a.rod_in = 41.5
    a.setup = "A"
    plan.add_point(449716.0, 4604569.0, purpose="building corner")
    plan.add_line("swale-1", [(449708.0, 4604561.0), (449712.0, 4604562.5)])
    plan.setups.append(PlannedSetup(name="A", e=449710.0, n=4604563.0))
    return plan, a


def _reopen(window, saved):
    """Open a saved project in a second window, with the same stub providers."""
    import unittest.mock as mock

    from gpsrtk.ui.main import MainWindow

    other = MainWindow()
    other.state.cache_dir = window.state.cache_dir
    with mock.patch.object(type(other.state), "all_providers",
                           lambda self: window.state.all_providers()):
        other.state.load_project(saved)
    return other


def test_the_plan_survives_save_and_reopen(window, tmp_path):
    plan, a = _seeded_plan(window.state)
    saved = window.state.save_project(tmp_path / "withplan.yardproj")

    other = _reopen(window, saved)
    try:
        back = other.state.plan
        assert [p.number for p in back.points] == [p.number for p in plan.points]
        one = back.by_number(a.number)
        assert one.observed_e == pytest.approx(a.observed_e)
        assert one.observed_n == pytest.approx(a.observed_n)
        assert one.rod_in == pytest.approx(41.5)
        assert one.purpose == "building corner" and one.setup == "A"
        assert [ln.line_id for ln in back.lines] == ["swale-1"]
        assert [s.name for s in back.setups] == ["A"]
    finally:
        other.view3d.close_plotter()


def test_a_measured_coordinate_actually_reaches_the_file(window, tmp_path):
    """The exact failure: the numbers typed in were not in the JSON."""
    _seeded_plan(window.state)
    saved = window.state.save_project(tmp_path / "coords.yardproj")
    raw = saved.read_text(encoding="utf-8")

    assert "plan" in json.loads(raw)
    assert "449712.4" in raw, "the measured easting is not in the saved file"


def test_reopening_restores_the_plan_layer_and_its_readings(window, tmp_path):
    """Rod readings feed the level network, so the plan has to be back in
    place before anything asks the surface what its datum is."""
    _seeded_plan(window.state)
    window.state.refresh_plan_layer()
    saved = window.state.save_project(tmp_path / "layer.yardproj")

    other = _reopen(window, saved)
    try:
        assert other.state.PLAN_LAYER in other.state.layers
        assert other.state.spots is not None
    finally:
        other.view3d.close_plotter()


def test_an_empty_plan_is_not_written(window, tmp_path):
    saved = window.state.save_project(tmp_path / "noplan.yardproj")
    assert json.loads(saved.read_text())["plan"] is None


def test_a_version_1_project_without_a_plan_still_opens(tmp_path):
    """Every project written before this existed is a version 1 file."""
    old = Project(site=example_site(), active_layer="track_points").to_dict()
    old["version"] = 1
    old.pop("plan")
    old.pop("imagery_offset")
    path = tmp_path / "old.yardproj"
    path.write_text(json.dumps(old), encoding="utf-8")

    back = Project.load(path)
    assert back.plan is None and back.imagery_offset is None


def test_resaving_stamps_the_current_format(tmp_path):
    """A v1 file reopened and saved now carries a plan; claiming to still be
    v1 would be a lie about what is in it."""
    from gpsrtk.project import VERSION

    saved = Project(site=example_site(), version=1).save(tmp_path / "bump.yardproj")
    assert json.loads(saved.read_text())["version"] == VERSION == 2


# --- imagery alignment ----------------------------------------------------

def _basemap_rect(window, name):
    item = window.map2d._basemap_items[name]
    return item.mapRectToParent(item.boundingRect())


def test_the_imagery_offset_round_trips(window, tmp_path):
    from gpsrtk.georef import ImageryOffset

    window.state.set_imagery_offset(ImageryOffset(de=0.30, dn=-0.15))
    saved = window.state.save_project(tmp_path / "off.yardproj")

    other = _reopen(window, saved)
    try:
        assert other.state.imagery_offset.de == pytest.approx(0.30)
        assert other.state.imagery_offset.dn == pytest.approx(-0.15)
    finally:
        other.view3d.close_plotter()


def test_no_offset_is_not_written(window, tmp_path):
    saved = window.state.save_project(tmp_path / "zero.yardproj")
    assert json.loads(saved.read_text())["imagery_offset"] is None


def test_the_offset_moves_the_basemap_and_nothing_else(window):
    """The measurements are the better-known thing. Shifting them to agree
    with an aerial photo would be backwards."""
    from gpsrtk.georef import ImageryOffset

    window.state.fetch_all_imagery()
    window.map2d.refresh()
    rect0 = _basemap_rect(window, "photo-a")
    points0 = window.map2d.scatter.getData()

    window.state.set_imagery_offset(ImageryOffset(de=0.30, dn=-0.15))
    window.map2d.refresh()
    rect1 = _basemap_rect(window, "photo-a")
    points1 = window.map2d.scatter.getData()

    assert rect1.x() - rect0.x() == pytest.approx(0.30, abs=1e-6)
    assert rect1.y() - rect0.y() == pytest.approx(-0.15, abs=1e-6)
    assert np.array_equal(points0[0], points1[0])
    assert np.array_equal(points0[1], points1[1])


def test_a_shifted_basemap_says_so_on_the_view(window):
    """A silently moved photo is the kind of thing that gets trusted later."""
    from gpsrtk.georef import ImageryOffset

    window.state.fetch_all_imagery()
    window.state.set_imagery_offset(ImageryOffset(de=0.3048, dn=0.0))
    window.map2d.refresh()
    assert "imagery shifted 1.00 ft" in window.map2d.info.text()


def test_solving_from_the_panel_applies_and_reports(window, monkeypatch):
    shown = []
    monkeypatch.setattr(
        QMessageBox, "information",
        staticmethod(lambda *a, **k: (shown.append(a), QMessageBox.Ok)[1]))

    plan = window.state.plan
    for i in range(3):
        p = plan.add_point(449710.0 + 5 * i, 4604563.0,
                           purpose="building corner")
        p.observed_e, p.observed_n = p.planned_e + 0.30, p.planned_n - 0.15
        p.method, p.fix = "rtk", 4
    window._on_plan_changed()
    window.solve_imagery_offset()

    assert window.state.imagery_offset.de == pytest.approx(0.30)
    assert "imagery moved" in shown[-1][2].lower()
    # 0.30 m is 0.98 ft; the box shows feet to two places.
    assert window.basemap_panel.off_e.value() == pytest.approx(0.98, abs=0.005)


def test_solving_with_nothing_to_solve_from_explains_itself(window, monkeypatch):
    shown = []
    monkeypatch.setattr(
        QMessageBox, "information",
        staticmethod(lambda *a, **k: (shown.append(a), QMessageBox.Ok)[1]))
    window.solve_imagery_offset()

    assert window.state.imagery_offset.zero
    assert "see in the photo" in shown[-1][2]
