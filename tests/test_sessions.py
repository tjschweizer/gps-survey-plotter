"""Comparing merged outings instead of pooling them.

Two synthetic outings over the same ground a month apart, the second on a
mount 5 cm higher (see `synthetic.py`). What has to hold:

  * each outing's own repeatability is measured separately, and the step
    between them is measured as a step;
  * hiding an outing takes it off the map and, only when asked, out of the
    surface and the QC - and never out of the datum solve;
  * a hidden outing does not recolour the others, and the choice survives a
    save and reopen.
"""

import pytest

from gpsrtk import qc
from gpsrtk.app import AppState, report, views
from gpsrtk.app.sessions import sessions_payload
from gpsrtk.model.pointset import SESSION
from gpsrtk.site import example_site

FIRST = "Synthetic Yard/2026-08-27"
SECOND = "Outing 2/2026-09-27"


@pytest.fixture
def merged(synthetic_zip, synthetic_outing2, tmp_path):
    st = AppState(site=example_site(), cache_dir=tmp_path / "cache")
    st.load(synthetic_zip)
    st.add_export(synthetic_outing2)
    return st


def _sessions_drawn(state):
    d = views.drawn_points(state)
    return set(d[SESSION].astype(str))


# --- measuring each outing ------------------------------------------------------

def test_each_session_is_measured_against_itself(merged):
    stats = qc.session_crossovers(merged.result)
    assert set(stats["within"]) == {FIRST, SECOND}
    for name in (FIRST, SECOND):
        within = stats["within"][name]
        assert within["n"] > 1000
        assert within["rms"] < 0.03, "each outing is internally consistent"


def test_the_step_between_sessions_is_measured_as_a_step(merged):
    """The pooled RMS mixes a 5 cm step into the noise; split, the step is
    the mean and the noise is the scatter about it."""
    between = qc.session_crossovers(merged.result)["between"]
    (pair, stats), = between.items()
    assert pair == tuple(sorted((FIRST, SECOND)))
    # names sort Outing 2 first, so the difference reads first - second.
    assert stats["bias"] == pytest.approx(-0.05, abs=0.005)
    assert stats["scatter"] < 0.03
    pooled = qc.crossover_stats(merged.result)
    assert pooled["rms"] > 2 * stats["scatter"], "pooling hides which is which"


def test_pairs_read_later_minus_earlier(merged):
    payload = sessions_payload(merged)
    (pair,) = payload["pairs"]
    assert (pair["earlier"], pair["later"]) == (FIRST, SECOND)
    assert pair["diff_cm"] == pytest.approx(5.0, abs=0.5)
    assert pair["after_cm"] is None, "nothing solved yet"


def test_after_solving_the_step_is_gone_and_says_so(merged):
    merged.solve_vertical("ellipsoidal")
    payload = sessions_payload(merged)
    (pair,) = payload["pairs"]
    assert pair["diff_cm"] == pytest.approx(5.0, abs=0.5), "as logged"
    assert abs(pair["after_cm"]) < 0.5, "after the solved offsets"
    offsets = {s["name"]: s["offset_cm"] for s in payload["list"]}
    assert offsets[SECOND] - offsets[FIRST] == pytest.approx(5.0, abs=0.5)


def test_each_session_reports_what_it_logged_and_what_was_kept(merged):
    rows = {s["name"]: s for s in sessions_payload(merged)["list"]}
    for name, row in rows.items():
        assert row["points"] == 8020
        assert 90 < row["fixed_pct"] < 100
        assert 0 < row["kept"] < row["points"], "float is filtered out"
        assert row["minutes"] == pytest.approx(40, abs=1)
        assert row["colour"].startswith("rgb(")


def test_a_single_session_still_gets_its_numbers(state):
    payload = sessions_payload(state)
    assert len(payload["list"]) == 1 and payload["pairs"] == []
    assert payload["list"][0]["rms_cm"] < 3.0


# --- hiding sessions ---------------------------------------------------------------

def test_hiding_a_session_takes_it_off_the_map_only(merged):
    before = len(merged.result)
    merged.set_session_visible(SECOND, False)
    assert _sessions_drawn(merged) == {FIRST}
    assert len(merged.result) == before, "the surface still uses both"
    assert "1 session hidden" in views.points_note(merged)
    packed = views.unpack_points(views.pack_points(merged))
    assert packed["total"] == len(views.drawn_points(merged))


def test_shown_sessions_can_build_the_surface_and_qc_alone(merged):
    both = qc.crossover_stats(merged.result)
    merged.set_sessions_shown([FIRST])
    merged.set_surface_from_shown(True)

    assert set(merged.result.df[SESSION].astype(str)) == {FIRST}
    assert merged.surface.n_points == len(merged.result)
    alone = qc.crossover_stats(merged.result)
    assert alone["rms"] < both["rms"], "without the 5 cm step between them"
    assert "from 1 of 2 sessions" in report.qc_text(merged)
    assert "from 1 of 2 sessions" in views.points_note(merged)
    assert f"sessions: {FIRST}" in report.figure_note(merged)


