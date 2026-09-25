"""Project files and the basemap stack.

The property that matters most: reopening a project must reproduce the exact
elevations it was saved with. "Close enough" is not good enough for a datum -
a re-solve against slightly different inputs would drift silently.
"""

import json
import unittest.mock as mock

import numpy as np
import pytest

from gpsrtk.app import AppState, report, views
from gpsrtk.io.imagery import NoCoverageError, RasterLayer
from gpsrtk.model import pointset as P
from gpsrtk.project import SUFFIX, BasemapState, Project
from gpsrtk.site import example_site


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


STUBS = {
    "photo-a": (StubProvider("photo-a"), False),
    "photo-b": (StubProvider("photo-b"), False),
    "blank-one": (StubProvider("blank-one", outcome="blank"), False),
    "broken-one": (StubProvider("broken-one", outcome="boom"), False),
    "terrain-a": (StubProvider("terrain-a", terrain=True), True),
}


@pytest.fixture
def stubbed(state, monkeypatch):
    """The loaded synthetic export, with imagery that never leaves the machine."""
    monkeypatch.setattr(state, "all_providers", lambda: STUBS)
    return state


def _reopen(state, saved):
    """Open a saved project in a second state, with the same stub providers."""
    other = AppState(site=example_site(), cache_dir=state.cache_dir)
    with mock.patch.object(AppState, "all_providers", lambda self: STUBS):
        project, warnings = other.load_project(saved)
    return other, project, warnings


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
    from pathlib import Path
    assert Path(back.sources[0]).exists()


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

def test_fetch_all_reports_each_outcome(stubbed):
    result = stubbed.fetch_all_imagery()
    assert set(result.fetched) == {"photo-a", "photo-b", "terrain-a"}
    assert result.no_coverage == ["blank-one"]
    assert [n for n, _ in result.failed] == ["broken-one"]
    # A source that can only ever show an empty tile gets no checkbox.
    assert "blank-one" not in stubbed.basemaps


def test_only_the_first_photo_is_visible_after_fetching_all(stubbed):
    """Otherwise the map shows whichever raster happened to arrive last."""
    stubbed.fetch_all_imagery()
    visible = [b.provider for b in stubbed.visible_basemaps]
    assert visible == ["photo-a"]


def test_terrain_rasters_are_flagged(stubbed):
    stubbed.fetch_all_imagery()
    assert stubbed.basemaps["terrain-a"].terrain
    assert not stubbed.basemaps["photo-a"].terrain


def test_visibility_and_opacity_are_independent(stubbed):
    stubbed.fetch_all_imagery()
    stubbed.set_basemap_visible("terrain-a", True)
    stubbed.set_basemap_opacity("terrain-a", 0.35)
    assert stubbed.basemaps["terrain-a"].visible
    assert stubbed.basemaps["terrain-a"].opacity == pytest.approx(0.35)
    assert len(stubbed.visible_basemaps) == 2


def test_opacity_is_clamped(stubbed):
    stubbed.fetch_all_imagery()
    stubbed.set_basemap_opacity("photo-a", 5.0)
    assert stubbed.basemaps["photo-a"].opacity == 1.0
    stubbed.set_basemap_opacity("photo-a", -2.0)
    assert stubbed.basemaps["photo-a"].opacity == 0.0


def test_the_basemap_list_reports_changes_made_elsewhere(stubbed):
    """Visibility set by fetch-all or by opening a project must reach the
    panel. Without that the checkboxes silently disagree with the map."""
    stubbed.fetch_all_imagery()
    shown = {b["name"]: b for b in views.basemap_payload(stubbed)}
    assert shown["photo-a"]["visible"] and not shown["terrain-a"]["visible"]

    stubbed.set_basemap_visible("terrain-a", True)
    stubbed.set_basemap_opacity("terrain-a", 0.25)
    shown = {b["name"]: b for b in views.basemap_payload(stubbed)}
    assert shown["terrain-a"]["visible"]
    assert shown["terrain-a"]["opacity"] == pytest.approx(0.25)


def test_basemaps_are_listed_photos_first(stubbed):
    """Photographs underneath LiDAR products: a hillshade at partial opacity
    over an aerial reads well, and the reverse does not."""
    stubbed.fetch_all_imagery()
    names = [b["name"] for b in views.basemap_payload(stubbed)]
    assert names.index("terrain-a") > names.index("photo-a")


