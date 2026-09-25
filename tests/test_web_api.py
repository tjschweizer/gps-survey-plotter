"""The HTTP layer: what the browser can ask for, and what it gets back.

The rules behind each action are tested where they live (`test_plan_edit`,
`test_project`, `test_merge`). These pin the contract around them: that every
action replies with a snapshot and says what happened, that failures come
back as dialogs rather than crashes, that the busy state always clears, and
that nothing outside this machine's own page can drive the server.
"""

import io
import zipfile

import numpy as np
import pytest
from fastapi.testclient import TestClient

from gpsrtk.app import views
from gpsrtk.io.imagery import NoCoverageError, RasterLayer
from gpsrtk.web import create_app

E0, N0 = 449712.0, 4604565.0
ACTION = {"X-Yard-Survey": "1"}


class StubImagery:
    """Stands in for a web service, with controllable behaviour."""

    description = "stub"
    attribution = "stub credit"

    def __init__(self, name="stub", *, reachable=True, outcome="ok"):
        self.name = name
        self._reachable = reachable
        self._outcome = outcome

    def available(self):
        return (True, "ok") if self._reachable else (False, "unreachable")

    def fetch(self, extent, epsg, size=1024):
        if self._outcome == "blank":
            raise NoCoverageError("no coverage for this extent")
        if self._outcome == "boom":
            raise RuntimeError("service exploded")
        rng = np.random.default_rng(0)
        return RasterLayer(image=rng.integers(0, 255, (32, 32, 3), dtype=np.uint8),
                           extent=extent, epsg=epsg, source=self.name,
                           attribution=self.attribution)


class StubLinework:
    name = "stubvec"
    description = "stub linework"
    survey_grade = False

    def available(self):
        return True, "ok"

    def fetch(self, extent, epsg):
        from gpsrtk.io.vector import VectorLayer

        ring = np.array([[extent.xmin, extent.ymin], [extent.xmax, extent.ymax]])
        return VectorLayer(rings=[ring], epsg=epsg, source="stubvec")


@pytest.fixture
def app(state):
    return create_app(state, vector_providers={"stubvec": StubLinework()})


@pytest.fixture
def client(app):
    return TestClient(app, base_url="http://127.0.0.1", headers=ACTION)


@pytest.fixture
def server(app):
    return app.state.server


def post(client, url, body=None, status=200):
    r = client.post(url, json=body or {})
    assert r.status_code == status, r.text
    return r.json()


def with_imagery(state, monkeypatch, **providers):
    monkeypatch.setattr(state, "all_providers",
                        lambda: {n: (p, False) for n, p in providers.items()})


# --- who may talk to it ------------------------------------------------------------

def test_actions_need_the_page_header(app):
    """A custom header cannot be sent cross-site without a CORS preflight,
    which this server never grants - so another site cannot drive it."""
    bare = TestClient(app, base_url="http://127.0.0.1")
    r = bare.post("/api/vertical/clear", json={})
    assert r.status_code == 403
    assert r.json()["error"]["title"] == "Refused"


def test_actions_from_another_origin_are_refused(client):
    r = client.post("/api/vertical/clear", json={},
                    headers={"Origin": "https://example.com"})
    assert r.status_code == 403


def test_actions_from_its_own_page_are_accepted(client):
    r = client.post("/api/vertical/clear", json={},
                    headers={"Origin": "http://127.0.0.1:8765"})
    assert r.status_code == 200


def test_a_foreign_host_name_is_refused(app):
    """A page cannot reach this server by pointing a hostname of its own at
    127.0.0.1 and then calling it same-origin."""
    r = TestClient(app, base_url="http://attacker.example").get("/api/state")
    assert r.status_code == 400


def test_reading_needs_no_header(app):
    r = TestClient(app, base_url="http://127.0.0.1").get("/api/state")
    assert r.status_code == 200


# --- the snapshot ------------------------------------------------------------------

def test_the_snapshot_describes_the_loaded_data(client):
    s = client.get("/api/state").json()
    assert s["has_data"]
    assert [ly["name"] for ly in s["layers"] if ly["active"]] == ["track_points"]
    assert s["chain"]["total"].endswith("kept)")
    assert "crossover residuals" in s["qc"]
    assert s["vertical"]["solved"] is False
    assert "RAW ELLIPSOIDAL" in s["vertical"]["text"]
    assert s["surface"]["measured"] > 0.5
    assert len(s["home"]) == 4
    assert s["title"] == "Yard Survey"


def test_every_action_replies_with_the_moved_revisions(client):
    before = client.get("/api/state").json()["rev"]
    reply = post(client, "/api/layers/visible", {"name": "spots", "visible": False})
    after = reply["state"]["rev"]
    assert after["selection"] == before["selection"] + 1
    assert after["result"] == before["result"]
    assert not next(ly for ly in reply["state"]["layers"] if ly["name"] == "spots")["visible"]


