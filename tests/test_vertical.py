"""Vertical datum: level network, session offsets, and applying a model."""

import json

import numpy as np
import pytest

from gpsrtk import filters as F, vertical as V
from gpsrtk.model import pointset as P
from gpsrtk.model.adjust import LeastSquares
from gpsrtk.model.pointset import concat, elevation_column
from gpsrtk.units import M_PER_IN, ft_to_m, m_to_ft


@pytest.fixture(scope="module")
def lawn(spots):
    return F.KindSelect(names=["lawn"]).apply(spots)


@pytest.fixture(scope="module")
def fixed(tracks):
    return F.default_chain().run(tracks)


# --- the engine ----------------------------------------------------------

def test_least_squares_recovers_a_known_solution():
    ls = LeastSquares()
    ls.add({"a": 1.0, "b": -1.0}, 3.0)
    ls.add({"b": 1.0, "c": -1.0}, 2.0)
    ls.add({"a": 1.0, "c": -1.0}, 5.0)     # redundant and consistent
    ls.constrain("c", 10.0)
    adj = ls.solve()

    assert adj.values["c"] == pytest.approx(10.0, abs=1e-6)
    assert adj.values["b"] == pytest.approx(12.0, abs=1e-6)
    assert adj.values["a"] == pytest.approx(15.0, abs=1e-6)
    # Three observations of rank 2 plus one constraint determines three
    # unknowns, leaving one redundant observation.
    assert adj.dof == 1
    assert np.abs(adj.obs_residuals).max() < 1e-6


def test_anchor_rows_do_not_pollute_residual_statistics():
    """An anchor exists to keep a column in the system, not to be fitted.
    Letting it into sigma0 makes a good network look like it has a blunder."""
    ls = LeastSquares()
    ls.add({"a": 1.0, "b": -1.0}, 0.0)
    ls.add({"a": 1.0, "b": -1.0}, 0.0)
    ls.anchor("lonely", 0.0)
    ls.constrain("a", 100.0)
    adj = ls.solve()

    assert adj.n_obs == 2, "anchor must not be counted as an observation"
    assert len(adj.obs_residuals) == 2
    assert np.abs(adj.obs_residuals).max() < 1e-6


def test_disconnected_unknowns_are_reported():
    ls = LeastSquares()
    ls.add({"a": 1.0, "b": -1.0}, 1.0)
    ls.add({"x": 1.0, "y": -1.0}, 1.0)
    groups = ls.components()
    assert len(groups) == 2


# --- laser level network -------------------------------------------------

def test_level_network_matches_the_archive_convention(lawn):
    """process_survey.py set HI = benchmark + rod(benchmark).
    Point 1 had a 45.5 in rod and was held at 100.000 ft."""
    net = V.level_network(lawn, benchmark_id=1, benchmark_elev_ft=100.0)
    hi = m_to_ft(next(iter(net.instrument_heights.values())))
    assert hi == pytest.approx(100.0 + 45.5 / 12.0, abs=1e-4)
    assert m_to_ft(net.elevations[1]) == pytest.approx(100.0, abs=1e-6)


def test_level_network_reports_zero_redundancy(lawn):
    """Every point shot once from one setup: the fit is forced, not verified.
    Reporting this is what stops a perfect fit looking like a good one."""
    net = V.level_network(lawn)
    assert net.adjustment.dof == 0
    assert not net.adjustment.has_redundancy


def test_one_shared_point_between_setups_buys_no_redundancy(lawn):
    """A second setup adds an unknown (its own HI), so a single shared point
    only determines it. This is the field-procedure trap worth encoding."""
    import pandas as pd

    d = lawn.df.copy()
    extra = d.iloc[[3]].copy()
    extra[P.SETUP] = 1
    extra[P.ROD_IN] = extra[P.ROD_IN] + 6.0
    tied = lawn.with_frame(pd.concat([d, extra], ignore_index=True), "one tie")

    net = V.level_network(tied)
    assert len(net.instrument_heights) == 2
    assert net.adjustment.dof == 0
    assert not net.adjustment.has_redundancy