def test_imagery_extent_does_not_move_when_filters_change(stubbed):
    """The cache key is built from the extent. If filtering moved it, every
    threshold tweak would silently re-hit a public service."""
    before = stubbed.data_extent()
    stubbed.chain.stages[2].enabled = True
    stubbed.chain.stages[2].minimum = 0.9
    stubbed.recompute()
    after = stubbed.data_extent()

    assert len(stubbed.result) < len(stubbed.source)
    assert (before.xmin, before.ymin, before.xmax, before.ymax) == \
           (after.xmin, after.ymin, after.xmax, after.ymax)


# --- end to end ----------------------------------------------------------

def test_reopening_reproduces_identical_elevations(stubbed, tmp_path):
    st = stubbed
    st.fetch_all_imagery()
    st.set_basemap_visible("terrain-a", True)
    st.set_basemap_opacity("terrain-a", 0.4)
    st.chain.stages[2].enabled = True
    st.recompute()
    st.solve_vertical("local")

    before = st.result.df[P.ELEV].to_numpy().copy()
    shift = st.vertical.datum_shift_m
    saved = st.save_project(tmp_path / "s.yardproj",
                            {"colormap": "viridis", "tab": 1})

    other, project, warnings = _reopen(st, saved)
    assert not warnings, warnings
    after = other.result.df[P.ELEV].to_numpy()
    assert np.array_equal(before, after), "elevations must be identical"
    assert other.vertical.datum_shift_m == shift
    assert other.chain.stages[2].enabled
    assert other.basemaps["terrain-a"].opacity == pytest.approx(0.4)
    assert {b.provider for b in other.visible_basemaps} == \
           {"photo-a", "terrain-a"}
    # The view comes back the way it was left, with the session comparison.
    assert project.view == {"colormap": "viridis", "tab": 1, "hidden_sessions": [],
                            "surface_from_shown_sessions": False}
    assert other.view == project.view


def test_the_first_plan_edit_after_reopening_keeps_the_stored_model(stubbed, tmp_path):
    """Reopening applies the stored vertical model rather than re-solving it.
    A plan edit that the level network cannot see must not re-solve it
    either, or the exact elevations are gone at the first click."""
    stubbed.solve_vertical("local")
    saved = stubbed.save_project(tmp_path / "keep.yardproj")

    other, _, _ = _reopen(stubbed, saved)
    stored = other.vertical
    other.plan.add_point(449712.0, 4604565.0)
    other.plan_changed()
    assert other.vertical is stored


def test_saving_without_a_vertical_model_is_fine(stubbed, tmp_path):
    stubbed.fetch_all_imagery()
    saved = stubbed.save_project(tmp_path / "novert.yardproj")
    assert json.loads(saved.read_text())["vertical"] is None


def test_saving_names_the_project(stubbed, tmp_path):
    assert stubbed.title == "Yard Survey"
    stubbed.save_project(tmp_path / "named.yardproj")
    assert stubbed.title == "Yard Survey — named.yardproj"


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


def test_the_plan_survives_save_and_reopen(stubbed, tmp_path):
    plan, a = _seeded_plan(stubbed)
    saved = stubbed.save_project(tmp_path / "withplan.yardproj")

    other, _, _ = _reopen(stubbed, saved)
    back = other.plan
    assert [p.number for p in back.points] == [p.number for p in plan.points]
    one = back.by_number(a.number)
    assert one.observed_e == pytest.approx(a.observed_e)
    assert one.observed_n == pytest.approx(a.observed_n)
    assert one.rod_in == pytest.approx(41.5)
    assert one.purpose == "building corner" and one.setup == "A"
    assert [ln.line_id for ln in back.lines] == ["swale-1"]
    assert [s.name for s in back.setups] == ["A"]


def test_a_measured_coordinate_actually_reaches_the_file(stubbed, tmp_path):
    """The exact failure: the numbers typed in were not in the JSON."""
    _seeded_plan(stubbed)
    saved = stubbed.save_project(tmp_path / "coords.yardproj")
    raw = saved.read_text(encoding="utf-8")

    assert "plan" in json.loads(raw)
    assert "449712.4" in raw, "the measured easting is not in the saved file"