def test_switching_the_active_layer_rebuilds_from_it(client):
    s = post(client, "/api/layers/active", {"name": "spots"})["state"]
    assert [ly["name"] for ly in s["layers"] if ly["active"]] == ["spots"]
    assert s["chain"]["stages"][0]["counts"].startswith("14 →")


# --- heavy resources ----------------------------------------------------------------

def test_points_come_as_one_binary_buffer(client, state):
    r = client.get("/api/points", params={"color_by": "fix quality"})
    assert r.headers["content-type"] == "application/octet-stream"
    points = views.unpack_points(r.content)
    assert points["total"] == len(state.result) == len(points["x"])
    # Fixed only by default, so every point is the fixed green.
    assert (points["rgba"][:, :3] == (60, 200, 90)).all()
    x, y = state.site.to_local(state.result.df["e"].to_numpy(),
                               state.result.df["n"].to_numpy())
    assert points["x"] == pytest.approx(x, abs=1e-3)
    assert points["y"] == pytest.approx(y, abs=1e-3)


def test_points_are_subsampled_above_the_cap(client, state, monkeypatch):
    monkeypatch.setattr(views, "MAX_SCATTER", 1000)
    points = views.unpack_points(client.get("/api/points").content)
    assert points["total"] == len(state.result)
    assert len(points["x"]) == 1000
    assert f"drawing {1000:,}" in client.get("/api/state").json()["points_note"]


def test_every_colouring_has_a_colour_per_point(client, state):
    n = len(state.result)
    for colour in ("elevation", "fix quality", "speed", "session"):
        points = views.unpack_points(
            client.get("/api/points", params={"color_by": colour, "cmap": "viridis"}).content)
        assert points["rgba"].shape == (n, 4), colour


def test_the_surface_image_is_north_up(state):
    """Row 0 of the grid is the south edge; row 0 of an image is the top.
    Getting the flip wrong mirrors the lot north to south, silently."""
    from PIL import Image

    from gpsrtk.surface import Extent, Surface

    z = np.tile(np.linspace(0.0, 1.0, 40)[:, None], (1, 40))    # rises northward
    surface = Surface(z=z, mask=np.zeros_like(z, bool),
                      extent=Extent(0, 40, 0, 40), px=1.0, n_points=1, n_cells=1)
    img = np.asarray(Image.open(io.BytesIO(views.surface_png(surface, "gray"))))
    assert img[:5, :, :3].mean() > img[-5:, :, :3].mean() + 100


def test_unmeasured_cells_are_transparent(state):
    from PIL import Image

    s = state.surface
    img = np.asarray(Image.open(io.BytesIO(views.surface_png(s))))
    assert ((img[..., 3] == 0) == np.flipud(s.mask)).all()


def test_the_3d_grid_leaves_unmeasured_cells_empty(client, state):
    g = client.get("/api/surface/grid").json()
    s = state.surface
    assert len(g["y"]) == len(g["z"]) == s.z.shape[0]
    assert len(g["x"]) == len(g["z"][0]) == s.z.shape[1]
    nulls = sum(v is None for row in g["z"] for v in row)
    assert nulls == int(s.mask.sum())
    assert g["colorscale"][0][0] == 0.0 and g["colorscale"][-1][0] == 1.0


def test_features_carry_spot_markers_once_each(client):
    """FEATURE_POINTS repeats every spot; drawing both would double them."""
    markers = client.get("/api/features").json()["markers"]
    assert len(markers) == 12          # 14 shots of 12 distinct points


def test_spot_markers_say_what_they_are(client, state):
    """The datum dialog asks for an ID; the map's squares used to carry none."""
    markers = client.get("/api/features").json()["markers"]
    one = next(m for m in markers if m["station"] == "1")
    spots = state.layers["spots"].df
    assert one["kind"] == "lawn" and one["date"] == "2026-08-27"
    assert one["rod"] == pytest.approx(spots.loc[spots["point_id"] == 1, "rod_in"].iloc[0])
    assert {m["station"] for m in markers} == {str(k) for k in range(1, 13)}


def test_the_datum_picker_describes_each_station(client):
    choices = client.get("/api/datum").json()["choices"]
    assert [c["point"] for c in choices] == list(range(1, 13))
    seven = next(c for c in choices if c["point"] == 7)
    assert seven["label"] == "lawn · 2 readings · 2026-08-27"
    assert next(c for c in choices if c["point"] == 1)["label"].startswith("lawn · rod ")


# --- the busy state always clears ------------------------------------------------------
#
# The desktop app's wait cursor was a stack, and an unbalanced push left an
# hourglass on screen for good. The browser shows a busy overlay while an
# action holds the state; these check it is released however the action ends.

def _idle(client):
    return client.get("/api/progress").json()["busy"] is False


@pytest.mark.parametrize("outcome,status", [("ok", 200), ("blank", 400), ("boom", 400)])
def test_busy_clears_after_fetching_imagery(client, state, monkeypatch, outcome, status):
    with_imagery(state, monkeypatch, stub=StubImagery(outcome=outcome))
    r = client.post("/api/imagery/fetch", json={"name": "stub"})
    assert r.status_code == status
    assert _idle(client)