def test_two_shared_points_between_setups_buy_redundancy(lawn):
    """Redundancy gained is (shared points - 1), so two is the minimum."""
    import pandas as pd

    d = lawn.df.copy()
    extra = d.iloc[[3, 7]].copy()
    extra[P.SETUP] = 1
    extra[P.ROD_IN] = extra[P.ROD_IN] + 6.0     # second setup reads 6 in higher
    tied = lawn.with_frame(pd.concat([d, extra], ignore_index=True), "two ties")

    net = V.level_network(tied)
    assert net.adjustment.has_redundancy
    assert net.adjustment.dof == 1
    assert len(net.instrument_heights) == 2
    # The two setups must differ by exactly the 6 inches injected.
    his = sorted(net.instrument_heights.values())
    assert (his[1] - his[0]) == pytest.approx(6.0 * M_PER_IN, abs=1e-6)
    # Consistent data means the residuals stay at zero even with redundancy.
    assert np.abs(net.adjustment.obs_residuals).max() < 1e-6


def test_a_blunder_shows_up_once_there_is_redundancy(lawn):
    """The point of redundancy: a mis-read rod becomes visible."""
    import pandas as pd

    d = lawn.df.copy()
    extra = d.iloc[[3, 7]].copy()
    extra[P.SETUP] = 1
    extra[P.ROD_IN] = extra[P.ROD_IN] + 6.0
    extra.iloc[1, extra.columns.get_loc(P.ROD_IN)] += 3.0   # transposed digit
    tied = lawn.with_frame(pd.concat([d, extra], ignore_index=True), "blunder")

    net = V.level_network(tied)
    assert np.abs(net.adjustment.obs_residuals).max() > 0.5 * M_PER_IN


def test_setup_ids_forward_fill(lawn):
    """The setup attribute is typed once when the instrument is placed and left
    blank after, so blank means 'same setup', not 'unknown'."""
    setups = V.resolve_setups(lawn)
    assert setups.notna().all()
    assert setups.nunique() == 1


# --- session offsets -----------------------------------------------------

def test_session_offsets_recover_an_injected_bias(fixed):
    """Split one session in two, shift one half by a known amount, and check
    the solver gets it back. This is the whole basis of multi-session merging."""
    d = fixed.df.copy()
    half = np.arange(len(d)) % 2 == 0
    injected = 0.137                                  # metres
    d.loc[half, P.SESSION] = "session_B"
    d.loc[half, P.Z] = d.loc[half, P.Z] + injected
    mixed = fixed.with_frame(d, "synthetic split")

    sol = V.session_offsets(mixed, radius_m=0.5, min_seconds=0.0,
                            reference=str(d.loc[~half, P.SESSION].iloc[0]))
    assert sol.offsets["session_B"] == pytest.approx(injected, abs=0.01)
    assert not sol.unresolved


def test_session_offsets_flag_sessions_with_no_overlap(fixed):
    """A session sharing no ground with the reference has an unknowable offset.
    It must be reported, not silently assigned zero.

    The split leaves a wide gap so the two groups genuinely cannot pair; a
    boundary split still produces neighbours across the line.
    """
    d = fixed.df.copy()
    lo, hi = d[P.E].quantile([0.25, 0.75])
    west, east = d[P.E] < lo, d[P.E] > hi
    d = d.loc[west | east].reset_index(drop=True)
    d.loc[d[P.E] > hi, P.SESSION] = "island"
    split = fixed.with_frame(d, "disjoint sessions")

    reference = str(d.loc[d[P.E] < lo, P.SESSION].iloc[0])
    sol = V.session_offsets(split, radius_m=0.30, min_seconds=0.0,
                            reference=reference)
    assert sol.pair_counts == {}, "the two groups must not overlap at all"
    assert "island" in sol.unresolved


def test_session_offsets_need_more_than_one_session(fixed):
    sol = V.session_offsets(fixed, radius_m=0.3)
    assert sol.adjustment.n_obs == 0


def test_real_two_session_offset_is_well_determined(fixed, lawn):
    """The rod-mounted and mower-mounted antennas differ by a real constant.
    Tight scatter is what makes it a constant rather than noise."""
    sol = V.session_offsets(concat([fixed, lawn]), radius_m=1.0)
    assert len(sol.offsets) == 2
    assert not sol.unresolved
    magnitude = max(abs(v) for v in sol.offsets.values())
    assert 1.5 < magnitude < 2.5, "expected roughly 2 m of antenna difference"
    assert sol.adjustment.sigma0 < 0.10