def test_reopening_restores_the_plan_layer_and_its_readings(stubbed, tmp_path):
    """Rod readings feed the level network, so the plan has to be back in
    place before anything asks the surface what its datum is."""
    _seeded_plan(stubbed)
    stubbed.refresh_plan_layer()
    saved = stubbed.save_project(tmp_path / "layer.yardproj")

    other, _, _ = _reopen(stubbed, saved)
    assert other.PLAN_LAYER in other.layers
    assert other.spots is not None


def test_an_empty_plan_is_not_written(stubbed, tmp_path):
    saved = stubbed.save_project(tmp_path / "noplan.yardproj")
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
    assert json.loads(saved.read_text())["version"] == VERSION == 3


# --- imagery alignment ----------------------------------------------------

def test_the_imagery_offset_round_trips(stubbed, tmp_path):
    from gpsrtk.georef import ImageryOffset

    stubbed.set_imagery_offset(ImageryOffset(de=0.30, dn=-0.15))
    saved = stubbed.save_project(tmp_path / "off.yardproj")

    other, _, _ = _reopen(stubbed, saved)
    assert other.imagery_offset.de == pytest.approx(0.30)
    assert other.imagery_offset.dn == pytest.approx(-0.15)


def test_no_offset_is_not_written(stubbed, tmp_path):
    saved = stubbed.save_project(tmp_path / "zero.yardproj")
    assert json.loads(saved.read_text())["imagery_offset"] is None


def test_the_offset_moves_the_basemap_and_nothing_else(stubbed):
    """The measurements are the better-known thing. Shifting them to agree
    with an aerial photo would be backwards."""
    from gpsrtk.georef import ImageryOffset

    stubbed.fetch_all_imagery()
    extent0 = {b["name"]: b["extent"] for b in views.basemap_payload(stubbed)}
    points0 = views.unpack_points(views.pack_points(stubbed))

    stubbed.set_imagery_offset(ImageryOffset(de=0.30, dn=-0.15))
    extent1 = {b["name"]: b["extent"] for b in views.basemap_payload(stubbed)}
    points1 = views.unpack_points(views.pack_points(stubbed))

    x0, y0, x1, y1 = extent0["photo-a"]
    assert extent1["photo-a"] == pytest.approx([x0 + 0.30, y0 - 0.15,
                                                x1 + 0.30, y1 - 0.15], abs=1e-6)
    assert np.array_equal(points0["x"], points1["x"])
    assert np.array_equal(points0["y"], points1["y"])


def test_a_shifted_basemap_says_so_on_the_view(stubbed):
    """A silently moved photo is the kind of thing that gets trusted later."""
    from gpsrtk.georef import ImageryOffset

    stubbed.fetch_all_imagery()
    stubbed.set_imagery_offset(ImageryOffset(de=0.3048, dn=0.0))
    assert "imagery shifted 1.00 ft" in report.info_bits(stubbed)


def test_a_hand_typed_shift_carries_no_statistics(stubbed):
    stubbed.set_manual_imagery_offset(1.0, -0.5)
    off = stubbed.imagery_offset
    assert off.de == pytest.approx(0.3048)
    assert off.dn == pytest.approx(-0.1524)
    assert off.n == 0 and "by hand" in off.describe()


# --- source fingerprints ------------------------------------------------------

def test_a_renamed_copy_is_refused_as_already_loaded(synthetic_zip, tmp_path):
    """Duplicates were detected by path only, so a renamed copy merged as
    new data and doubled every point."""
    import shutil

    st = AppState(site=example_site(), cache_dir=tmp_path / "cache")
    st.load(synthetic_zip)
    copy = tmp_path / "Synthetic Yard (1).zip"
    shutil.copy(synthetic_zip, copy)
    before = len(st.layers["track_points"])
    with pytest.raises(ValueError, match="same contents as Synthetic Yard.zip"):
        st.add_export(copy)
    assert len(st.layers["track_points"]) == before


def test_fingerprints_are_saved_with_the_project(stubbed, tmp_path):
    saved = stubbed.save_project(tmp_path / "fp.yardproj")
    stored = json.loads(saved.read_text("utf-8"))["source_fingerprints"]
    (entry,) = stored.values()
    assert entry["size"] == stubbed.sources[0].stat().st_size
    assert len(entry["sha256"]) == 64