def test_a_blank_tile_is_reported_as_no_coverage(client, state, monkeypatch):
    with_imagery(state, monkeypatch, stub=StubImagery(outcome="blank"))
    error = post(client, "/api/imagery/fetch", {"name": "stub"}, 400)["error"]
    assert error["title"] == "No coverage here" and error["level"] == "warning"


def test_busy_clears_when_the_provider_is_unreachable(client, state, monkeypatch):
    with_imagery(state, monkeypatch, stub=StubImagery(reachable=False))
    error = post(client, "/api/imagery/fetch", {"name": "stub"}, 400)["error"]
    assert error["title"] == "Imagery unavailable"
    assert _idle(client)


def test_a_successful_fetch_actually_stores_the_layer(client, state, monkeypatch):
    """Guard against the busy tests passing because nothing happened."""
    with_imagery(state, monkeypatch, stub=StubImagery())
    s = post(client, "/api/imagery/fetch", {"name": "stub"})["state"]
    assert [b["name"] for b in s["basemaps"]] == ["stub"]
    assert s["basemaps"][0]["visible"]
    assert "stub credit" in s["info"]
    png = client.get("/api/basemap.png", params={"name": "stub"})
    assert png.headers["content-type"] == "image/png"


def test_fetch_all_reports_and_releases(client, state, monkeypatch):
    with_imagery(state, monkeypatch, good=StubImagery("good"),
                 blank=StubImagery("blank", outcome="blank"))
    reply = post(client, "/api/imagery/fetch_all")
    assert "1 fetched, 1 with no coverage here" in reply["notice"]["text"]
    assert _idle(client)


def test_busy_clears_after_fetching_linework(client):
    s = post(client, "/api/vectors/fetch", {"name": "stubvec"})["state"]
    assert _idle(client)
    assert "linework REFERENCE ONLY" in s["info"]
    assert len(client.get("/api/features").json()["vectors"]) == 1


def test_busy_clears_after_solving_the_datum(client, state):
    reply = post(client, "/api/vertical/solve", {"mode": "local"})
    assert _idle(client)
    assert state.vertical is not None
    assert reply["notice"]["title"] == "Vertical model (local)"
    assert reply["state"]["vertical"]["solved"]


def test_busy_clears_when_solving_fails(client, state, monkeypatch):
    def fail(*a, **k):
        raise RuntimeError("nope")

    monkeypatch.setattr(state, "solve_vertical", fail)
    error = post(client, "/api/vertical/solve", {"mode": "local"}, 400)["error"]
    assert error == {"title": "Could not solve", "text": "nope", "level": "error"}
    assert _idle(client)


def test_nested_actions_stay_balanced(server):
    with server.acting("outer"):
        with server.acting("inner"):
            assert server.busy == "inner"
        assert server.busy == "outer"
    assert server.busy is None


# --- dialogs: notices, errors and confirmations ---------------------------------------------

def test_an_action_without_data_says_so(app, tmp_path):
    from gpsrtk.app import AppState
    from gpsrtk.site import example_site

    empty = TestClient(create_app(AppState(site=example_site(), cache_dir=tmp_path)),
                       base_url="http://127.0.0.1", headers=ACTION)
    error = post(empty, "/api/sessions", status=400)["error"]
    assert error == {"title": "No data", "text": "Load an export first.", "level": "info"}


def test_a_refused_plan_edit_is_a_warning_with_its_own_title(client):
    post(client, "/api/plan/point/add", {"x": 12.0, "y": 15.0})
    error = post(client, "/api/plan/cell",
                 {"number": 1, "field": "rod", "value": "forty"}, 400)["error"]
    assert error["title"] == "Rod reading" and error["level"] == "warning"


def test_a_rod_reading_under_a_foot_is_confirmed_first(client, state):
    post(client, "/api/plan/point/add", {"x": 12.0, "y": 15.0})
    reply = post(client, "/api/plan/cell",
                 {"number": 1, "field": "rod", "value": "5.26"})
    assert reply["confirm"]["title"] == "Rod reading"
    assert state.plan.by_number(1).rod_in is None
    post(client, "/api/plan/cell",
         {"number": 1, "field": "rod", "value": "5.26", "confirm": True})
    assert state.plan.by_number(1).rod_in == pytest.approx(5.26)


def test_a_bulk_delete_asks_and_only_then_deletes(client, state):
    for x in (10.0, 12.0, 14.0):
        post(client, "/api/plan/point/add", {"x": x, "y": 15.0})
    reply = post(client, "/api/plan/delete", {"numbers": [1, 2]})
    assert reply["confirm"]["title"] == "Delete points"
    assert "state" not in reply
    assert len(state.plan.points) == 3

    post(client, "/api/plan/delete", {"numbers": [1, 2], "confirm": True})
    assert [p.number for p in state.plan.points] == [3]


