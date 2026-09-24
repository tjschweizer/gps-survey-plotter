"""Printable maps: slope with drainage arrows, and a contoured heightmap.

These are the two pictures that answer grading questions at a glance - where
does water go, and how much fall is there - laid out as finished figures
rather than screen views: a title that says what was drawn and from which
data, axes in metres from the site's local origin (the frame the Revit export
uses), a north arrow, and a colour bar with units.

What they will not do is draw ground that was not measured. Masked cells are
left blank and the edge of the survey is outlined, so a hole in coverage
reads as a hole rather than as flat lawn.

Rendering is matplotlib's Agg backend, so no display is needed.
"""

from __future__ import annotations

import io

import numpy as np

from .. import terrain
from ..surface import Surface

FIGSIZE = (10.0, 9.4)
DPI = 150
ARROW_COLOUR = "#1fb4c8"
OUTLINE = "0.55"
MARGIN_M = 1.5


def _figure(surface: Surface, site, title: str):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=FIGSIZE, dpi=DPI)
    ax.set_title(title, loc="left", fontsize=13, fontweight="bold")
    ax.set_xlabel("East (m)")
    ax.set_ylabel("North (m)")
    ax.set_aspect("equal")
    ax.grid(True, alpha=0.15)

    # Frame the measured ground, not the whole square the surface was
    # gridded on: the corners of that square are mostly interpolated fill.
    x, y = _local_axes(surface, site)
    rows, cols = np.nonzero(~surface.mask)
    if rows.size:
        ax.set_xlim(x[cols.min()] - MARGIN_M, x[cols.max()] + MARGIN_M)
        ax.set_ylim(y[rows.min()] - MARGIN_M, y[rows.max()] + MARGIN_M)
    _north_arrow(ax)
    return fig, ax


def _local_axes(surface: Surface, site):
    x, y = terrain.grid_axes(surface)
    lx, ly = site.to_local(x, y)
    return np.asarray(lx), np.asarray(ly)


def _extent(surface: Surface, site):
    """imshow extent: pixel edges, half a node beyond the outer nodes."""
    x, y = _local_axes(surface, site)
    hx, hy = (x[1] - x[0]) / 2, (y[1] - y[0]) / 2
    return (x[0] - hx, x[-1] + hx, y[0] - hy, y[-1] + hy)


def _north_arrow(ax) -> None:
    # On a pale disc, so it stays legible where the data reaches the corner.
    ax.annotate("N", xy=(0.95, 0.96), xytext=(0.95, 0.86),
                xycoords="axes fraction", textcoords="axes fraction",
                ha="center", va="top", fontsize=13, fontweight="bold",
                bbox={"boxstyle": "circle,pad=0.25", "fc": "white",
                      "ec": "none", "alpha": 0.8},
                arrowprops={"arrowstyle": "-|>", "color": "black", "lw": 1.4},
                zorder=5)


def _outline(ax, surface: Surface, site) -> None:
    """A thin line round the measured ground, so its edge is unmistakable."""
    x, y = _local_axes(surface, site)
    ax.contour(x, y, (~surface.mask).astype(float), levels=[0.5],
               colors=OUTLINE, linewidths=0.7)


def _colourbar(fig, ax, mappable, label: str, **kw):
    """A colour bar exactly as tall as the map beside it."""
    from mpl_toolkits.axes_grid1 import make_axes_locatable

    cax = make_axes_locatable(ax).append_axes("right", size="4%", pad=0.25)
    bar = fig.colorbar(mappable, cax=cax, **kw)
    bar.set_label(label)
    return bar


def _png(fig) -> bytes:
    import matplotlib.pyplot as plt

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, bbox_inches="tight", pad_inches=0.3)
    plt.close(fig)
    return buf.getvalue()


def slope_map(surface: Surface, site, *, note: str = "",
              slope_max_pct: float = 10.0, arrow_spacing_m: float = 1.5) -> bytes:
    """Slope in percent grade, with arrows pointing downhill. Returns PNG."""
    from matplotlib import colormaps

    field = terrain.slope(surface)
    title = "Slope and drainage direction (arrows point downhill)"
    fig, ax = _figure(surface, site, title + (f"\n{note}" if note else ""))

    cmap = colormaps["magma_r"].with_extremes(bad=(1, 1, 1, 0))
    image = ax.imshow(np.ma.masked_invalid(field.slope_pct), origin="lower",
                      extent=_extent(surface, site), cmap=cmap, vmin=0.0,
                      vmax=slope_max_pct, interpolation="bilinear")
    _outline(ax, surface, site)

    arrows = terrain.drainage_arrows(surface, arrow_spacing_m, field)
    if arrows:
        ax_e, ax_n = site.to_local(np.array([a.e for a in arrows]),
                                   np.array([a.n for a in arrows]))
        length = 0.6 * arrow_spacing_m
        # Every arrow the same length: direction is the message, and the
        # colour underneath already carries the steepness.
        ax.quiver(ax_e, ax_n, [a.down_e for a in arrows], [a.down_n for a in arrows],
                  color=ARROW_COLOUR, angles="xy", scale_units="xy",
                  scale=1.0 / length, pivot="mid", width=0.0042,
                  headwidth=3.6, headlength=4.0, headaxislength=3.5, zorder=3)

    _colourbar(fig, ax, image, "Slope (%)", extend="max")
    return _png(fig)


def heightmap(surface: Surface, site, *, note: str = "",
              interval_m: float = 0.05) -> bytes:
    """Shaded relief coloured by height above the low point, with labelled
    contours every `interval_m`. Returns PNG."""
    from matplotlib import colormaps
    from matplotlib.cm import ScalarMappable
    from matplotlib.colors import LightSource, Normalize

    low, high = terrain.relief(surface)
    relief_cm = (high - low) * 100.0
    area = terrain.mapped_area_m2(surface)
    interval_cm = interval_m * 100.0
    head = (f"Heightmap — shaded relief, {interval_cm:g} cm contours\n"
            f"{relief_cm:.0f} cm total relief · {area:,.0f} m² mapped")
    fig, ax = _figure(surface, site, head + (f" · {note}" if note else ""))

    above_cm = (surface.z - low) * 100.0
    norm = Normalize(0.0, max(relief_cm, 1e-6))
    cmap = colormaps["turbo"]
    dx, dy = terrain.node_spacing(surface)
    # Colour by height, shade by the surface itself - in metres, so the
    # exaggeration means what it says against metre pixel spacing.
    colour = cmap(norm(above_cm))[..., :3]
    shaded = LightSource(azdeg=315, altdeg=45).shade_rgb(
        colour, surface.z, blend_mode="soft", vert_exag=3.0, dx=dx, dy=dy)
    rgba = np.dstack([np.clip(shaded, 0, 1), np.where(surface.mask, 0.0, 1.0)])
    ax.imshow(rgba, origin="lower", extent=_extent(surface, site),
              interpolation="bilinear")
    _outline(ax, surface, site)

    x, y = _local_axes(surface, site)
    levels = [lv.above_low_m * 100.0 for lv in terrain.contours(surface, interval_m)]
    if levels:
        lines = ax.contour(x, y, np.ma.masked_invalid(np.where(surface.mask, np.nan, above_cm)),
                           levels=levels, colors="black", linewidths=0.5, alpha=0.75)
        labelled = levels[1::2]            # every second level, as on the map
        if labelled:
            ax.clabel(lines, levels=labelled, fmt="%g", fontsize=7, inline=True)

    _colourbar(fig, ax, ScalarMappable(norm=norm, cmap=cmap),
               "Height above lowest point (cm)")
    return _png(fig)