# --- applying a model ----------------------------------------------------

def test_apply_vertical_arithmetic(fixed):
    geoid = V.GeoidSeparation(value_m=-29.207, model="GEOID18")
    out = V.apply_vertical(fixed, geoid=geoid, antenna_height_m=0.5,
                           datum_shift_m=2.0)
    expected = fixed.df[P.Z].to_numpy() - 0.5 + 29.207 + 2.0
    assert out.df[P.ELEV].to_numpy() == pytest.approx(expected)


def test_elevation_column_prefers_the_derived_value(fixed):
    assert elevation_column(fixed) == P.Z
    out = V.apply_vertical(fixed, datum_shift_m=1.0)
    assert elevation_column(out) == P.ELEV


def test_binning_preserves_the_elevation_column(fixed, site):
    """Reducing only the raw column would silently discard the vertical model
    and make exports fall back to ellipsoidal heights."""
    adj = V.apply_vertical(fixed, datum_shift_m=-260.0)
    binned = F.BinToCell(cell=1.0).for_site(site).apply(adj)
    assert P.ELEV in binned.df.columns
    assert elevation_column(binned) == P.ELEV


def test_local_datum_puts_the_benchmark_where_the_laser_says(fixed, spots):
    """End to end: after solving, the walked surface must agree with the laser
    elevations it was tied to, with essentially zero mean."""
    from scipy.interpolate import griddata

    model = V.solve_vertical(fixed, spots, mode="local")
    assert model.level is not None
    assert model.tie and model.tie["n"] > 0

    adj = model.apply(fixed)
    lawn = F.KindSelect(names=["lawn"]).apply(spots)
    ref = V.spots_with_laser_elevations(lawn, model.level)

    zi = griddata(adj.df[[P.E, P.N]].to_numpy(), adj.df[P.ELEV].to_numpy(),
                  ref.df[[P.E, P.N]].to_numpy(), method="linear")
    diff = zi - ref.df[P.ELEV].to_numpy()
    ok = np.isfinite(diff)
    assert ok.sum() > 10
    assert abs(diff[ok].mean()) < 0.02, "tie must close to near zero mean"

    # And the result must land in the neighbourhood of the 100 ft benchmark,
    # not the ~860 ft of an untied ellipsoidal height.
    elevs_ft = m_to_ft(adj.df[P.ELEV].to_numpy())
    assert 90 < elevs_ft.min() < 110


def test_surface_follows_the_elevation_column(fixed, site, spots):
    from gpsrtk.surface import build_surface

    model = V.solve_vertical(fixed, spots, mode="local")
    adj = model.apply(fixed)
    s = build_surface(adj, site, size=64)
    assert s.z_column == P.ELEV
    assert 90 < m_to_ft(np.nanmin(s.z_masked)) < 110


# --- geoid ---------------------------------------------------------------

def test_geoid_cache_is_used_without_network(tmp_path):
    cache = tmp_path / "geoid.json"
    cache.write_text(json.dumps({
        "value_m": -29.207, "model": "GEOID18", "error_m": 0.03,
        "lat": 42.0, "lon": -93.6}), encoding="utf-8")

    sep = V.fetch_geoid_separation(0.0, 0.0, cache_path=cache)
    assert sep.value_m == pytest.approx(-29.207)
    assert sep.model == "GEOID18"
    # H = h - N, so a negative separation raises the height.
    assert sep.to_orthometric(100.0) == pytest.approx(129.207)


# --- datum tie -----------------------------------------------------------

def test_default_datum_is_flagged_as_arbitrary(site):
    v = site.vertical
    assert not v.tied_to_model
    assert "ARBITRARY" in v.describe()


