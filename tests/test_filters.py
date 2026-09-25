"""Filter chain behaviour: ordering, caching, accounting, serialisation."""

import json

import pytest

from gpsrtk import filters as F
from gpsrtk.model import pointset as P


def test_default_chain_keeps_only_fixed(tracks):
    chain = F.default_chain()
    out = chain.run(tracks)
    assert (out.df[P.FIX] == P.FIX_RTK).all()
    assert 0 < len(out) < len(tracks)


def test_disabled_stage_is_skipped_but_reported(tracks):
    chain = F.default_chain()
    chain.run(tracks)
    speed = [r for r in chain.results if r.label.startswith("speed")][0]
    assert not speed.enabled
    # A disabled stage must not change the count.
    assert speed.n_in == speed.n_out


def test_enabling_a_stage_changes_the_result(tracks):
    chain = F.default_chain()
    before = len(chain.run(tracks))
    chain.stages[2].enabled = True
    after = len(chain.run(tracks))
    assert after < before


def test_cache_reuse_is_transparent(tracks):
    """Re-running after toggling a late stage must give the same answer as a
    cold chain. Caching is an optimisation, never a semantic difference."""
    warm = F.default_chain()
    warm.run(tracks)
    warm.stages[2].enabled = True
    warm_out = warm.run(tracks)

    cold = F.default_chain()
    cold.stages[2].enabled = True
    cold_out = cold.run(tracks)

    assert len(warm_out) == len(cold_out)
    assert warm_out.df[P.Z].sum() == pytest.approx(cold_out.df[P.Z].sum())


def test_despike_survives_a_missing_height(state):
    """One blank elevation must cost one point, not the whole layer."""
    import numpy as np

    d = state.source.df.copy()
    d.loc[5, P.Z] = np.nan
    ps = state.source.with_frame(d, "one blank height")
    out = F.PercentileDespike().apply(ps)
    clean = F.PercentileDespike().apply(state.source)
    assert len(out) >= len(clean) - 2
    assert out.df[P.Z].notna().all()


def test_chain_round_trips_through_json(tracks):
    chain = F.default_chain()
    chain.stages[2].enabled = True
    expected = len(chain.run(tracks))

    restored = F.FilterChain.from_list(json.loads(json.dumps(chain.to_list())))
    assert len(restored.run(tracks)) == expected


def test_every_registered_stage_round_trips():
    """A stage that cannot be saved silently breaks project reproducibility."""
    for kind, cls in F.REGISTRY.items():
        stage = cls()
        clone = F.Stage.from_dict(json.loads(json.dumps(stage.to_dict())))
        assert clone.to_dict() == stage.to_dict(), kind


def test_polygon_exclusion(tracks, site):
    """Excluding an area must remove points, and inverting must be complementary."""
    d = tracks.df
    e0, n0 = d[P.E].median(), d[P.N].median()
    box = [[e0 - 5, n0 - 5], [e0 + 5, n0 - 5], [e0 + 5, n0 + 5], [e0 - 5, n0 + 5]]

    out = F.PolygonSelect(vertices=box, exclude=True).apply(tracks)
    inside = F.PolygonSelect(vertices=box, exclude=False).apply(tracks)
    assert len(out) + len(inside) == len(tracks)
    assert 0 < len(inside) < len(tracks)


def test_bin_to_cell_matches_surfacing_binning(tracks, site):
    """The export path and the surfacing path must agree on the grid, or a
    Revit export will not sit on its own heightmap."""
    from gpsrtk.surface import bin_cells

    fixed = tracks.fixed
    staged = F.BinToCell(cell=0.5).for_site(site).apply(fixed)
    internal = bin_cells(fixed, 0.5, site)
    assert len(staged) == len(internal)


def test_bin_leaves_mixed_session_cells_unlabelled(tracks, site):
    """A cell straddling two sessions has no single session; labelling it with
    an arbitrary one would corrupt per-session offset estimation."""
    import pandas as pd

    d = tracks.fixed.df.copy()
    # Force two sessions to overlap on the same ground.
    d[P.SESSION] = ["a" if i % 2 else "b" for i in range(len(d))]
    mixed = tracks.fixed.with_frame(d, "synthetic sessions")

    out = F.BinToCell(cell=2.0).for_site(site).apply(mixed)
    assert out.df[P.SESSION].isna().any()


def test_a_new_source_of_the_same_size_is_filtered_afresh(state):
    """The prefix cache was keyed on id(), which Python hands out again once
    an object is collected. A new export of the same size at a reused
    address was served the old export's result. It is keyed on a token now,
    which is never reused."""
    import gc

    tracks = state.layers["track_points"]
    chain = F.default_chain()
    first = tracks.with_frame(tracks.df.copy(), "all fixed")
    first.df[P.FIX] = P.FIX_RTK
    kept_before = len(chain.run(first))
    address = id(first)
    del first
    gc.collect()

    # Try to land the replacement at the same address, as happens in
    # practice; the answer must be right whether or not it does.
    for _ in range(50):
        second = tracks.with_frame(tracks.df.copy(), "all float")
        second.df[P.FIX] = P.FIX_FLOAT
        if id(second) == address:
            break
    assert kept_before > 0
    assert len(chain.run(second)) == 0


def test_every_point_set_has_its_own_token(state):
    tracks = state.layers["track_points"]
    tokens = {tracks.token, tracks.select(tracks.df[P.FIX] == 4).token,
              tracks.with_frame(tracks.df, "same frame").token}
    assert len(tokens) == 3
