"""Gridding point samples into a terrain raster.

This is the block that `archive/heightmap.py` and `archive/iso.py` each carried
their own near-identical copy of. It exists once here.

The sequence, and why each step is there:

  bin to cells      Samples are ~0.3 m apart along a mowing pass and ~1 m
                    between passes. Triangulating that directly produces sliver
                    triangles which hillshade into false corduroy. Median within
                    a cell also rejects single-epoch spikes.
  linear griddata   Interpolation only - never extrapolation. Outside the convex
                    hull comes back NaN, which is correct: we did not measure it.
  distance mask     Anything further than `mask_distance_m` from a measured cell
                    is not terrain we surveyed. The house footprint, tree wells,
                    and everything beyond the mowed hull fall out here. Reporting
                    the measured fraction alongside the raster is not optional -
                    a hillshade of interpolated fill looks exactly as convincing
                    as a hillshade of real ground.
  nearest fill      The masked cells still need *some* value so that smoothing
                    and 16-bit export do not propagate NaN. They are filled and
                    then flagged, never silently presented as measurement.
  median + gaussian Removes residual per-cell noise without moving real breaks
                    much. Applied after filling so the kernels see no NaN.

Row order is ascending northing: row 0 is the SOUTH edge, matching the meshgrid.
Raster formats want north-up, so `to_image()` flips. Keeping the flip at the
edge rather than baked in avoids the sign confusion that world files invite.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.interpolate import griddata
from scipy.ndimage import distance_transform_edt, gaussian_filter, median_filter

from .model.pointset import PointSet, E, N, Z, elevation_column
from .site import Site


@dataclass
class Extent:
    """Projected bounding box of a raster."""

    xmin: float
    xmax: float
    ymin: float
    ymax: float

    @property
    def width(self) -> float:
        return self.xmax - self.xmin

    @property
    def height(self) -> float:
        return self.ymax - self.ymin

    @classmethod
    def square_around(cls, x, y, margin: float = 1.02) -> "Extent":
        """Square extent centred on the data, as the archive scripts used.

        Square keeps pixels isotropic, which the hillshade and the world file
        both assume.
        """
        cx, cy = (x.min() + x.max()) / 2, (y.min() + y.max()) / 2
        half = max(x.max() - x.min(), y.max() - y.min()) / 2 * margin
        return cls(cx - half, cx + half, cy - half, cy + half)


@dataclass
class Surface:
    """A gridded terrain raster plus the honesty metadata that goes with it."""

    z: np.ndarray               # (size, size), row 0 = south, filled everywhere
    mask: np.ndarray            # True where unmeasured (fill, not measurement)
    extent: Extent
    px: float                   # ground sample distance, metres
    n_points: int               # samples in
    n_cells: int                # occupied bins after reduction
    site: Site | None = None
    z_column: str = Z           # which height this surface was built from

    @property
    def measured_fraction(self) -> float:
        return float((~self.mask).mean())

    @property
    def z_masked(self) -> np.ndarray:
        """Elevations with unmeasured cells as NaN, for plotting."""
        return np.where(self.mask, np.nan, self.z)

    def to_image(self) -> tuple[np.ndarray, np.ndarray]:
        """North-up (row 0 = north) elevation and mask, for raster export."""
        return np.flipud(self.z), np.flipud(self.mask)

    def world_file(self) -> str:
        """ESRI world file contents for the north-up image."""
        return (f"{self.px}\n0.0\n0.0\n{-self.px}\n"
                f"{self.extent.xmin + self.px / 2}\n"
                f"{self.extent.ymax - self.px / 2}\n")

    def describe(self) -> str:
        zm = self.z_masked
        return (f"{self.n_points:,} points -> {self.n_cells:,} cells -> "
                f"{self.z.shape[0]}x{self.z.shape[1]} raster\n"
                f"  GSD {self.px * 100:.2f} cm/px, "
                f"relief {np.nanmax(zm) - np.nanmin(zm):.3f} m\n"
                f"  {self.measured_fraction * 100:.1f}% measured, "
                f"{(1 - self.measured_fraction) * 100:.1f}% interpolated fill")


def bin_cells(ps: PointSet, cell: float, site: Site | None = None,
              z_column: str | None = None) -> pd.DataFrame:
    """Reduce samples to one row per occupied cell: mean position, median z.

    Bins are anchored to the site's local origin rather than to the data's own
    bounding box, so two sessions covering overlapping ground land on the same
    grid. That is what makes differencing and offset estimation between sessions
    meaningful; anchoring to the data would shift the grid every time a session
    covered slightly different extent.
    """
    d = ps.df
    z_column = z_column or elevation_column(ps)
    ox = site.origin_e if site else 0.0
    oy = site.origin_n if site else 0.0
    ix = np.floor((d[E].to_numpy() - ox) / cell).astype(np.int64)
    iy = np.floor((d[N].to_numpy() - oy) / cell).astype(np.int64)

    b = pd.DataFrame({"ix": ix, "iy": iy,
                      "x": d[E].to_numpy(), "y": d[N].to_numpy(),
                      "z": d[z_column].to_numpy()})
    g = b.groupby(["ix", "iy"], sort=False).agg(
        x=("x", "mean"), y=("y", "mean"), z=("z", "median"),
        sd=("z", "std"), count=("z", "size"))
    return g.reset_index()


def build_surface(ps: PointSet, site: Site, *,
                  size: int | None = None,
                  cell: float | None = None,
                  mask_distance: float | None = None,
                  median_size: int | None = None,
                  sigma: float | None = None,
                  extent: Extent | None = None,
                  z_column: str | None = None) -> Surface:
    """Grid a point set into a Surface. Defaults come from the site.

    Height comes from the derived elevation column once a vertical model has
    been applied, and from raw ellipsoidal height otherwise.
    """
    sd = site.surface
    size = size or sd.raster_size
    cell = cell if cell is not None else sd.bin_cell_m
    mask_distance = (mask_distance if mask_distance is not None
                     else sd.mask_distance_m)
    median_size = median_size if median_size is not None else sd.median_size
    sigma = sigma if sigma is not None else sd.gaussian_sigma

    if len(ps) == 0:
        raise ValueError("cannot build a surface from an empty point set")

    z_column = z_column or elevation_column(ps)
    b = bin_cells(ps, cell, site, z_column)
    ext = extent or Extent.square_around(b.x.to_numpy(), b.y.to_numpy())

    gx = np.linspace(ext.xmin, ext.xmax, size)
    gy = np.linspace(ext.ymin, ext.ymax, size)
    GX, GY = np.meshgrid(gx, gy)
    px = ext.width / size

    pts = (b.x.to_numpy(), b.y.to_numpy())
    z = griddata(pts, b.z.to_numpy(), (GX, GY), method="linear")

    # Distance from every pixel to the nearest cell that actually holds samples.
    have = np.zeros((size, size), bool)
    cxi = np.clip(((b.x - ext.xmin) / px).astype(int), 0, size - 1)
    cyi = np.clip(((b.y - ext.ymin) / px).astype(int), 0, size - 1)
    have[cyi, cxi] = True
    far = distance_transform_edt(~have) * px > mask_distance
    mask = np.isnan(z) | far

    z = np.where(np.isnan(z),
                 griddata(pts, b.z.to_numpy(), (GX, GY), method="nearest"), z)
    if median_size and median_size > 1:
        z = median_filter(z, size=median_size)
    if sigma:
        z = gaussian_filter(z, sigma=sigma)

    return Surface(z=z, mask=mask, extent=ext, px=px,
                   n_points=len(ps), n_cells=len(b), site=site,
                   z_column=z_column)