def test_tying_the_datum_shifts_by_a_constant(fixed, spots, site):
    """Changing which point is held, and to what, must translate the whole
    survey rigidly. If it changed the shape, the tie would be corrupting the
    surface rather than placing it."""
    from copy import deepcopy

    loose = V.solve_vertical(fixed, spots, mode="local",
                             benchmark_id=1, benchmark_elev_ft=100.0)
    z_loose = loose.apply(fixed).df[P.ELEV].to_numpy()

    tied = V.solve_vertical(fixed, spots, mode="local",
                            benchmark_id=4, benchmark_elev_ft=0.25,
                            tied_to_model=True, model_frame="Revit project")
    z_tied = tied.apply(fixed).df[P.ELEV].to_numpy()

    shift = z_tied - z_loose
    assert np.ptp(shift) < 1e-9, "must be a rigid translation"
    assert (z_tied.max() - z_tied.min()) == pytest.approx(
        z_loose.max() - z_loose.min())
    assert tied.tied_to_model
    assert "TIED" in tied.describe()


def test_benchmark_that_was_never_shot_is_refused(fixed, spots):
    """Falling back silently would produce a surface on a datum the user
    believes is tied when it is not."""
    with pytest.raises(ValueError, match="not among the rod shots"):
        V.solve_vertical(fixed, spots, mode="local", benchmark_id=999,
                         benchmark_elev_ft=0.0, tied_to_model=True)


def test_tie_state_survives_serialisation(fixed, spots):
    model = V.solve_vertical(fixed, spots, mode="local", benchmark_id=4,
                             benchmark_elev_ft=0.25, tied_to_model=True,
                             model_frame="Revit project")
    back = V.VerticalModel.from_dict(json.loads(json.dumps(model.to_dict())))
    assert back.tied_to_model
    assert back.model_frame == "Revit project"
    assert back.datum_shift_m == pytest.approx(model.datum_shift_m)


def test_site_datum_round_trips_including_new_fields(tmp_path, site):
    from gpsrtk.site import Site

    site.vertical.benchmark_point_id = 7
    site.vertical.tied_to_model = True
    site.vertical.model_frame = "Revit project"
    path = tmp_path / "site.json"
    site.save(path)

    back = Site.load(path)
    assert back.vertical.benchmark_point_id == 7
    assert back.vertical.tied_to_model
    assert back.vertical.model_frame == "Revit project"


def test_older_site_json_without_tie_fields_still_loads(tmp_path):
    """Projects saved before the datum tie existed must keep opening."""
    from gpsrtk.site import Site

    legacy = {"name": "old", "epsg": 32615, "origin_e": 449700.0,
              "origin_n": 4604550.0,
              "vertical": {"name": "local arbitrary",
                           "benchmark_elev_ft": 100.0, "benchmark_note": ""},
              "surface": {}, "notes": ""}
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")

    back = Site.load(path)
    assert back.vertical.benchmark_point_id == 1
    assert back.vertical.tied_to_model is False


# --- which rod shots reach the network (synthetic) ------------------------

def test_a_benchmark_plan_shot_can_hold_the_datum(state):
    """A plan shot whose purpose is "benchmark" is levelling like any other.
    It used to be left out of the network, so the datum could not hang from
    the garage slab: "benchmark point 101 is not among the rod shots"."""
    point = state.plan.add_point(449705.0, 4604555.0, purpose="benchmark",
                                 setup="0")
    point.number = 101
    point.rod_in = 30.0
    state.plan_changed()
    state.site.vertical.benchmark_point_id = "P101"
    state.site.vertical.benchmark_elev_ft = 100.0

    model = state.solve_vertical("local")
    assert model.level.elevations["P101"] == pytest.approx(ft_to_m(100.0), abs=1e-6)
    # The other shots moved with it: the same setup reads them against it.
    assert 1 in model.level.elevations
    assert model.tie and model.tie["n"] > 0


def test_a_building_shot_joins_the_network_but_not_the_tie(state):
    """A "bldg" rod shot is levelled, and can hold the datum, but it does
    not describe the ground, so the tie to the GNSS surface leaves it out."""
    spots = state.layers["spots"]
    d = spots.df.copy()
    d.loc[d["point_id"] == 12, P.KIND] = "bldg"
    spots = spots.with_frame(d, "one building shot")

    model = V.solve_vertical(state.filtered, spots, mode="local",
                             benchmark_id=12, benchmark_elev_ft=100.0)
    assert model.level.elevations[12] == pytest.approx(ft_to_m(100.0), abs=1e-6)
    lawn = F.KindSelect(names=["lawn"]).apply(spots)
    reference = V.spots_with_laser_elevations(lawn, model.level)
    assert 12 not in set(reference.df["point_id"])
    assert len(reference) == len(lawn)