def test_a_merge_offers_to_solve_the_offsets(client, synthetic_outing2):
    notice = post(client, "/api/export/add", {"path": str(synthetic_outing2)})["notice"]
    assert notice["level"] == "info"
    assert notice["actions"] == [{"label": "Solve session offsets",
                                  "post": "/api/vertical/solve",
                                  "body": {"mode": "ellipsoidal"}}]


def test_a_merge_that_cannot_be_reconciled_is_a_warning(
        client, synthetic_outing2, synthetic_elsewhere):
    post(client, "/api/export/add", {"path": str(synthetic_outing2)})
    notice = post(client, "/api/export/add", {"path": str(synthetic_elsewhere)})["notice"]
    assert notice["level"] == "warning"
    assert "CANNOT BE RECONCILED" in notice["text"]
    assert notice["actions"] == []


def test_opening_an_export_with_instrument_height_says_so(client, tmp_path):
    import zipfile

    from synthetic import track_points

    tracks = track_points().head(200)
    tracks["Instrument Ht"] = 1.8
    path = tmp_path / "Pole.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Pole_TRACK_POINTS.csv", tracks.to_csv(index=False))
    notice = post(client, "/api/export/open", {"path": str(path)})["notice"]
    assert "Instrument Ht is set" in notice["text"]
    assert "not applied" in notice["text"]


def test_opening_a_missing_file_is_an_error_not_a_crash(client, tmp_path):
    error = post(client, "/api/export/open", {"path": str(tmp_path / "nope.zip")},
                 400)["error"]
    assert error["title"] == "Could not open"


def test_a_file_with_no_survey_points_leaves_the_session_alone(client, state, tmp_path):
    """A mis-click in the file dialog must not empty the session."""
    before = {k: len(v) for k, v in state.layers.items()}
    state.solve_vertical("ellipsoidal")
    notes = tmp_path / "notes.csv"
    notes.write_text("hello,world\n1,2\n")

    error = post(client, "/api/export/open", {"path": str(notes)}, 400)["error"]
    assert "no survey points" in error["text"]
    assert {k: len(v) for k, v in state.layers.items()} == before
    assert state.vertical is not None


# --- the plan through the API ---------------------------------------------------------------

def test_points_are_placed_and_moved_in_local_metres(client, state):
    reply = post(client, "/api/plan/point/add", {"x": 12.0, "y": 15.0})
    assert reply["selected"] == 1
    point = state.plan.by_number(1)
    assert (point.planned_e, point.planned_n) == pytest.approx((E0, N0))

    reply = post(client, "/api/plan/point/move", {"number": 1, "x": 14.0, "y": 14.0})
    assert reply["moved"]
    assert (point.planned_e, point.planned_n) == pytest.approx((E0 + 2, N0 - 1))


def test_a_measured_point_is_not_moved(client, state):
    post(client, "/api/plan/point/add", {"x": 12.0, "y": 15.0})
    post(client, "/api/plan/coords", {"number": 1, "frame": "local ft",
                                      "x": "40", "y": "50"})
    reply = post(client, "/api/plan/point/move", {"number": 1, "x": 20.0, "y": 20.0})
    assert reply["moved"] is False
    assert state.plan.by_number(1).planned_e == pytest.approx(E0)


def test_a_line_and_a_setup_can_be_drawn(client):
    s = post(client, "/api/plan/line/add",
             {"vertices": [[5.0, 5.0], [10.0, 6.0], [15.0, 5.0]]})["state"]
    assert s["plan"]["lines"][0]["line_id"] == "line-1"
    assert len(s["plan"]["points"]) == 3
    s = post(client, "/api/plan/setup/add", {"x": 20.0, "y": 10.0})["state"]
    assert s["plan"]["setups"] == [{"name": "A", "x": pytest.approx(20.0),
                                    "y": pytest.approx(10.0)}]


def test_a_position_editor_action_says_what_it_did(client):
    post(client, "/api/plan/point/add", {"x": 12.0, "y": 15.0})
    reply = post(client, "/api/plan/coords",
                 {"number": 1, "frame": "UTM m", "x": str(E0 + 0.5), "y": str(N0)})
    assert reply["result"].startswith("Measured position set")
    reply = post(client, "/api/plan/coords/clear", {"number": 1})
    assert "planned mark stands" in reply["result"]


def test_solving_the_alignment_applies_and_reports(client, state):
    for i in range(3):
        p = state.plan.add_point(449710.0 + 5 * i, 4604563.0, purpose="building corner")
        p.observed_e, p.observed_n = p.planned_e + 0.30, p.planned_n - 0.15
        p.method, p.fix = "rtk", 4
    state.plan_changed()

    reply = post(client, "/api/imagery/offset/solve")
    assert state.imagery_offset.de == pytest.approx(0.30)
    assert "imagery moved" in reply["notice"]["text"].lower()
    # 0.30 m is 0.98 ft, which is what the alignment boxes show.
    assert reply["state"]["imagery_offset"]["de_ft"] == pytest.approx(0.98, abs=0.005)


