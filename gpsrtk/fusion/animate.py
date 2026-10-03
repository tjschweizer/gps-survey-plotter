"""An animated GIF of a mowing session: the track appears as it was mowed,
coloured by height, framed like the heightmap.

    python -m gpsrtk.fusion.animate <fused.csv> <site.local.json> <out.gif> [--seconds 30]

The track is every fused epoch (fixed, and float the IMU carried), coloured
by height above the surface's lowest point on the heightmap's colour scale,
so a frame and the finished heightmap read the same way. Float epochs the
Cube didn't cover are left out: their heights are tens of centimetres off.

The background is a raster layer from the plotter's imagery providers when
one is given (cached), otherwise plain.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ..io import figures
from ..site import Site
from ..surface import Extent, build_surface
from .maps import point_set

FPS = 10


def frame_limits(surface, site) -> tuple[float, float, float, float]:
    """The heightmap's view: the measured ground plus its margin, local metres."""
    x, y = figures._local_axes(surface, site)
    rows, cols = np.nonzero(~surface.mask)
    m = figures.MARGIN_M
    return x[cols.min()] - m, x[cols.max()] + m, y[rows.min()] - m, y[rows.max()] + m


def background(site, limits, provider_name: str | None, cache_dir: Path | None,
               offset: tuple[float, float] = (0.0, 0.0)):
    """(image, local extent, credit), or None for a plain background.

    `offset` (east, north, metres) moves the photo onto the RTK positions:
    aerial imagery is commonly a metre or two out (on 2026-10-03 the Iowa
    2016-18 ortho sat about 1 m west and 2 m north of the mower's track)."""
    if not provider_name:
        return None
    from ..io.imagery import default_providers, fetch_cached

    provider = default_providers()[provider_name]
    # Fetch beyond the frame by the shift (and a metre), so the moved photo still covers it.
    de, dn = offset
    pad = float(np.hypot(de, dn)) + 1.0
    x0, x1, y0, y1 = limits
    e0, n0 = site.to_projected(x0 - pad, y0 - pad)
    e1, n1 = site.to_projected(x1 + pad, y1 + pad)
    layer = fetch_cached(provider, Extent(e0, e1, n0, n1), site.epsg, 1024, cache_dir)
    lx0, ly0 = site.to_local(layer.extent.xmin, layer.extent.ymin)
    lx1, ly1 = site.to_local(layer.extent.xmax, layer.extent.ymax)
    return layer.image, (lx0 + de, lx1 + de, ly0 + dn, ly1 + dn), layer.attribution or provider.attribution