# --- stations (synthetic) ---------------------------------------------------

@pytest.mark.parametrize("value, key", [
    (12, 12), (12.0, 12), ("12", 12), (" 12 ", 12), ("12.0", 12),
    ("p12", "P12"), (" bm1 ", "BM1"), ("", None), (None, None),
    (float("nan"), None),
])
def test_station_names_have_one_canonical_form(value, key):
    assert V.station_key(value) == key


def test_a_station_comes_from_the_attribute_then_the_name_then_the_id():
    import pandas as pd

    ps = P.PointSet(df=pd.DataFrame({
        "e": 0.0, "n": 0.0, "z_ellip_m": 0.0,
        "point_id": pd.array([1, 2, 3, 4, 5], dtype="Int64"),
        "station": ["bm1", None, None, "", 7],
        "feature_name": ["P9", "p12", "bm2", "spot 4", None],
    }))
    assert list(V.stations(ps, marks=["BM2"])) == ["BM1", "P12", "BM2", 4, 7]
    assert list(V.stations(ps)) == ["BM1", "P12", 3, 4, 7]


def test_a_plan_number_and_a_sw_maps_id_are_different_points(state):
    """Plan #1 and SW Maps spot 1 used to be solved as one point, with
    residuals of +/-31.8 in. They are different points and fit exactly."""
    point = state.plan.add_point(449703.0, 4604560.0, setup="0")
    point.rod_in = 20.0
    state.plan_changed()

    model = state.solve_vertical("local")
    assert np.abs(model.level.adjustment.obs_residuals).max() < 1e-6
    assert {1, "P1"} <= set(model.level.elevations)
    assert state.spot_ids()[:2] == [1, 2] and state.spot_ids()[-1] == "P1"


def test_two_readings_on_one_station_add_a_degree_of_freedom(state):
    import pandas as pd

    spots = state.layers["spots"]

    def with_bm1(readings):
        d = spots.df
        extra = d.iloc[[0] * readings].copy()
        extra["point_id"] = pd.array([900 + k for k in range(readings)], dtype="Int64")
        extra[P.STATION] = "BM1"
        extra[P.ROD_IN] = 50.0
        return spots.with_frame(pd.concat([d, extra], ignore_index=True), "bm1")

    once = V.level_network(with_bm1(1))
    twice = V.level_network(with_bm1(2))
    assert "BM1" in twice.elevations
    assert twice.adjustment.dof == once.adjustment.dof + 1


def test_a_plan_station_can_be_the_benchmark(state):
    for k in range(3):
        point = state.plan.add_point(449703.0 + k, 4604560.0, setup="0")
        point.rod_in = 40.0 + k
    state.plan_changed()
    state.set_datum_tie(point="p3", elev_ft=100.0)
    assert state.site.vertical.benchmark_point_id == "P3"

    model = state.solve_vertical("local")
    assert model.level.elevations["P3"] == pytest.approx(ft_to_m(100.0), abs=1e-6)
    # One inch lower on the rod is one inch higher on the ground.
    assert m_to_ft(model.level.elevations["P2"]) == pytest.approx(100.0 + 1 / 12, abs=1e-6)


def test_a_benchmark_typed_as_a_number_is_still_a_sw_maps_id(state):
    state.set_datum_tie(point="7", elev_ft=100.0)
    assert state.site.vertical.benchmark_point_id == 7
    with pytest.raises(ValueError, match="not a point id"):
        state.set_datum_tie(point="BM9", elev_ft=100.0)


def test_differences_added_in_bulk_solve_the_same():
    """The offset solve adds its rows in bulk; the answer is the same."""
    one, many = LeastSquares(), LeastSquares()
    rows = [("b", "a", 1.0), ("c", "b", 2.0), ("c", "a", 3.1)]
    for p, m, v in rows:
        one.add({p: 1.0, m: -1.0}, v)
    many.add_differences([r[0] for r in rows], [r[1] for r in rows],
                         [r[2] for r in rows])
    for ls in (one, many):
        ls.constrain("a", 0.0)
    a, b = one.solve(), many.solve()
    assert a.values == pytest.approx(b.values)
    assert a.dof == b.dof == 1
    assert len(many.components()) == 1