def test_solving_with_nothing_to_solve_from_explains_itself(client, state):
    error = post(client, "/api/imagery/offset/solve", status=400)["error"]
    assert state.imagery_offset.zero
    assert "see in the photo" in error["text"]
    assert error["level"] == "info"


# --- the filter stack ----------------------------------------------------------------------

def test_stages_can_be_added_moved_and_removed(client):
    s = post(client, "/api/chain/add", {"kind": "accuracy_threshold"})["state"]
    kinds = [st["kind"] for st in s["chain"]["stages"]]
    assert kinds[-1] == "accuracy_threshold"
    s = post(client, "/api/chain/move", {"index": len(kinds) - 1, "delta": -1})["state"]
    assert s["chain"]["stages"][-2]["kind"] == "accuracy_threshold"
    s = post(client, "/api/chain/remove", {"index": len(kinds) - 2})["state"]
    assert "accuracy_threshold" not in [st["kind"] for st in s["chain"]["stages"]]


def test_a_parameter_is_parsed_from_what_was_typed(client, state):
    s = post(client, "/api/chain/edit",
             {"index": 2, "enabled": True, "params": {"minimum": "0.9"}})["state"]
    assert state.chain.stages[2].minimum == pytest.approx(0.9)
    assert s["chain"]["stages"][2]["counts"] != "disabled"


def test_a_parameter_the_stage_cannot_use_is_undone(client, state):
    """A mistyped value used to stay in the chain, holding a value no stage
    could apply. The edit is rolled back and the error shown instead."""
    before = state.chain.to_list()
    error = post(client, "/api/chain/edit",
                 {"index": 2, "enabled": True, "params": {"minimum": "'fast'"}},
                 400)["error"]
    assert error["title"] == "Filter parameter"
    assert state.chain.to_list() == before
    assert state.result is not None


# --- the datum tie ---------------------------------------------------------------------------

def test_the_datum_form_lists_the_points_that_were_shot(client):
    form = client.get("/api/datum").json()
    assert form["points"] == list(range(1, 13))
    assert form["point"] == 1 and form["elev_ft"] == 100.0 and not form["tied"]


def test_changing_the_tie_re_solves_a_solved_model(client, state):
    post(client, "/api/vertical/solve", {"mode": "local"})
    reply = post(client, "/api/datum", {"point": "1", "elev_ft": 250.0,
                                        "note": "slab", "frame": "Revit", "tied": True})
    assert reply["notice"]["title"].startswith("Vertical model")
    assert state.site.vertical.tied_to_model
    assert "[tied to Revit]" in reply["state"]["qc"]


def test_a_bad_benchmark_is_refused(client):
    error = post(client, "/api/datum", {"point": "garage", "elev_ft": 100.0},
                 400)["error"]
    assert "'garage' is not a point id" in error["text"]


# --- files, downloads and projects -------------------------------------------------------------

def _zip_names(client, url):
    r = client.get(url)
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("attachment")
    return sorted(zipfile.ZipFile(io.BytesIO(r.content)).namelist())


def test_the_revit_export_is_a_download_with_its_origin(client):
    reply = post(client, "/api/export/revit")
    assert _zip_names(client, reply["download"]["url"]) == \
        ["revit_points_ft.csv", "revit_points_ft_ORIGIN.txt"]
    # No vertical model: these are antenna heights, and it says so loudly.
    assert reply["notice"]["level"] == "warning"
    assert "WARNING: no vertical model applied" in reply["notice"]["text"]


def test_the_heightmap_is_a_download_of_every_file(client):
    reply = post(client, "/api/export/heightmap")
    names = _zip_names(client, reply["download"]["url"])
    assert "heightmap_fixed_16bit.png" in names
    assert "heightmap_fixed_mask.npy" in names
    assert "heightmap_fixed.pgw" in names


def _zip_text(client, url, name):
    r = client.get(url)
    return zipfile.ZipFile(io.BytesIO(r.content)).read(name).decode("utf-8")


def test_offsets_only_heights_are_not_called_a_local_datum(client, state):
    """After "Solve session offsets only" the elevation column holds
    ellipsoidal ANTENNA heights near 860 ft. They were labelled LOCAL
    ARBITRARY in the Revit sidecar; every readout now says what they are."""
    post(client, "/api/vertical/solve", {"mode": "ellipsoidal"})
    label = "ELLIPSOIDAL antenna height, session offsets applied"
    s = client.get("/api/state").json()
    assert label in s["qc"]
    assert s["terrain"]["elevation"]["datum"] == "ellipsoidal, offsets applied"

    reply = post(client, "/api/export/revit")
    assert reply["notice"]["level"] == "warning"
    assert label in reply["notice"]["text"]
    sidecar = _zip_text(client, reply["download"]["url"],
                        "revit_points_ft_ORIGIN.txt")
    assert label in sidecar and "LOCAL ARBITRARY" not in sidecar

    reply = post(client, "/api/export/heightmap")
    info = _zip_text(client, reply["download"]["url"], "heightmap_fixed_INFO.txt")
    assert f"heights           : {label} (elev_m)" in info


