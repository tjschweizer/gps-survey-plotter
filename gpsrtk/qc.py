"""Measured quality control.

CLAUDE.md is explicit that measured QC beats the receiver's own accuracy
estimates, which are optimistic for float by roughly 3x and correlate with
actual error only 0.44. So the numbers that matter here come from the data
disagreeing with itself, not from the `hacc`/`vacc` columns.

Crossovers are the workhorse: where the rover passed near the same ground at
two well-separated times, the elevation difference is error, because the ground
did not move. The time separation is what makes it meaningful - two points 0.2 s
apart share the same atmosphere, the same satellites, and the same multipath,
so they agree with each other far better than either agrees with the truth.
"""

from __future__ import annotations

import numpy as np
from scipy.spatial import cKDTree

from .model.pointset import PointSet, E, N, Z, TIME


def crossover_pairs(ps: PointSet, radius_m: float = 0.30,
                    min_seconds: float = 60.0) -> np.ndarray:
    """Index pairs of near-coincident, well-separated observations.

    Returns an (n, 2) array of row indices into `ps.df`. Shared by the QC
    statistics and by the session-offset solver, so both are talking about the
    same geometry.
    """
    d = ps.df
    if len(d) < 2:
        return np.empty((0, 2), dtype=int)

    tree = cKDTree(d[[E, N]].to_numpy())
    pairs = tree.query_pairs(radius_m, output_type="ndarray")
    if len(pairs) == 0:
        return np.empty((0, 2), dtype=int)

    if TIME in d.columns and min_seconds:
        t = d[TIME].to_numpy().astype("datetime64[ns]").astype(np.int64) / 1e9
        dt = np.abs(t[pairs[:, 0]] - t[pairs[:, 1]])
        pairs = pairs[dt > min_seconds]
    return pairs


def crossover_diffs(ps: PointSet, radius_m: float = 0.30,
                    min_seconds: float = 60.0,
                    column: str = Z) -> np.ndarray:
    """Elevation differences between near-coincident, well-separated passes.

    Returns signed differences in metres. Defaults reproduce the definition
    behind the established figures in CLAUDE.md: pairs within 30 cm, more than
    60 s apart.
    """
    pairs = crossover_pairs(ps, radius_m, min_seconds)
    if len(pairs) == 0:
        return np.empty(0)
    z = ps.df[column].to_numpy()
    return z[pairs[:, 0]] - z[pairs[:, 1]]


def crossover_stats(ps: PointSet, radius_m: float = 0.30,
                    min_seconds: float = 60.0, column: str = Z) -> dict:
    """Summary of crossover disagreement. All lengths in metres."""
    dz = crossover_diffs(ps, radius_m, min_seconds, column)
    if len(dz) == 0:
        return {"n": 0}
    a = np.abs(dz)
    return {
        "n": int(len(dz)),
        "rms": float(np.sqrt(np.mean(dz ** 2))),
        "median_abs": float(np.median(a)),
        "p95_abs": float(np.percentile(a, 95)),
        "bias": float(np.mean(dz)),
    }


def format_stats(s: dict, unit: str = "cm") -> str:
    if not s.get("n"):
        return "no crossover pairs found"
    k = 100.0 if unit == "cm" else 1.0
    return (f"{s['n']:,} pairs  |  RMS {s['rms'] * k:.2f} {unit}  "
            f"median |d| {s['median_abs'] * k:.2f} {unit}  "
            f"p95 {s['p95_abs'] * k:.2f} {unit}")