def animate(fused_csv, site_path, out_gif, seconds: float = 30.0, provider: str | None = None,
            cache_dir: str | Path | None = None, tz: str = "America/Chicago", log=print,
            imagery_offset: tuple[float, float] = (0.0, 0.0),
            append: list[tuple[str | Path, float]] = ()) -> Path:
    """`append`: still images (a path, seconds on screen) shown after the
    last frame, each fitted into the frame on white."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import colormaps
    from matplotlib.colors import Normalize
    from PIL import Image

    fused = pd.read_csv(fused_csv)
    site = Site.load(site_path)
    ps, _ = point_set(fused)
    surface = build_surface(ps, site)
    low = float(np.nanmin(np.where(surface.mask, np.nan, surface.z)))
    high = float(np.nanmax(np.where(surface.mask, np.nan, surface.z)))
    limits = frame_limits(surface, site)

    keep = (fused["fix"] == 4) | ((fused["fix"] == 5) & (fused["source"] == "fused"))
    track = fused.loc[keep].sort_values("t")
    from pyproj import Transformer
    e, n = Transformer.from_crs("EPSG:4326", f"EPSG:{site.epsg}", always_xy=True).transform(
        track["lon"].to_numpy(), track["lat"].to_numpy())
    lx, ly = site.to_local(e, n)
    above = (track["h"].to_numpy() - low) * 100.0
    t = track["t"].to_numpy()
    utc0 = float(fused["utc"].iloc[0])
    start = datetime.fromtimestamp(utc0, timezone.utc).astimezone(ZoneInfo(tz))

    norm = Normalize(0.0, (high - low) * 100.0)
    cmap = colormaps["turbo"]
    fig, ax = plt.subplots(figsize=(8.0, 7.6), dpi=100)
    ax.set_aspect("equal")
    ax.set_xlim(limits[0], limits[1])
    ax.set_ylim(limits[2], limits[3])
    ax.set_xlabel("East (m)")
    ax.set_ylabel("North (m)")
    bg = background(site, limits, provider, Path(cache_dir) if cache_dir else None, imagery_offset)
    credit = ""
    if bg is not None:
        img, ext, credit = bg
        ax.imshow(img, extent=ext, origin="upper", interpolation="bilinear", zorder=0)
        # Dimmed a little, so the coloured track stands out.
        ax.imshow(np.zeros((2, 2, 4)) + [0, 0, 0, 0.25], extent=ext, origin="upper", zorder=1)
    figures._north_arrow(ax)
    dots = ax.scatter([], [], s=3.5, c=[], cmap=cmap, norm=norm, linewidths=0, zorder=2)
    mower, = ax.plot([], [], "o", ms=9, mfc="white", mec="black", mew=1.5, zorder=3)
    clock = ax.text(0.02, 0.97, "", transform=ax.transAxes, va="top", ha="left", fontsize=11,
                    bbox={"boxstyle": "round,pad=0.3", "fc": "white", "ec": "none", "alpha": 0.85}, zorder=4)
    if credit:
        ax.text(0.01, 0.01, credit, transform=ax.transAxes, fontsize=6.5, color="white", va="bottom", zorder=4)
    bar = fig.colorbar(plt.cm.ScalarMappable(norm=norm, cmap=cmap), ax=ax, fraction=0.04, pad=0.02)
    bar.set_label("Height above lowest point (cm)")
    ax.set_title(f"Mowing {start:%Y-%m-%d}, from {start:%H:%M}", loc="left", fontsize=12, fontweight="bold")
    fig.tight_layout()

    frames = max(2, int(round(seconds * FPS)))
    times = np.linspace(t[0], t[-1], frames)
    total = t[-1] - t[0]

    def draw(T):
        i = int(np.searchsorted(t, T, side="right"))
        dots.set_offsets(np.column_stack([lx[:i], ly[:i]]))
        dots.set_array(above[:i])
        if i:
            mower.set_data([lx[i - 1]], [ly[i - 1]])
        el = T - t[0]
        clock.set_text(f"{int(el // 60):02d}:{int(el % 60):02d} of {int(total // 60):02d}:{int(total % 60):02d}")
        fig.canvas.draw()
        return Image.fromarray(np.asarray(fig.canvas.buffer_rgba())[..., :3].copy())

    # One palette for every frame, from the finished picture: then frames
    # differ only where the track grew, and the GIF stores only that.
    palette = draw(times[-1]).quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    images = []
    for k, T in enumerate(times):
        images.append(draw(T).quantize(palette=palette, dither=Image.Dither.NONE))
        if (k + 1) % 50 == 0:
            log(f"  {k + 1}/{frames} frames")
    plt.close(fig)
    # Hold the last frame a few seconds, then any stills, before looping.
    durations = [1000 // FPS] * (len(images) - 1) + [3000]
    size = images[0].size
    for path, secs in append:
        still = Image.open(path).convert("RGB")
        k = min(size[0] / still.width, size[1] / still.height)
        still = still.resize((round(still.width * k), round(still.height * k)), Image.Resampling.LANCZOS)
        card = Image.new("RGB", size, "white")
        card.paste(still, ((size[0] - still.width) // 2, (size[1] - still.height) // 2))
        # Its own palette: a map's colours aren't the track's.
        images.append(card.quantize(colors=255, method=Image.Quantize.MEDIANCUT))
        durations.append(int(secs * 1000))
    out = Path(out_gif)
    out.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(out, save_all=True, append_images=images[1:], duration=durations, loop=0, disposal=1)
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("fused")
    ap.add_argument("site")
    ap.add_argument("out")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--imagery", default=None, help="an imagery provider's name, for a background")
    ap.add_argument("--cache", default=None, help="folder to cache imagery in")
    ap.add_argument("--imagery-offset", type=float, nargs=2, default=(0.0, 0.0), metavar=("EAST", "NORTH"),
                    help="move the photo by this many metres to sit on the RTK positions")
    ap.add_argument("--append", nargs=2, action="append", default=[], metavar=("IMAGE", "SECONDS"),
                    help="show this still after the animation for this long (repeatable)")
    a = ap.parse_args(argv)
    out = animate(a.fused, a.site, a.out, a.seconds, a.imagery, a.cache, log=lambda s: print(s, flush=True),
                  imagery_offset=tuple(a.imagery_offset), append=[(p, float(t)) for p, t in a.append])
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main(sys.argv[1:])