def test_a_local_solve_is_labelled_local_and_arbitrary(client):
    post(client, "/api/vertical/solve", {"mode": "local"})
    s = client.get("/api/state").json()
    assert "[local datum, ARBITRARY origin]" in s["qc"]
    assert s["terrain"]["elevation"]["datum"] == "local datum"
    reply = post(client, "/api/export/revit")
    assert "LOCAL ARBITRARY" in _zip_text(client, reply["download"]["url"],
                                          "revit_points_ft_ORIGIN.txt")


def test_navd88_heights_say_they_are_of_the_antenna(state):
    from gpsrtk import vertical as V

    model = state.solve_vertical(
        "navd88", geoid=V.GeoidSeparation(value_m=-29.2, model="GEOID18"))
    assert V.height_label(model, state.site, "elev_m") == \
        "NAVD88 orthometric, antenna height"
    assert V.height_label(None, state.site, "z_ellip_m", short=True) == \
        "raw ellipsoidal"


def test_the_heightmap_is_named_after_the_points_it_was_made_from(client, state):
    """It said "fixed" whatever the filter stack kept."""
    fix = next(s for s in state.chain.stages if s.kind == "fix_select")
    fix.values = (4, 5)
    state.recompute()
    reply = post(client, "/api/export/heightmap")
    assert reply["download"]["filename"] == "heightmap_all.zip"
    assert "heightmap_all_16bit.png" in _zip_names(client, reply["download"]["url"])


def test_the_field_sheet_opens_in_the_browser(client):
    post(client, "/api/plan/point/add", {"x": 12.0, "y": 15.0})
    reply = post(client, "/api/plan/fieldsheet")
    assert reply["download"]["inline"]
    r = client.get(reply["download"]["url"])
    assert r.headers["content-disposition"].startswith("inline")
    assert "Tie A" in r.text


def test_an_expired_download_says_so(client):
    r = client.get("/api/downloads/0123456789abcdef")
    assert r.status_code == 404 and "expired" in r.text


def test_saving_needs_somewhere_to_save(client):
    error = post(client, "/api/project/save", {"view": {}}, 400)["error"]
    assert error["title"] == "Save project"


def test_a_project_saves_and_reopens_through_the_api(client, state, tmp_path):
    view = {"colormap": "magma", "color_by": "session", "tab": 0}
    s = post(client, "/api/project/save",
             {"path": str(tmp_path / "api"), "view": view})["state"]
    assert s["project_path"].endswith("api.yardproj")
    assert s["title"] == "Yard Survey — api.yardproj"

    before = s["rev"]["view"]
    s = post(client, "/api/project/open", {"path": s["project_path"]})["state"]
    assert {k: s["view"][k] for k in view} == view
    # The session comparison travels with the view.
    assert s["view"]["hidden_sessions"] == []
    assert s["view"]["surface_from_shown_sessions"] is False
    assert s["rev"]["view"] > before


def test_a_project_with_a_missing_export_opens_with_a_warning(client, tmp_path):
    from gpsrtk.project import Project
    from gpsrtk.site import example_site

    path = Project(site=example_site(), sources=[str(tmp_path / "gone.zip")]).save(
        tmp_path / "p.yardproj")
    notice = post(client, "/api/project/open", {"path": str(path)})["notice"]
    assert notice["level"] == "warning"
    assert "source not found" in notice["text"]


def test_the_file_listing_filters_and_orders(client, tmp_path):
    (tmp_path / "b folder").mkdir()
    (tmp_path / "a.zip").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / ".hidden.zip").write_bytes(b"x")
    listing = client.get("/api/files", params={"dir": str(tmp_path), "exts": ".zip"}).json()
    names = [(e["name"], e["dir"]) for e in listing["entries"]]
    assert ("b folder", True) in names and ("a.zip", False) in names
    assert "notes.txt" not in dict(names) and ".hidden.zip" not in dict(names)
    assert names.index(("b folder", True)) < names.index(("a.zip", False))
    assert listing["parent"] == str(tmp_path.parent.resolve())


def test_a_folder_that_is_not_there_is_a_warning(client, tmp_path):
    r = client.get("/api/files", params={"dir": str(tmp_path / "nowhere")})
    assert r.status_code == 400 and r.json()["error"]["level"] == "warning"


def test_the_page_is_served_and_always_revalidated(client):
    r = client.get("/")
    assert r.status_code == 200 and "Yard Survey" in r.text
    assert r.headers["cache-control"] == "no-cache"
    assert client.get("/static/js/main.js").headers["cache-control"] == "no-cache"


def test_quit_without_a_server_to_stop_says_so(client):
    assert post(client, "/api/quit") == {"stopping": False}


# --- sessions ----------------------------------------------------------------------

def test_the_snapshot_describes_each_session(client):
    sessions = client.get("/api/state").json()["sessions"]
    (only,) = sessions["list"]
    assert only["shown"] and only["rms_cm"] < 3.0
    assert sessions["surface_from_shown"] is False