def test_a_source_changed_since_saving_is_warned_about(synthetic_zip, tmp_path):
    """The stored offsets are re-applied, not re-solved; a file re-exported
    under the same name would receive them silently."""
    import shutil

    from synthetic import write_export

    source = tmp_path / "Synthetic Yard.zip"
    shutil.copy(synthetic_zip, source)
    st = AppState(site=example_site(), cache_dir=tmp_path / "cache")
    st.load(source)
    st.solve_vertical("local")
    saved = st.save_project(tmp_path / "changed.yardproj")

    write_export(source, dz=0.02, seed=5)             # re-exported, same name
    _, _, warnings = _reopen(st, saved)
    assert any("has changed since the project was saved" in w for w in warnings)


def test_an_unchanged_source_is_not_warned_about(stubbed, tmp_path):
    saved = stubbed.save_project(tmp_path / "same.yardproj")
    _, _, warnings = _reopen(stubbed, saved)
    assert not warnings


def test_a_project_without_fingerprints_still_opens(stubbed, tmp_path):
    saved = stubbed.save_project(tmp_path / "old.yardproj")
    d = json.loads(saved.read_text("utf-8"))
    del d["source_fingerprints"]
    saved.write_text(json.dumps(d), "utf-8")
    other, project, warnings = _reopen(stubbed, saved)
    assert project.source_fingerprints == {} and not warnings
    assert other.fingerprints


class _DeadProxy(StubProvider):
    """Fails the way every provider does behind a dead proxy."""

    def fetch(self, extent, epsg, size=1024):
        raise ConnectionError(
            f"HTTPSConnectionPool(host='{self.name}.example.org', port=443): Max "
            f"retries exceeded with url: /arcgis/rest/services/{self.name}/export "
            "(Caused by ProxyError('Unable to connect to proxy', "
            "OSError('Tunnel connection failed: 403 Forbidden')))")


def test_identical_failures_are_grouped(state, monkeypatch):
    """Ten providers behind one dead proxy gave ten near-identical errors,
    each cut off mid-word. Now they are one line naming them all."""
    dead = {f"dead-{k}": (_DeadProxy(f"dead-{k}"), False) for k in range(4)}
    monkeypatch.setattr(state, "all_providers",
                        lambda: {**dead, "broken-one": STUBS["broken-one"]})
    report = state.fetch_all_imagery()
    groups = report.failure_groups()
    assert len(groups) == 2
    assert sorted(groups[0][1]) == ["dead-0", "dead-1", "dead-2", "dead-3"]
    text = report.describe()
    assert "(4 sources)" in text and "dead-0, dead-1, dead-2, dead-3" in text
    assert "Tunnel connection failed: 403 Forbidden" in text, "not cut mid-word"
    assert text.count("ProxyError") == 1


def test_a_black_holed_service_fails_in_seconds_not_minutes():
    from gpsrtk.io import imagery, vector

    assert imagery.TIMEOUT[0] == vector.TIMEOUT[0] == 10.0
    assert imagery.TIMEOUT[1] == 60.0


# --- atomic saves ---------------------------------------------------------------

def test_a_save_that_fails_leaves_the_old_file_whole(stubbed, tmp_path, monkeypatch):
    """Written in place, an interrupted save left a truncated project - the
    only record of the solved datum. Now it is the old file or the new one."""
    import os

    saved = stubbed.save_project(tmp_path / "keep.yardproj")
    before = saved.read_bytes()

    def refuse(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", refuse)
    stubbed.plan.add_point(449712.0, 4604565.0)
    with pytest.raises(OSError, match="disk full"):
        stubbed.save_project(saved)
    assert saved.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["keep.yardproj"], "no temp left"


def test_a_save_retries_once_past_a_sync_lock(tmp_path, monkeypatch):
    """OneDrive locks a file for a moment while it syncs it."""
    import os

    from gpsrtk import fileio
    from gpsrtk.plan import Plan
    from gpsrtk.site import Site

    real, calls = os.replace, []

    def locked_once(src, dst):
        calls.append(dst)
        if len(calls) == 1:
            raise PermissionError("in use")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", locked_once)
    monkeypatch.setattr(fileio, "RETRY_AFTER_S", 0.0)
    plan = Plan()
    plan.add_point(449712.0, 4604565.0)
    path = plan.save(tmp_path / "shots")
    assert Plan.load(path).points[0].number == 1 and len(calls) == 2

    calls.clear()
    example_site().save(tmp_path / "site.json")
    assert Site.load(tmp_path / "site.json").name == "example site"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["shots.yardplan", "site.json"]
