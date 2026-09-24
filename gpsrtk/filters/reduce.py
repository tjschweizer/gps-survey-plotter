"""Reduction stages: collapse many samples into fewer, better ones."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..model.pointset import (PointSet, E, N, Z, ELEV, TIME, TRACK, SOURCE,
                              SESSION, KIND)
from .base import Stage, register


@register
class BinToCell(Stage):
    """Collapse samples to one point per occupied square cell.

    Surfacing does its own binning internally, so this stage is not needed to
    build a raster and stacking both just bins twice. Its real use is the
    export path: process_full.py decimated before writing a Revit points file
    because Revit chokes well before the hundreds of thousands of raw fixes a
    few mowing sessions produce.

    Median is the default statistic. Mean is available but reacts to the
    single-epoch spikes that median ignores.

    Bins are anchored to the site origin, not to the data extent, so the same
    ground always falls in the same cell across sessions.
    """

    kind, label = "bin_to_cell", "bin"

    def __init__(self, cell=0.5, statistic="median", origin_e=0.0,
                 origin_n=0.0, enabled: bool = True):
        super().__init__(enabled)
        self.cell = cell
        self.statistic = statistic
        self.origin_e = origin_e
        self.origin_n = origin_n

    def params(self):
        return {"cell": self.cell, "statistic": self.statistic,
                "origin_e": self.origin_e, "origin_n": self.origin_n}

    def for_site(self, site) -> "BinToCell":
        """Anchor this stage's grid to a site's local origin."""
        self.origin_e = site.origin_e
        self.origin_n = site.origin_n
        return self

    def apply(self, ps: PointSet) -> PointSet:
        if len(ps) == 0:
            return ps
        d = ps.df
        ix = np.floor((d[E].to_numpy() - self.origin_e) / self.cell).astype(np.int64)
        iy = np.floor((d[N].to_numpy() - self.origin_n) / self.cell).astype(np.int64)

        # Every height column present must survive binning. Reducing only the
        # raw ellipsoidal column would silently discard an applied vertical
        # model, and the export would quietly fall back to ellipsoidal heights.
        heights = [c for c in (Z, ELEV) if c in d.columns]
        if not heights:
            raise ValueError("point set carries no height column to reduce")

        cols = {"ix": ix, "iy": iy, E: d[E].to_numpy(), N: d[N].to_numpy()}
        for c in heights:
            cols[c] = d[c].to_numpy()
        work = pd.DataFrame(cols)

        agg = {E: (E, "mean"), N: (N, "mean")}
        for c in heights:
            agg[c] = (c, self.statistic)
        agg["z_sd"] = (heights[0], "std")
        agg["n_samples"] = (heights[0], "size")
        out = work.groupby(["ix", "iy"], sort=False).agg(**agg).reset_index(drop=True)

        # Carry through identity columns where a cell is unambiguous. A cell
        # straddling two sessions has no single session, and labelling it with
        # an arbitrary one would corrupt per-session offset estimation.
        for col in (SESSION, SOURCE, TRACK, KIND):
            if col in d.columns:
                grouped = pd.DataFrame({"ix": ix, "iy": iy, col: d[col].to_numpy()})
                nun = grouped.groupby(["ix", "iy"], sort=False)[col].nunique()
                first = grouped.groupby(["ix", "iy"], sort=False)[col].first()
                out[col] = np.where(nun.to_numpy() == 1, first.to_numpy(), None)

        if TIME in d.columns:
            grouped = pd.DataFrame({"ix": ix, "iy": iy, TIME: d[TIME].to_numpy()})
            out[TIME] = grouped.groupby(["ix", "iy"], sort=False)[TIME].median().to_numpy()

        return ps.with_frame(out, self.describe())

    def describe(self) -> str:
        return f"{self.label}({self.cell} m, {self.statistic})"