def test_sessions_can_be_hidden_soloed_and_restored(client, synthetic_outing2):
    s = post(client, "/api/export/add", {"path": str(synthetic_outing2)})["state"]
    names = [row["name"] for row in s["sessions"]["list"]]
    assert len(names) == 2 and len(s["sessions"]["pairs"]) == 1
    everything = views.unpack_points(client.get("/api/points").content)["total"]

    s = post(client, "/api/sessions/visible", {"name": names[0], "visible": False})["state"]
    assert [row["shown"] for row in s["sessions"]["list"]] == [False, True]
    assert "1 session hidden" in s["points_note"]
    fewer = views.unpack_points(client.get("/api/points").content)["total"]
    assert 0 < fewer < everything

    s = post(client, "/api/sessions/shown", {"names": [names[0]]})["state"]
    assert [row["shown"] for row in s["sessions"]["list"]] == [True, False]
    s = post(client, "/api/sessions/shown", {"names": None})["state"]
    assert all(row["shown"] for row in s["sessions"]["list"])


def test_the_surface_can_follow_the_shown_sessions(client, synthetic_outing2):
    s = post(client, "/api/export/add", {"path": str(synthetic_outing2)})["state"]
    first = s["sessions"]["list"][0]["name"]
    post(client, "/api/sessions/shown", {"names": [first]})
    s = post(client, "/api/sessions/surface", {"on": True})["state"]
    assert s["sessions"]["surface_from_shown"]
    assert "from 1 of 2 sessions" in s["qc"]
    assert "from 1 of 2 sessions" in s["points_note"]


# --- terrain -------------------------------------------------------------------------

def test_the_snapshot_carries_what_the_legend_needs(client):
    s = client.get("/api/state").json()
    t = s["terrain"]
    assert 0 < t["slope"]["median"] < t["slope"]["p90"]
    assert t["mapped_m2"] > 1000 and t["relief_cm"] > 50
    assert t["elevation"]["lo_ft"] < t["elevation"]["hi_ft"]
    assert t["elevation"]["datum"] == "raw ellipsoidal"
    assert {"terrain", "magma_r"} <= set(s["scales"])


def test_the_surface_can_be_shaded_by_slope(client):
    elevation = client.get("/api/surface.png", params={"mode": "elevation"})
    slope = client.get("/api/surface.png", params={"mode": "slope", "slope_max": 8})
    assert slope.headers["content-type"] == "image/png"
    assert slope.content != elevation.content


def test_contours_arrive_in_local_metres_labelled_from_the_low_point(client, state):
    data = client.get("/api/contours", params={"interval_cm": 10}).json()
    cms = [level["cm"] for level in data["levels"]]
    assert cms[:3] == [10, 20, 30]
    assert [level["major"] for level in data["levels"][:2]] == [False, True]
    xs = [x for level in data["levels"] for line in level["lines"] for x, _ in line]
    assert -1 < min(xs) and max(xs) < 41, "local metres, not UTM"


def test_a_contour_interval_out_of_range_is_refused(client):
    r = client.get("/api/contours", params={"interval_cm": 0})
    assert r.status_code == 400 and r.json()["error"]["title"] == "Contours"


def test_drainage_arrows_are_unit_vectors_on_measured_ground(client):
    data = client.get("/api/drainage", params={"spacing_m": 2}).json()
    assert data["spacing_m"] == 2 and len(data["arrows"]) > 100
    for x, y, de, dn, pct in data["arrows"]:
        assert de * de + dn * dn == pytest.approx(1.0, abs=1e-3)
        assert pct >= 0


def test_the_printed_maps_download_as_png(client):
    for url, body, name in (("/api/export/slope_map", {"slope_max": 8}, "slope_map.png"),
                            ("/api/export/contour_map", {"interval_cm": 10}, "contours_10cm.png")):
        reply = post(client, url, body)
        assert reply["download"]["filename"] == name
        r = client.get(reply["download"]["url"])
        assert r.headers["content-type"] == "image/png"
        assert r.content.startswith(b"\x89PNG")


def test_the_revit_export_says_how_many_laser_shots_it_added(client):
    post(client, "/api/vertical/solve", {"mode": "local"})
    reply = post(client, "/api/export/revit")
    assert "12 laser terrain shots were added" in reply["notice"]["text"]
    reply = post(client, "/api/export/heightmap")
    assert "12 laser terrain shots replace the GNSS" in reply["notice"]["text"]


def test_the_status_strip_says_what_the_heights_are(client):
    strip = client.get("/api/state").json()["strip"]
    texts = [s["text"] for s in strip]
    assert texts[0] == "Datum: raw ellipsoidal, not solved" and strip[0]["level"] == "warn"
    assert "model: none" in texts and "RTK fixed only" in texts
    assert any(t.endswith("% measured") for t in texts)

    strip = post(client, "/api/vertical/solve", {"mode": "local"})["state"]["strip"]
    assert strip[0]["text"] == "Datum: local, arbitrary origin"
    assert "model: local" in [s["text"] for s in strip]


