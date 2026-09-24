"""Heightmap raster export.

Produces what archive/heightmap.py produced: a 16-bit greyscale PNG carrying
the elevations, a world file georeferencing it, a shaded-relief preview for
eyeballing, and an INFO sidecar recording the grey-to-elevation mapping.

The 16-bit PNG is the actual data product; the colour preview is not. Over the
site's ~1.2 m of relief, 65536 levels give roughly 0.02 mm per grey level, so
quantisation is nowhere near the error floor.

The INFO file is not decoration. A greyscale heightmap with no record of its
elevation range is unrecoverable data, and the mask matters just as much: the
16-bit raster has interpolated fill in the unmeasured cells because the format
has no NoData, so anyone reading it back needs to know which pixels were real.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from ..surface import Surface

UINT16_MAX = 65535


def write_heightmap(surface: Surface, outdir: str | Path, *,
                    tag: str = "", preview: bool = True,
                    vert_exag: float = 4.0) -> dict:
    """Write the 16-bit heightmap, world file, mask, preview, and INFO."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    base = f"heightmap{tag}"

    z_img, mask_img = surface.to_image()
    zmin, zmax = float(z_img.min()), float(z_img.max())
    span = zmax - zmin

    if span <= 0:
        raise ValueError("surface has no vertical relief; nothing to encode")

    norm = (z_img - zmin) / span
    img16 = (norm * UINT16_MAX).astype(np.uint16)

    paths = {}
    paths["png16"] = outdir / f"{base}_16bit.png"
    # Pillow infers I;16 from a uint16 array. Passing mode= explicitly is
    # deprecated and removed in Pillow 13 (October 2026).
    Image.fromarray(img16).save(paths["png16"])

    paths["mask"] = outdir / f"{base}_mask.npy"
    np.save(paths["mask"], mask_img)

    paths["world"] = outdir / f"{base}.pgw"
    paths["world"].write_text(surface.world_file(), encoding="utf-8")

    if preview:
        paths["preview"] = _write_preview(
            z_img, mask_img, surface.px, outdir / f"{base}_color.png", vert_exag)

    paths["info"] = outdir / f"{base}_INFO.txt"
    paths["info"].write_text(
        _info_text(surface, zmin, zmax, span), encoding="utf-8")

    return {"paths": paths, "zmin": zmin, "zmax": zmax,
            "measured_fraction": surface.measured_fraction}


def _write_preview(z_img, mask_img, px, path: Path, vert_exag: float) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LightSource

    ls = LightSource(azdeg=315, altdeg=45)
    rgb = ls.shade(z_img, cmap=plt.cm.terrain, blend_mode="soft",
                   vert_exag=vert_exag, dx=px, dy=px)
    rgb = (rgb * 255).astype(np.uint8)
    rgb[mask_img] = 245          # unmeasured: house, tree wells, off-lot
    Image.fromarray(rgb).save(path)
    return path


def _info_text(surface: Surface, zmin: float, zmax: float, span: float) -> str:
    site = surface.site
    crs = f"EPSG:{site.epsg}" if site else "unknown"
    return (
        "Heightmap\n"
        "=========\n"
        f"raster            : {surface.z.shape[1]} x {surface.z.shape[0]}, "
        "16-bit greyscale, north up\n"
        f"ground sample dist: {surface.px * 100:.2f} cm/pixel\n"
        f"extent            : {surface.extent.width:.2f} x "
        f"{surface.extent.height:.2f} m\n"
        f"CRS               : {crs}\n\n"
        f"grey 0     = {zmin:.4f} m\n"
        f"grey 65535 = {zmax:.4f} m\n"
        f"vertical range     = {span:.4f} m\n"
        f"scale              = {span / UINT16_MAX * 1000:.5f} mm per grey level\n\n"
        f"elevation(m) = {zmin:.4f} + grey/65535 * {span:.4f}\n\n"
        f"points used       : {surface.n_points:,}\n"
        f"occupied cells    : {surface.n_cells:,}\n"
        f"unmeasured pixels : {(1 - surface.measured_fraction) * 100:.1f}% "
        "(house footprint, tree wells,\n"
        "                    outside the surveyed area) - shown pale in the\n"
        "                    preview, INTERPOLATED FILL in the 16-bit raster,\n"
        "                    true mask in the .npy\n"
    )
