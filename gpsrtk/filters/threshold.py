"""Threshold stages: drop rows whose quality or motion metrics fall outside
acceptable bounds."""

from __future__ import annotations

import numpy as np

from ..model.pointset import PointSet, E, N, Z, HACC, VACC, SPEED, PDOP
from .base import Stage, register


class _Range(Stage):
    """Shared implementation for simple numeric range filters."""

    column = ""

    def __init__(self, minimum=None, maximum=None, enabled: bool = True):
        super().__init__(enabled)
        self.minimum = minimum
        self.maximum = maximum

    def params(self):
        return {"minimum": self.minimum, "maximum": self.maximum}

    def apply(self, ps: PointSet) -> PointSet:
        if self.column not in ps.df:
            return ps
        v = ps.df[self.column].to_numpy()
        m = np.ones(len(v), bool)
        if self.minimum is not None:
            m &= v > self.minimum
        if self.maximum is not None:
            m &= v <= self.maximum
        return ps.select(m, self.describe())


@register
class SpeedThreshold(_Range):
    """Drop slow points.

    CLAUDE.md: above 0.9 m/s gives 4.13 cm RMS while keeping 62% of points.
    Note the caveat recorded there - slow points cluster at turns near trees,
    so this partly selects for open sky rather than purely for steady motion.
    """

    kind, label, column = "speed_threshold", "speed", SPEED

    def __init__(self, minimum=0.9, maximum=None, enabled: bool = True):
        super().__init__(minimum, maximum, enabled)


@register
class PDOPThreshold(_Range):
    """Drop points collected under poor satellite geometry."""

    kind, label, column = "pdop_threshold", "pdop", PDOP

    def __init__(self, minimum=None, maximum=3.0, enabled: bool = True):
        super().__init__(minimum, maximum, enabled)


@register
class AccuracyThreshold(Stage):
    """Drop points whose reported accuracy exceeds limits.

    Use with care. CLAUDE.md records that reported accuracy is optimistic for
    float by roughly 3x, correlating with actual error only 0.44, so this is a
    coarse screen and not a substitute for crossover QC.
    """

    kind, label = "accuracy_threshold", "accuracy"

    def __init__(self, max_h=0.05, max_v=0.10, enabled: bool = True):
        super().__init__(enabled)
        self.max_h = max_h
        self.max_v = max_v

    def params(self):
        return {"max_h": self.max_h, "max_v": self.max_v}

    def apply(self, ps: PointSet) -> PointSet:
        m = np.ones(len(ps), bool)
        if HACC in ps.df and self.max_h is not None:
            m &= ps.df[HACC].to_numpy() <= self.max_h
        if VACC in ps.df and self.max_v is not None:
            m &= ps.df[VACC].to_numpy() <= self.max_v
        return ps.select(m, self.describe())


@register
class PercentileDespike(Stage):
    """Clip gross elevation outliers by percentile.

    Deliberately blunt and symmetric, matching the archive scripts. It removes
    single-epoch blunders, not systematic error: a whole float episode sitting
    30 cm low passes through this untouched.

    A row with no height at all is dropped here too, and does not take part
    in the percentiles. One blank elevation in an export used to make both
    percentiles NaN, and every comparison against NaN is false - so the stage
    silently removed every point.
    """

    kind, label = "percentile_despike", "despike"

    def __init__(self, low=0.2, high=99.8, enabled: bool = True):
        super().__init__(enabled)
        self.low = low
        self.high = high

    def params(self):
        return {"low": self.low, "high": self.high}

    def apply(self, ps: PointSet) -> PointSet:
        if len(ps) == 0:
            return ps
        z = ps.df[Z].to_numpy(dtype=float)
        have = np.isfinite(z)
        if not have.any():
            return ps.select(have, self.describe())
        lo, hi = np.percentile(z[have], [self.low, self.high])
        return ps.select(have & (z >= lo) & (z <= hi), self.describe())


@register
class SurfaceResidual(Stage):
    """Drop points disagreeing with a reference surface by more than a limit.

    The reference is normally the laser-shot spots, which are independent of
    the GNSS heights and far better vertically. This is the stage that turns
    "the laser says the ground is here" into an actual filter on walked data.

    The reference is attached at runtime rather than serialised, because it is
    another layer in the project, not a parameter value.
    """

    kind, label = "surface_residual", "residual"

    def __init__(self, max_residual_m=0.15, enabled: bool = True):
        super().__init__(enabled)
        self.max_residual_m = max_residual_m
        self._reference = None

    def params(self):
        return {"max_residual_m": self.max_residual_m}

    def set_reference(self, reference: PointSet) -> "SurfaceResidual":
        self._reference = reference
        return self

    def apply(self, ps: PointSet) -> PointSet:
        if self._reference is None or len(self._reference) < 3:
            return ps
        from scipy.interpolate import griddata

        ref = self._reference.df
        zi = griddata(ref[[E, N]].to_numpy(), ref[Z].to_numpy(),
                      ps.df[[E, N]].to_numpy(), method="linear")
        resid = np.abs(ps.df[Z].to_numpy() - zi)
        # Points outside the reference hull come back NaN. Keep them, rather
        # than silently deleting everything the laser never covered.
        keep = np.isnan(resid) | (resid <= self.max_residual_m)
        return ps.select(keep, self.describe())
