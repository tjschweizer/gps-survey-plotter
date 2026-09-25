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
import pandas as pd
from scipy.spatial import cKDTree

from .model.pointset import PointSet, E, N, Z, TIME, SESSION


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


def pair_diffs(ps: PointSet, pairs: np.ndarray, column: str = Z) -> np.ndarray:
    """`crossover_diffs` from pairs already found, so one neighbour search
    can serve every readout that needs it. Pairs with no height at either
    end are dropped, which is what searching only the rows with a height
    would have found."""
    if len(pairs) == 0 or column not in ps.df.columns:
        return np.empty(0)
    z = ps.df[column].to_numpy(dtype=float)
    dz = z[pairs[:, 0]] - z[pairs[:, 1]]
    return dz[np.isfinite(dz)]


def summarise(dz: np.ndarray) -> dict:
    """RMS, median and p95 of absolute differences, and their mean. Metres."""
    dz = np.asarray(dz, dtype=float)
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


def crossover_stats(ps: PointSet, radius_m: float = 0.30,
                    min_seconds: float = 60.0, column: str = Z) -> dict:
    """Summary of crossover disagreement. All lengths in metres."""
    return summarise(crossover_diffs(ps, radius_m, min_seconds, column))


def session_crossovers(ps: PointSet, radius_m: float = 0.30,
                       min_seconds: float = 60.0, column: str = Z,
                       pairs: np.ndarray | None = None) -> dict:
    """Crossover residuals split by acquisition session.

    Pooling every outing into one RMS hides which outing is good and which
    is not. Split, the same pairs answer two different questions:

      within   pairs from one session measure that outing's repeatability -
               the number to compare outings by. The sign of each difference
               depends only on pair order, so the mean means nothing here.
      between  pairs from two sessions measure how far apart the outings sit.
               The mean is the offset between them (the later-named minus the
               earlier-named); the scatter about the mean says how well one
               constant describes it.

    Rows with no height in `column` are left out: a laser rod shot has no
    GNSS height and can never be half of a crossover.

    `pairs`, when given, are `crossover_pairs` of `ps` found already with
    the same radius and separation; they are reused rather than searched
    for again.
    """
    out: dict = {"within": {}, "between": {}}
    d = ps.df
    if SESSION not in d.columns or len(d) < 2:
        return out
    if column in d.columns:
        finite = np.isfinite(d[column].to_numpy(dtype=float))
        if pairs is not None:
            # Renumber the pairs onto the rows that survive.
            keep = finite[pairs[:, 0]] & finite[pairs[:, 1]]
            new_index = np.cumsum(finite) - 1
            pairs = new_index[pairs[keep]]
        ps = ps.select(finite, f"has {column}")
        d = ps.df
    names = d[SESSION].astype(str).to_numpy()
    for name in sorted(set(names)):
        out["within"][name] = {"n": 0}
    if len(d) < 2 or column not in d.columns:
        return out

    if pairs is None:
        pairs = crossover_pairs(ps, radius_m, min_seconds)
    if len(pairs) == 0:
        return out
    z = d[column].to_numpy(dtype=float)
    i, j = pairs[:, 0], pairs[:, 1]
    same = names[i] == names[j]

    for name in out["within"]:
        m = same & (names[i] == name)
        out["within"][name] = summarise(z[i[m]] - z[j[m]])

    cross = ~same
    first_is_i = names[i] < names[j]
    lo = np.where(first_is_i, i, j)[cross]
    hi = np.where(first_is_i, j, i)[cross]
    frame = pd.DataFrame({"a": names[lo], "b": names[hi], "dz": z[hi] - z[lo]})
    for (a, b), part in frame.groupby(["a", "b"], sort=True):
        stats = summarise(part["dz"].to_numpy())
        stats["scatter"] = float(part["dz"].std(ddof=0))
        out["between"][(str(a), str(b))] = stats
    return out


def format_stats(s: dict, unit: str = "cm") -> str:
    if not s.get("n"):
        return "no crossover pairs found"
    k = 100.0 if unit == "cm" else 1.0
    return (f"{s['n']:,} pairs  |  RMS {s['rms'] * k:.2f} {unit}  "
            f"median |d| {s['median_abs'] * k:.2f} {unit}  "
            f"p95 {s['p95_abs'] * k:.2f} {unit}")
