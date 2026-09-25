"""Combining survey exports into one working data set.

Merging is not concatenation. Two outings of the same lawn do not share a
vertical datum: the antenna was mounted once per outing, the base was selected
once, the rod was measured once, and every one of those carries a constant
error that differs between visits. Stack them naively and the surface steps at
the boundary between them.

What makes the step removable is OVERLAP - ground covered on both visits. Where
two sessions pass within a few centimetres of the same spot, the height
difference is the offset between them, because the ground did not move. Without
overlap the offset is not merely hard to find, it is not determined by anything,
and no amount of later processing recovers it.

So the job of this module is to merge the points and then say plainly which
sessions can be reconciled and which cannot. `describe_sessions` is what turns
a silent stack of numbers into a statement you can act on before the next
outing, while it is still possible to go and walk the overlap.

The offsets themselves are solved in `vertical.session_offsets`; this module
only measures whether that solve has anything to work with.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import qc
from .model.pointset import (E, FIX, FIX_FLOAT, FIX_RTK, KIND, N, ROD_IN,
                             SESSION, SOURCE, TIME, Z, PointSet, concat)

# Overlap is judged on the same geometry the offset solver uses, so a session
# this module calls linked is one the solver can actually tie: two moving
# points within 30 cm, or a static shot within 1 m of anything. A static shot
# was taken standing still on the spot, so its position is good to a few
# centimetres however far it is from the nearest pass, and the ground within
# a metre of it is the same ground on a lawn.
RADIUS_M = 0.30
STATIC_RADIUS_M = 1.0
MIN_SECONDS = 60.0

# Below this, an offset is being fitted to a handful of pairs that could all be
# from one patch of ground and one pass. It is not nothing, but it should not
# be presented as a solved datum.
THIN_OVERLAP = 25


@dataclass
class SessionInfo:
    """One acquisition session, as found in the merged data."""

    name: str
    points: int
    fixed: int
    floated: int
    start: object = None
    end: object = None
    sources: tuple[str, ...] = ()

    @property
    def date(self) -> str:
        """The date the session started, from its label.

        A later session on the same day is labelled "YYYY-MM-DD HH:MM", so
        the date is the first ten characters.
        """
        tail = self.name.rsplit("/", 1)[-1]
        return tail[:10] if tail[:2].isdigit() else ""

    @property
    def fixed_fraction(self) -> float:
        return self.fixed / self.points if self.points else 0.0

    def describe(self) -> str:
        bits = [f"{self.points:,} points"]
        if self.fixed or self.floated:
            bits.append(f"{self.fixed_fraction * 100:.0f}% fixed")
        if self.start is not None and self.end is not None:
            try:
                minutes = (self.end - self.start).total_seconds() / 60.0
                bits.append(f"{minutes:.0f} min")
            except (TypeError, AttributeError):
                pass
        return f"{self.name}  —  " + ", ".join(bits)


@dataclass
class MergeReport:
    """What a merge did, and whether the result can be reconciled vertically."""

    layers: dict[str, tuple[int, int]] = field(default_factory=dict)
    sessions: list[SessionInfo] = field(default_factory=list)
    overlaps: dict[tuple[str, str], int] = field(default_factory=dict)
    unlinked: list[str] = field(default_factory=list)
    thin: list[tuple[str, str, int]] = field(default_factory=list)
    reprojected: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    static_shots: int = 0         # static shots the overlap was judged with

    @property
    def added(self) -> int:
        return sum(after - before for before, after in self.layers.values())

    @property
    def reconcilable(self) -> bool:
        """Whether every session can be tied to the others."""
        return len(self.sessions) < 2 or not self.unlinked

    def describe(self) -> str:
        lines = []
        if self.layers:
            lines.append("Layers:")
            for name, (before, after) in self.layers.items():
                gained = after - before
                lines.append(f"  {name}: {before:,} + {gained:,} = {after:,}"
                             if before else f"  {name}: {after:,} (new)")

        if self.sessions:
            lines += ["", f"Sessions ({len(self.sessions)}):"]
            lines += [f"  {s.describe()}" for s in self.sessions]

        if len(self.sessions) > 1:
            within = f"within {RADIUS_M * 100:.0f} cm"
            if self.static_shots:
                within += (f", or {STATIC_RADIUS_M:g} m of one of "
                           f"{self.static_shots:,} static shots")
            lines += ["", f"Overlap between sessions (crossover pairs {within}):"]
            if self.overlaps:
                for (a, b), count in sorted(self.overlaps.items(),
                                            key=lambda kv: -kv[1]):
                    lines.append(f"  {a}  <->  {b}:  {count:,}")
            else:
                lines.append("  none")

        for a, b, count in self.thin:
            lines += ["", f"WARNING: {a} and {b} share only {count} crossover "
                          "pairs. That is enough to compute an offset and not "
                          "enough to trust it — it may all come from one patch "
                          "of ground on one pass."]

        if self.unlinked:
            lines += ["", "CANNOT BE RECONCILED:"]
            for name in self.unlinked:
                lines.append(f"  {name} overlaps no other session.")
            lines += ["",
                      "Its vertical offset is not determined by anything in "
                      "this data, and nothing done later recovers it. Either "
                      "re-walk some ground that another session also covers, "
                      "or keep it out of the surface."]

        if self.reprojected:
            lines += ["", "Re-projected into the site's CRS: "
                          + ", ".join(self.reprojected)]
        lines += ["", *self.notes] if self.notes else []
        return "\n".join(lines).strip()


# --- merging --------------------------------------------------------------

def merge_layers(existing: dict[str, PointSet],
                 incoming: dict[str, PointSet]) -> tuple[dict, dict]:
    """Combine two sets of layers by name, keeping the union of columns.

    Layers are matched by name because that is what they mean: `track_points`
    from two outings is the same kind of observation of the same ground. A
    layer only one side has is carried through untouched.

    Returns the merged layers and, per layer, the (before, after) counts.
    """
    merged = dict(existing)
    counts: dict[str, tuple[int, int]] = {}
    for name, ps in incoming.items():
        before = len(merged[name]) if name in merged else 0
        if name in merged:
            merged[name] = concat([merged[name], ps], layer=name)
        else:
            merged[name] = ps
        counts[name] = (before, len(merged[name]))
    return merged, counts


def reproject(ps: PointSet, epsg: int) -> PointSet:
    """Re-derive E/N from lat/lon in the given CRS.

    Needed when a reader projected into a different zone than the site uses -
    the `.swmz` reader picks the zone from the data, because a reader is handed
    a file and has no site. Reprojecting from lat/lon rather than transforming
    E/N avoids a second rounding.
    """
    from pyproj import Transformer

    from .model.pointset import E, LAT, LON, N

    if not ps.has(LAT, LON):
        raise ValueError(
            f"layer '{ps.layer}' carries no lat/lon, so its coordinates "
            "cannot be re-derived in another CRS")
    tx = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    df = ps.df.copy()
    df[E], df[N] = tx.transform(df[LON].to_numpy(), df[LAT].to_numpy())
    return ps.with_frame(df, f"reproject -> EPSG:{epsg}")


def crs_disagreement_m(ps: PointSet, epsg: int) -> float:
    """How far the stored E/N sits from lat/lon projected into `epsg`.

    A few millimetres means the reader used the same CRS and simply rounded.
    Kilometres means a different zone, and every distance computed across the
    join would be wrong. Returns 0.0 when there is no lat/lon to check against,
    because an unverifiable claim is not a disagreement.
    """
    from pyproj import Transformer

    from .model.pointset import E, LAT, LON, N

    if not ps.has(LAT, LON) or len(ps) == 0:
        return 0.0
    d = ps.df
    good = d[LAT].notna() & d[LON].notna() & d[E].notna() & d[N].notna()
    if not good.any():
        return 0.0
    sample = d.loc[good].head(500)
    tx = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    e, n = tx.transform(sample[LON].to_numpy(), sample[LAT].to_numpy())
    return float(np.max(np.hypot(e - sample[E].to_numpy(),
                                 n - sample[N].to_numpy())))


# --- diagnosis ------------------------------------------------------------

def describe_sessions(ps: PointSet) -> list[SessionInfo]:
    """One record per session present in a point set."""
    if SESSION not in ps.df.columns or len(ps) == 0:
        return []
    out = []
    for name, part in ps.df.groupby(ps.df[SESSION].astype(str), sort=True):
        fix = part[FIX] if FIX in part.columns else pd.Series(dtype=float)
        start = end = None
        if TIME in part.columns and part[TIME].notna().any():
            start, end = part[TIME].min(), part[TIME].max()
        out.append(SessionInfo(
            name=str(name),
            points=len(part),
            fixed=int((fix == FIX_RTK).sum()),
            floated=int((fix == FIX_FLOAT).sum()),
            start=start, end=end,
            sources=tuple(sorted(map(str, part[SOURCE].dropna().unique())))
            if SOURCE in part.columns else (),
        ))
    return out


def static_rows(df: pd.DataFrame) -> np.ndarray:
    """Rows that are static shots: anything with a rod reading or a `type`.

    Continuous logging carries neither; a recorded shot carries one or both.
    """
    static = np.zeros(len(df), dtype=bool)
    for column in (ROD_IN, KIND):
        if column in df.columns:
            static |= df[column].notna().to_numpy()
    return static


def overlap_pairs(ps: PointSet, *, radius_m: float = RADIUS_M,
                  static_radius_m: float = STATIC_RADIUS_M,
                  min_seconds: float = MIN_SECONDS) -> np.ndarray:
    """Row pairs that count as overlap: the one definition of it.

    Two moving points pair within `radius_m`; a static shot pairs with any
    point within `static_radius_m`. Both must be more than `min_seconds`
    apart. Returns an (n, 2) array of row indices into `ps.df`, each pair
    once. The merge report counts these and `vertical.session_offsets`
    solves from them, so the two cannot disagree about which sessions are
    linked.
    """
    from scipy.spatial import cKDTree

    d = ps.df
    if len(d) < 2:
        return np.empty((0, 2), dtype=int)
    static = static_rows(d)
    out = []

    moving = np.flatnonzero(~static)
    if len(moving) >= 2:
        sub = qc.crossover_pairs(ps.select(~static, "moving"), radius_m,
                                 min_seconds)
        out.append(moving[sub])

    shots = np.flatnonzero(static)
    if len(shots) and static_radius_m:
        xy = d[[E, N]].to_numpy(dtype=float)
        near = cKDTree(xy).query_ball_point(xy[shots], static_radius_m)
        i = np.repeat(shots, [len(n) for n in near])
        j = (np.concatenate([np.asarray(n, dtype=int) for n in near])
             if len(i) else np.empty(0, dtype=int))
        # A pair of two static shots is found from both ends; keep one.
        keep = (i != j) & ~(static[j] & (j < i))
        i, j = i[keep], j[keep]
        if TIME in d.columns and min_seconds:
            t = d[TIME].to_numpy().astype("datetime64[ns]").astype(np.int64) / 1e9
            far = np.abs(t[i] - t[j]) > min_seconds
            i, j = i[far], j[far]
        out.append(np.column_stack([i, j]))

    out = [p for p in out if len(p)]
    return np.vstack(out).astype(int) if out else np.empty((0, 2), dtype=int)


def session_overlap(ps: PointSet, *, radius_m: float = RADIUS_M,
                    static_radius_m: float = STATIC_RADIUS_M,
                    min_seconds: float = MIN_SECONDS,
                    column: str = Z) -> dict[tuple[str, str], int]:
    """Count overlap pairs between each pair of DIFFERENT sessions.

    This is exactly the evidence `vertical.session_offsets` fits its offsets
    to (`overlap_pairs`), counted rather than solved. Points with no height in
    `column` are dropped first, because a laser rod shot reaches the datum
    through the level network and can never contribute an overlap here -
    leaving them in would make a session look better connected than it is.
    """
    if SESSION not in ps.df.columns or len(ps) < 2:
        return {}
    if column in ps.df.columns:
        ps = ps.select(ps.df[column].notna().to_numpy(), f"has {column}")
    if len(ps) < 2:
        return {}

    sessions = ps.df[SESSION].astype(str).to_numpy()
    pairs = overlap_pairs(ps, radius_m=radius_m,
                          static_radius_m=static_radius_m,
                          min_seconds=min_seconds)
    counts: dict[tuple[str, str], int] = {}
    for i, j in pairs:
        a, b = sessions[i], sessions[j]
        if a == b:
            continue
        key = (a, b) if a < b else (b, a)
        counts[key] = counts.get(key, 0) + 1
    return counts


def unlinked_sessions(names, overlaps) -> list[str]:
    """Sessions with no chain of overlap to the largest connected group.

    Connectivity is transitive: A tied to B and B tied to C puts C on the same
    datum as A without A and C ever having shared ground. What cannot be
    rescued is a session in no group at all, or in a group disjoint from the
    main one.
    """
    names = sorted(set(map(str, names)))
    if len(names) < 2:
        return []

    parent = {n: n for n in names}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for (a, b), count in overlaps.items():
        if count and a in parent and b in parent:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

    groups: dict[str, list[str]] = {}
    for n in names:
        groups.setdefault(find(n), []).append(n)
    if not groups:
        return []
    main = max(groups.values(), key=len)
    return sorted(n for n in names if n not in main)


def instrument_height_notes(layers: dict[str, PointSet], name: str) -> list[str]:
    """Warn when SW Maps' `Instrument Ht` was set, since it is never applied.

    Heights here are antenna heights: the session offsets and the datum tie
    absorb the height of the antenna above ground. The column is read and
    carried, but nothing subtracts it, so an export where it was set would
    mix conventions with every export where it was not - silently, unless it
    is said here. A value that changes within a session is worth saying
    separately: it records a mount change the heights themselves carry.
    """
    from .model.pointset import ANT_HT

    set_in: list[str] = []
    varies: list[str] = []
    for label, ps in layers.items():
        d = ps.df
        if ANT_HT not in d.columns:
            continue
        values = pd.to_numeric(d[ANT_HT], errors="coerce")
        if not (values.fillna(0.0) != 0.0).any():
            continue
        shown = sorted({round(float(v), 3) for v in values.dropna().unique()})
        set_in.append(f"{label} ({', '.join(f'{v:g}' for v in shown[:5])}"
                      f"{', ...' if len(shown) > 5 else ''} m)")
        if SESSION in d.columns:
            for session, part in values.groupby(d[SESSION].astype(str)):
                if part.dropna().nunique() > 1:
                    varies.append(f"{session} in {label} "
                                  f"({part.min():g} to {part.max():g} m)")
    if not set_in:
        return []
    notes = [f"NOTE: Instrument Ht is set in {name}: " + "; ".join(set_in)
             + ". It is not applied. Heights stay antenna heights, and the "
             "session offsets and the datum tie absorb the antenna height, "
             "so mixing this export with one where it was not set is safe "
             "only because the value is ignored."]
    if varies:
        notes.append("NOTE: Instrument Ht changes within a session: "
                     + "; ".join(varies) + ". A mount that changed height "
                     "mid-session steps the heights, and one offset per "
                     "session cannot remove it.")
    return notes


def diagnose(ps: PointSet, *, overlap: PointSet | None = None,
             **kw) -> MergeReport:
    """Sessions, overlaps, and what cannot be reconciled — without merging.

    `ps` is what was logged, and is what the sessions are described from.
    `overlap`, when given, is what the overlap is judged on instead - the
    filtered points the offset solver actually sees.
    """
    report = MergeReport()
    report.sessions = describe_sessions(ps)
    if len(report.sessions) > 1:
        basis = overlap if overlap is not None else ps
        report.overlaps = session_overlap(basis, **kw)
        column = kw.get("column", Z)
        has = (basis.df[column].notna().to_numpy() if column in basis.df.columns
               else np.ones(len(basis), dtype=bool))
        report.static_shots = int((static_rows(basis.df) & has).sum())
        report.unlinked = unlinked_sessions(
            [s.name for s in report.sessions], report.overlaps)
        report.thin = [(a, b, c) for (a, b), c in sorted(report.overlaps.items())
                       if 0 < c < THIN_OVERLAP]
    return report