def test_the_strip_says_when_session_offsets_are_unsolved(client, synthetic_outing2):
    post(client, "/api/export/add", {"path": str(synthetic_outing2)})
    strip = client.get("/api/state").json()["strip"]
    assert {"text": "2 sessions, offsets NOT solved", "level": "warn"} in strip
    strip = post(client, "/api/vertical/solve", {"mode": "ellipsoidal"})["state"]["strip"]
    assert {"text": "2 sessions, offsets solved", "level": "ok"} in strip


def test_a_solve_with_something_to_act_on_is_a_warning(client, synthetic_outing2,
                                                       synthetic_elsewhere):
    """Info notices become toasts that can be glanced past; a solve that
    leaves a session untied must still stop the work."""
    reply = post(client, "/api/vertical/solve", {"mode": "local"})
    assert reply["notice"]["level"] == "info"
    post(client, "/api/export/add", {"path": str(synthetic_outing2)})
    post(client, "/api/export/add", {"path": str(synthetic_elsewhere)})
    reply = post(client, "/api/vertical/solve", {"mode": "ellipsoidal"})
    assert reply["notice"]["level"] == "warning"
    assert "cannot be tied" in reply["notice"]["text"]


def test_the_3d_grid_and_the_layers_are_in_feet(client, state):
    """The 2D legend is in feet; the 3D view and the layer list said metres,
    and the 3D view named no datum."""
    g = client.get("/api/surface/grid").json()
    assert g["units"] == "ft" and g["datum"] == "raw ellipsoidal"
    finite = [v for row in g["z"] for v in row if v is not None]
    zmin_m = float(np.nanmin(state.surface.z_masked))
    assert min(finite) == pytest.approx(zmin_m / 0.3048, abs=1e-3)
    assert g["x"][1] - g["x"][0] == pytest.approx(
        (state.surface.extent.width / (state.surface.z.shape[1] - 1)) / 0.3048, abs=1e-3)

    layers = {ly["name"]: ly["describe"] for ly in client.get("/api/state").json()["layers"]}
    assert layers["track_points"].endswith(" ft")
    assert " m" not in layers["track_points"]
    # The reports keep metres.
    assert state.layers["track_points"].describe().endswith(" m")

    post(client, "/api/vertical/solve", {"mode": "local"})
    assert client.get("/api/surface/grid").json()["datum"] == "local datum"


def test_the_filter_stack_reads_in_words(client, state):
    """Plain names in the Add list; the fix filter as "fixed" and "float",
    not the text "[4]" - with the saved chain unchanged."""
    chain = client.get("/api/state").json()["chain"]
    names = {k["kind"]: k["name"] for k in chain["kinds"]}
    assert names["fix_select"] == "Fix quality (fixed / float)"
    assert names["speed_threshold"] == "Speed limits"
    fix = next(s for s in chain["stages"] if s["kind"] == "fix_select")
    (values,) = [p for p in fix["params"] if p["key"] == "values"]
    assert values["choices"] == [{"label": "fixed", "value": 4, "checked": True},
                                 {"label": "float", "value": 5, "checked": False}]

    index = [s["kind"] for s in chain["stages"]].index("fix_select")
    post(client, "/api/chain/edit", {"index": index, "params": {"values": [4, 5]}})
    saved = next(d for d in state.chain.to_list() if d["kind"] == "fix_select")
    assert saved["values"] == [4, 5]
    assert set(state.result.df["fix"].unique()) == {4, 5}


def test_opened_files_are_remembered_for_the_start_panel(tmp_path, synthetic_zip,
                                                         synthetic_outing2):
    from gpsrtk.app import AppState
    from gpsrtk.site import example_site
    from gpsrtk.web import files

    cache = tmp_path / "cache"
    st = AppState(site=example_site(), cache_dir=cache)
    c = TestClient(create_app(st), base_url="http://127.0.0.1", headers=ACTION)
    assert c.get("/api/state").json()["recent"] == []
    post(c, "/api/export/open", {"path": str(synthetic_zip)})
    post(c, "/api/export/add", {"path": str(synthetic_outing2)})
    post(c, "/api/project/save", {"path": str(tmp_path / "p.yardproj"), "view": {}})
    assert c.get("/api/state").json()["recent"] == [], "only the empty page lists them"

    fresh = AppState(site=example_site(), cache_dir=cache)
    recent = TestClient(create_app(fresh), base_url="http://127.0.0.1").get(
        "/api/state").json()["recent"]
    assert [(r["name"], r["kind"]) for r in recent] == [
        ("p.yardproj", "project"), ("Outing 2.zip", "export"),
        ("Synthetic Yard.zip", "export")]

    for k in range(12):                           # never more than eight
        f = tmp_path / f"x{k}.zip"
        f.write_bytes(b"")
        files.remember_recent(cache, f, "export")
    (tmp_path / "x11.zip").unlink()               # gone from disk: not listed
    names = [r["name"] for r in files.recent_files(cache)]
    assert len(names) == 7 and names[0] == "x10.zip"
