"""Selection stages: keep or drop rows by attribute, time, track, or area."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..model.pointset import (PointSet, E, N, FIX, FIX_RTK, TIME, TRACK,
                              SESSION, KIND)
from .base import Stage, register


@register
class FixSelect(Stage):
    """Keep only the listed solution qualities.

    Default is RTK fixed only, which CLAUDE.md makes the standing rule for
    surfaces unless float handling is explicitly being tested.
    """

    kind, label = "fix_select", "fix"

    def __init__(self, values=(FIX_RTK,), enabled: bool = True):
        super().__init__(enabled)
        self.values = tuple(values)

    def params(self):
        return {"values": list(self.values)}

    def apply(self, ps: PointSet) -> PointSet:
        if FIX not in ps.df:
            return ps
        return ps.select(ps.df[FIX].isin(self.values).to_numpy(),
                         f"fix in {self.values}")


class _NameSelect(Stage):
    """Shared implementation for keep/drop-by-name filters."""

    column = ""

    def __init__(self, names=(), exclude: bool = False, enabled: bool = True):
        super().__init__(enabled)
        self.names = tuple(names)
        self.exclude = exclude

    def params(self):
        return {"names": list(self.names), "exclude": self.exclude}

    def apply(self, ps: PointSet) -> PointSet:
        if self.column not in ps.df or not self.names:
            return ps
        m = ps.df[self.column].isin(self.names).to_numpy()
        return ps.select(~m if self.exclude else m, self.describe())


@register
class TrackSelect(_NameSelect):
    """Keep or drop named tracks/layers."""

    kind, label, column = "track_select", "track", TRACK


@register
class SessionSelect(_NameSelect):
    """Keep or drop whole acquisition sessions."""

    kind, label, column = "session_select", "session", SESSION


@register
class KindSelect(_NameSelect):
    """Keep or drop by user-assigned classification: lawn, bldg, and so on."""

    kind, label, column = "kind_select", "kind", KIND


@register
class TimeWindow(Stage):
    """Keep points inside a time range. Bounds are ISO strings or None."""

    kind, label = "time_window", "time"

    def __init__(self, start=None, end=None, enabled: bool = True):
        super().__init__(enabled)
        self.start = start
        self.end = end

    def params(self):
        return {"start": self.start, "end": self.end}

    def apply(self, ps: PointSet) -> PointSet:
        if TIME not in ps.df:
            return ps
        t = ps.df[TIME]
        m = np.ones(len(t), bool)
        if self.start:
            m &= (t >= pd.Timestamp(self.start)).to_numpy()
        if self.end:
            m &= (t <= pd.Timestamp(self.end)).to_numpy()
        return ps.select(m, self.describe())


@register
class PolygonSelect(Stage):
    """Keep or drop points inside a polygon.

    This is how the house footprint, the driveway, and flower beds get excluded
    from a terrain surface. CLAUDE.md is explicit that interpolating across the
    house and presenting the result as terrain is not acceptable. The distance
    mask in surfacing catches most of it, but only an explicit polygon catches
    ground that really was traversed and really is not lawn.

    Vertices are [[e, n], ...] in projected coordinates.
    """

    kind, label = "polygon_select", "polygon"

    def __init__(self, vertices=(), exclude: bool = True, enabled: bool = True):
        super().__init__(enabled)
        self.vertices = [list(map(float, v)) for v in vertices]
        self.exclude = exclude

    def params(self):
        return {"vertices": self.vertices, "exclude": self.exclude}

    def describe(self) -> str:
        verb = "outside" if self.exclude else "inside"
        return f"{self.label}({verb}, {len(self.vertices)} vertices)"

    def apply(self, ps: PointSet) -> PointSet:
        if len(self.vertices) < 3:
            return ps
        from matplotlib.path import Path

        inside = Path(np.asarray(self.vertices)).contains_points(
            ps.df[[E, N]].to_numpy())
        return ps.select(~inside if self.exclude else inside, self.describe())
