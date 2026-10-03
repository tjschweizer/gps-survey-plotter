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


def _height_background(ax, surface: Surface, site, interval_m: float):
    """Shaded relief coloured by height above the low point, with contours
    every `interval_m` (every second one labelled). Returns the colour
    scale's (norm, cmap), for its bar."""
    from matplotlib import colormaps
    from matplotlib.colors import LightSource, Normalize

    low, high = terrain.relief(surface)
    above_cm = (surface.z - low) * 100.0
    norm = Normalize(0.0, max((high - low) * 100.0, 1e-6))
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
    return norm, cmap


def heightmap(surface: Surface, site, *, note: str = "",
              interval_m: float = 0.05) -> bytes:
    """Shaded relief coloured by height above the low point, with labelled
    contours every `interval_m`. Returns PNG."""
    from matplotlib.cm import ScalarMappable

    low, high = terrain.relief(surface)
    relief_cm = (high - low) * 100.0
    area = terrain.mapped_area_m2(surface)
    interval_cm = interval_m * 100.0
    head = (f"Heightmap — shaded relief, {interval_cm:g} cm contours\n"
            f"{relief_cm:.0f} cm total relief · {area:,.0f} m² mapped")
    fig, ax = _figure(surface, site, head + (f" · {note}" if note else ""))
    norm, cmap = _height_background(ax, surface, site, interval_m)
    _colourbar(fig, ax, ScalarMappable(norm=norm, cmap=cmap),
               "Height above lowest point (cm)")
    return _png(fig)


def drainage_map(surface: Surface, site, *, note: str = "", interval_m: float = 0.05,
                 arrow_spacing_m: float = 2.0, full_slope_pct: float = 10.0,
                 key_slope_pct: float = 5.0) -> bytes:
    """Downhill arrows over the heightmap, each as long as the ground is
    steep: full length (0.9 x the spacing, so neighbours never touch) at
    `full_slope_pct` and steeper, shorter in proportion below. Returns PNG."""
    from matplotlib.cm import ScalarMappable

    field = terrain.slope(surface)
    stats = field.stats()
    slopes = (f" · slope median {stats['median']:.1f}%, 90th percentile {stats['p90']:.1f}%"
              if stats.get("n") else "")
    head = (f"Drainage — arrows point downhill, longer where steeper "
            f"(full length at {full_slope_pct:g}%)\n{interval_m * 100:g} cm contours{slopes}")
    fig, ax = _figure(surface, site, head + (f" · {note}" if note else ""))
    norm, cmap = _height_background(ax, surface, site, interval_m)

    arrows = terrain.drainage_arrows(surface, arrow_spacing_m, field)
    if arrows:
        full = 0.9 * arrow_spacing_m
        ax_e, ax_n = site.to_local(np.array([a.e for a in arrows]), np.array([a.n for a in arrows]))
        length = full * np.clip(np.array([a.slope_pct for a in arrows]) / full_slope_pct, 0, 1)
        # White with a dark edge: legible on every colour of the scale, and
        # unlike the thin black contours.
        q = ax.quiver(ax_e, ax_n, length * np.array([a.down_e for a in arrows]),
                      length * np.array([a.down_n for a in arrows]),
                      color="white", edgecolor="black", linewidth=0.6, angles="xy",
                      scale_units="xy", scale=1.0, pivot="mid", width=0.0032,
                      headwidth=3.2, headlength=3.4, headaxislength=3.0, minlength=0.5, zorder=3)
        ax.quiverkey(q, 0.86, 0.03, full * key_slope_pct / full_slope_pct, f"{key_slope_pct:g}% slope",
                     labelpos="W", coordinates="axes", fontproperties={"size": 9})
    _colourbar(fig, ax, ScalarMappable(norm=norm, cmap=cmap),
               "Height above lowest point (cm)")
    return _png(fig)


def contour_map(surface: Surface, site, *, note: str = "",
                interval_m: float = 0.05, major_every: int = 4) -> bytes:
    """Contour lines every `interval_m` over a pale hillshade, every
    `major_every`-th one heavier and labelled. Returns PNG."""
    from matplotlib.colors import LightSource

    low, high = terrain.relief(surface)
    area = terrain.mapped_area_m2(surface)
    interval_cm = interval_m * 100.0
    head = (f"Contours every {interval_cm:g} cm, heavier every {interval_cm * major_every:g} cm\n"
            f"{(high - low) * 100.0:.0f} cm total relief · {area:,.0f} m² mapped")
    fig, ax = _figure(surface, site, head + (f" · {note}" if note else ""))

    # A faint hillshade underneath, so the lines sit on recognisable ground.
    dx, dy = terrain.node_spacing(surface)
    shade = LightSource(azdeg=315, altdeg=45).hillshade(surface.z, vert_exag=3.0, dx=dx, dy=dy)
    grey = np.dstack([shade, shade, shade, np.where(surface.mask, 0.0, 0.35)])
    ax.imshow(grey, origin="lower", extent=_extent(surface, site), interpolation="bilinear")
    _outline(ax, surface, site)

    x, y = _local_axes(surface, site)
    above_cm = np.ma.masked_invalid(np.where(surface.mask, np.nan, (surface.z - low) * 100.0))
    levels = [lv.above_low_m * 100.0 for lv in terrain.contours(surface, interval_m)]
    if levels:
        step = interval_cm * major_every
        major = [lv for lv in levels if abs(lv / step - round(lv / step)) < 1e-6]
        minor = [lv for lv in levels if lv not in major]
        if minor:
            ax.contour(x, y, above_cm, levels=minor, colors="#3a3a3a", linewidths=0.45, alpha=0.8)
        if major:
            lines = ax.contour(x, y, above_cm, levels=major, colors="black", linewidths=1.1)
            ax.clabel(lines, fmt="%g cm", fontsize=7.5, inline=True)
    return _png(fig)
