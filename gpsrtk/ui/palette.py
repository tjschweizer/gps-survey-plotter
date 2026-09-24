"""Colour mapping shared by the 2D and 3D views.

Both views must agree on what a colour means, or comparing them is misleading.
Normalisation is done on the measured cells only - including the interpolated
fill would let a fill artefact at the edge of the survey set the colour range
for the real ground in the middle.
"""

from __future__ import annotations

import numpy as np

# Colormaps that suit terrain. `terrain` is what the archive scripts used;
# the greys are for when hillshade should carry the shape by itself, and
# `viridis`/`cividis` are the perceptually uniform options for reading
# absolute height rather than admiring relief.
COLORMAPS = ["terrain", "gist_earth", "viridis", "cividis", "magma",
             "Spectral_r", "gray"]


def normalize(z: np.ndarray, mask: np.ndarray | None = None,
              clip: tuple[float, float] = (0.5, 99.5)) -> np.ndarray:
    """Scale to 0..1 using percentiles of the measured cells only."""
    valid = z[~mask] if mask is not None else z
    valid = valid[np.isfinite(valid)]
    if valid.size == 0:
        return np.zeros_like(z, dtype=float)
    lo, hi = np.percentile(valid, clip)
    if hi <= lo:
        return np.zeros_like(z, dtype=float)
    return np.clip((z - lo) / (hi - lo), 0.0, 1.0)


def colorize(z: np.ndarray, cmap: str = "terrain",
             mask: np.ndarray | None = None,
             hillshade: bool = True, px: float = 1.0,
             vert_exag: float = 4.0) -> np.ndarray:
    """RGBA image for a height grid. Masked cells come back fully transparent."""
    import matplotlib
    import matplotlib.pyplot as plt
    from matplotlib.colors import LightSource

    filled = np.where(np.isfinite(z), z, np.nanmin(z) if np.isfinite(z).any() else 0.0)
    norm = normalize(filled, mask)
    table = matplotlib.colormaps[cmap]

    if hillshade:
        ls = LightSource(azdeg=315, altdeg=45)
        rgb = ls.shade(norm, cmap=table, blend_mode="soft",
                       vert_exag=vert_exag, dx=px, dy=px)
    else:
        rgb = table(norm)

    rgba = (np.asarray(rgb) * 255).astype(np.uint8)
    if rgba.shape[-1] == 3:
        rgba = np.dstack([rgba, np.full(rgba.shape[:2], 255, np.uint8)])
    if mask is not None:
        rgba[..., 3] = np.where(mask, 0, 255)
    return rgba