def test_the_datum_is_solved_from_every_session_regardless(merged):
    """Looking at one outing must never move the datum under the others."""
    merged.set_sessions_shown([FIRST])
    merged.set_surface_from_shown(True)
    model = merged.solve_vertical("ellipsoidal")
    assert set(model.offsets) == {FIRST, SECOND}


def test_the_numbers_describing_a_session_do_not_change_when_it_is_hidden(merged):
    before = {s["name"]: s["rms_cm"] for s in sessions_payload(merged)["list"]}
    merged.set_session_visible(SECOND, False)
    merged.set_surface_from_shown(True)
    after = {s["name"]: s["rms_cm"] for s in sessions_payload(merged)["list"]}
    assert after == before


def test_hiding_a_session_does_not_recolour_the_others(merged):
    """A colour that changed meaning between two views would make comparing
    them worthless."""
    def colour_of(name):
        d = views.drawn_points(merged)
        rgba = views.point_colours(d, "session", sessions=merged.sessions)
        rows = (d[SESSION].astype(str) == name).to_numpy()
        return tuple(rgba[rows][0, :3])

    shown_both = colour_of(FIRST)
    merged.set_session_visible(SECOND, False)
    assert colour_of(FIRST) == shown_both
    palette = views.session_palette(merged.sessions)
    swatch = {s["name"]: s["colour"] for s in sessions_payload(merged)["list"]}
    assert swatch[FIRST] == "rgb({},{},{})".format(*palette[FIRST])


def test_only_this_one_and_show_all(merged):
    merged.set_sessions_shown([SECOND])
    assert merged.hiding == {FIRST}
    merged.set_sessions_shown(merged.sessions)
    assert merged.hiding == set()


def test_hiding_every_session_leaves_nothing_to_surface(merged):
    merged.set_surface_from_shown(True)
    merged.set_sessions_shown([])
    assert len(merged.result) == 0 and merged.surface is None


def test_a_merge_keeps_hidden_sessions_and_a_fresh_load_forgets_them(
        synthetic_zip, synthetic_outing2, tmp_path):
    st = AppState(site=example_site(), cache_dir=tmp_path / "cache")
    st.load(synthetic_zip)
    st.set_session_visible(FIRST, False)
    st.add_export(synthetic_outing2)
    assert st.hiding == {FIRST}, "the new outing arrives shown"
    st.load(synthetic_outing2)
    assert st.hidden_sessions == set()


def test_the_comparison_survives_save_and_reopen(merged, tmp_path):
    merged.set_session_visible(SECOND, False)
    merged.set_surface_from_shown(True)
    before = merged.result.df["z_ellip_m"].to_numpy().copy()
    saved = merged.save_project(tmp_path / "compare.yardproj", {"colormap": "viridis"})

    other = AppState(site=example_site(), cache_dir=merged.cache_dir)
    project, warnings = other.load_project(saved)
    assert not warnings
    assert other.hidden_sessions == {SECOND}
    assert other.surface_from_shown
    assert (other.result.df["z_ellip_m"].to_numpy() == before).all()
    assert project.view["colormap"] == "viridis"


# --- one neighbour search for every readout ---------------------------------------

def test_the_readouts_share_one_neighbour_search(merged, monkeypatch):
    """The QC text and the Sessions panel describe the same points, and the
    neighbour search is the expensive part of both. It runs once."""
    from gpsrtk.web import create_app

    calls = []
    real = qc.crossover_pairs
    monkeypatch.setattr(qc, "crossover_pairs",
                        lambda *a, **k: calls.append(1) or real(*a, **k))
    merged.solve_vertical("ellipsoidal")
    create_app(merged).state.server.snapshot()
    assert len(calls) == 1


def test_shared_pairs_give_the_same_numbers(merged):
    """Reusing the pairs must not move a single figure."""
    merged.solve_vertical("ellipsoidal")
    ps = merged.result
    fresh = qc.format_stats(qc.crossover_stats(ps, column="elev_m"))
    assert fresh in report.qc_text(merged)

    payload = sessions_payload(merged)
    for column in ("elev_m", "z_ellip_m"):
        expected = qc.session_crossovers(merged.corrected, column=column)
        shared = qc.session_crossovers(merged.corrected, column=column,
                                       pairs=merged.crossover_pairs(merged.corrected))
        assert shared == expected
    within = qc.session_crossovers(merged.corrected, column="elev_m")["within"]
    for row in payload["list"]:
        assert row["pairs"] == within[row["name"]]["n"]
