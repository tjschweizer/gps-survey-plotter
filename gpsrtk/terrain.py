"""What the surface says about the ground: slope, drainage, contours.

All of it is derived from a built `Surface`, which is already binned, masked
and smoothed. Two rules carry over from surfacing:

  Nothing is reported for unmeasured cells. Gradients are taken on the filled
  grid so that measured cells at the edge of the survey still get one, then
  the mask is applied - a slope over the house footprint is interpolation of
  interpolation, and drawing it would invite someone to trust it.

  Slope is taken from the surface smoothed once more, over `SLOPE_SMOOTH_M`.
  A derivative amplifies whatever the surface carries at the scale of a grid
  cell, and below the 0.5 m binning that is interpolation facets and
  receiver noise, not ground. Smoothing in metres rather than pixels also
  means a slope map comes out the same whatever resolution it was gridded at.

  Horizontal and vertical units are both metres, so slope is a plain ratio.
  It is reported in percent grade (rise over run x 100), which is how
  drainage is specified; 2% is the usual minimum fall away from a
  foundation.

Contours are measured from the lowest measured point, in whole multiples of
the interval, so their labels read as "centimetres above the low spot" -
directly useful for grading, and independent of whether the vertical datum
has been tied to anything yet.

Grid geometry follows `build_surface`: node i of an axis sits at
`linspace(min, max, n)[i]`, and row 0 is the south edge.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .surface import Surface

# Below this the downhill direction is noise, not drainage: a flat pad
# still has a gradient in the last decimal place.
FLAT_PCT = 0.05

# Gaussian sigma applied before differentiating, in metres. Matches the bin
# cell: detail finer than the binning was never measured.
SLOPE_SMOOTH_M = 0.5


def grid_axes(surface: Surface) -> tuple[np.ndarray, np.ndarray]:
    """Projected easting of each column and northing of each row."""
    ny, nx = surface.z.shape
    e = surface.extent
    return np.linspace(e.xmin, e.xmax, nx), np.linspace(e.ymin, e.ymax, ny)


def node_spacing(surface: Surface) -> tuple[float, float]:
    x, y = grid_axes(surface)
    return float(x[1] - x[0]), float(y[1] - y[0])


@dataclass
class SlopeField:
    """Slope and downhill direction at every grid node."""

    slope_pct: np.ndarray        # percent grade, NaN where unmeasured
    down_e: np.ndarray           # unit vector pointing downhill, east part
    down_n: np.ndarray           # ... and north part; NaN where flat or unmeasured
    x: np.ndarray                # projected eastings of the columns
    y: np.ndarray                # projected northings of the rows

    def stats(self) -> dict:
        """Median, 90th percentile and maximum slope over measured ground."""
        s = self.slope_pct[np.isfinite(self.slope_pct)]
        if s.size == 0:
            return {"n": 0}
        return {"n": int(s.size), "median": float(np.median(s)),
                "p90": float(np.percentile(s, 90)), "max": float(s.max())}


def slope(surface: Surface, smooth_m: float = SLOPE_SMOOTH_M) -> SlopeField:
    from scipy.ndimage import gaussian_filter

    dx, dy = node_spacing(surface)
    z = surface.z
    if smooth_m > 0:
        # Pad by odd reflection before smoothing. It continues a trend past
        # the edge of the grid, where plain mirroring would fold it back and
        # flatten every slope within a metre of the edge - and the outermost
        # passes of a survey sit only just inside the grid.
        sy, sx = smooth_m / dy, smooth_m / dx
        py, px = int(np.ceil(4 * sy)), int(np.ceil(4 * sx))
        padded = np.pad(z, ((py, py), (px, px)), mode="reflect", reflect_type="odd")
        ny, nx = z.shape
        z = gaussian_filter(padded, sigma=(sy, sx))[py:py + ny, px:px + nx]
    # np.gradient returns derivatives along axis 0 (rows, northward) first.
    dz_dn, dz_de = np.gradient(z, dy, dx)
    grade = np.hypot(dz_de, dz_dn)
    pct = np.where(surface.mask, np.nan, grade * 100.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        flowing = np.isfinite(pct) & (pct >= FLAT_PCT)
        down_e = np.where(flowing, -dz_de / grade, np.nan)
        down_n = np.where(flowing, -dz_dn / grade, np.nan)
    x, y = grid_axes(surface)
    return SlopeField(pct, down_e, down_n, x, y)


@dataclass
class Arrow:
    e: float                     # projected position of the arrow's middle
    n: float
    down_e: float                # unit vector pointing downhill
    down_n: float
    slope_pct: float


def drainage_arrows(surface: Surface, spacing_m: float = 1.5,
                    field: SlopeField | None = None) -> list[Arrow]:
    """Downhill arrows on a regular grid, only where the ground was measured.

    Arrows are sampled, not averaged: the surface is already smoothed at a
    scale well below the spacing, so a sample is representative, and an
    average would blur a ridge into a flat.
    """
    field = field or slope(surface)
    dx, dy = node_spacing(surface)
    step_x = max(1, int(round(spacing_m / dx)))
    step_y = max(1, int(round(spacing_m / dy)))
    # Start half a step in, so the lattice sits inside the grid, not on its edge.
    rows = np.arange(step_y // 2, field.slope_pct.shape[0], step_y)
    cols = np.arange(step_x // 2, field.slope_pct.shape[1], step_x)
    out = []
    for r in rows:
        for c in cols:
            de, dn = field.down_e[r, c], field.down_n[r, c]
            if not (np.isfinite(de) and np.isfinite(dn)):
                continue
            out.append(Arrow(float(field.x[c]), float(field.y[r]), float(de),
                             float(dn), float(field.slope_pct[r, c])))
    return out


def relief(surface: Surface) -> tuple[float, float]:
    """Lowest and highest measured height, metres."""
    z = surface.z_masked
    if not np.isfinite(z).any():
        return float("nan"), float("nan")
    return float(np.nanmin(z)), float(np.nanmax(z))


def mapped_area_m2(surface: Surface) -> float:
    """Ground actually measured, in square metres."""
    dx, dy = node_spacing(surface)
    return float((~surface.mask).sum() * dx * dy)


@dataclass
class ContourLevel:
    above_low_m: float            # height above the lowest measured point
    height_m: float               # the same level on the surface's own datum
    major: bool                   # every second level: the labelled ones
    lines: list[np.ndarray] = field(default_factory=list)   # each (n, 2), projected


def contours(surface: Surface, interval_m: float = 0.05) -> list[ContourLevel]:
    """Contour lines over measured ground, every `interval_m` above the low point.

    The unmeasured cells are masked before tracing, so a contour stops at the
    edge of the survey instead of running across the house.
    """
    import contourpy

    if interval_m <= 0:
        raise ValueError("the contour interval must be positive")
    low, high = relief(surface)
    if not np.isfinite(low) or high - low < interval_m:
        return []
    x, y = grid_axes(surface)
    gen = contourpy.contour_generator(
        x, y, np.ma.masked_invalid(surface.z_masked),
        line_type=contourpy.LineType.Separate)

    levels = []
    k = 1
    while low + k * interval_m < high:
        height = low + k * interval_m
        lines = [np.asarray(line) for line in gen.lines(height) if len(line) > 1]
        levels.append(ContourLevel(above_low_m=k * interval_m, height_m=height,
                                   major=k % 2 == 0, lines=lines))
        k += 1
    return levels
