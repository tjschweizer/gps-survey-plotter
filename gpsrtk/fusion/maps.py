"""Terrain maps from a fused session: heightmap, contours, drainage.

    python -m gpsrtk.fusion.maps <fused.csv> <site.local.json> <out folder>

Points: every RTK-fixed epoch at its fused position, plus fused float
epochs only in 0.5 m cells that no fixed epoch reached. Fixed heights agree
to about 1.5 cm where passes cross; fused float to about 11 cm (2026-10-03),
so float only fills where nothing better exists. Heights are the antenna's:
the maps show height above the lowest point, so the antenna's constant
height above the ground drops out.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from pyproj import Transformer

from ..io import figures
from ..model.pointset import E, FIX, N, TIME, Z, PointSet
from ..site import Site
from ..surface import Extent, build_surface

CELL_M = 0.5


def point_set(fused: pd.DataFrame, with_float: bool = True) -> tuple[PointSet, dict]:
    """Fixed epochs, and fused float where no fixed epoch shares its cell."""
    to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32615", always_xy=True)
    e, n = to_utm.transform(fused["lon"].to_numpy(), fused["lat"].to_numpy())
    fix = fused["fix"].to_numpy()
    fixed = fix == 4
    keep = fixed.copy()
    info = {"fixed": int(fixed.sum()), "float_added": 0}
    if with_float:
        cell = lambda i: (np.floor(e[i] / CELL_M).astype(np.int64) << 32) + np.floor(n[i] / CELL_M).astype(np.int64)
        fixed_cells = set(cell(np.flatnonzero(fixed)).tolist())
        cand = np.flatnonzero((fix == 5) & (fused["source"].to_numpy() == "fused"))
        add = cand[[c not in fixed_cells for c in cell(cand).tolist()]]
        keep[add] = True
        info["float_added"] = int(len(add))
    df = pd.DataFrame({E: e[keep], N: n[keep], Z: fused["h"].to_numpy()[keep], FIX: fix[keep],
                       TIME: pd.to_datetime(fused["utc"].to_numpy()[keep], unit="s")})
    return PointSet(df, layer="track_points"), info


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
    2016-18 ortho sat about 1 m west and 1-2 m north of the mower's track)."""
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


def maps(fused_csv: str | Path, site_path: str | Path, out: str | Path, interval_m: float = 0.05,
         imagery: str | None = None, imagery_offset: tuple[float, float] = (0.0, 0.0),
         cache_dir: str | Path | None = None) -> list[str]:
    """The three maps; with `imagery`, also the drainage map over that photo."""
    fused = pd.read_csv(fused_csv)
    site = Site.load(site_path)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    lines = []
    ps_fixed, _ = point_set(fused, with_float=False)
    ps, info = point_set(fused)
    s_fixed = build_surface(ps_fixed, site)
    surface = build_surface(ps, site)
    lines.append(f"points: {info['fixed']:,} fixed, {info['float_added']:,} fused float in cells with no fixed epoch")
    lines.append(f"measured share of the gridded square: {s_fixed.measured_fraction * 100:.0f}% fixed only, "
                 f"{surface.measured_fraction * 100:.0f}% with fused float")
    note = f"fixed + fused float · {surface.measured_fraction * 100:.0f}% measured"
    for name, png in (("heightmap.png", figures.heightmap(surface, site, note=note, interval_m=interval_m)),
                      ("contours.png", figures.contour_map(surface, site, note=note, interval_m=interval_m)),
                      ("drainage.png", figures.drainage_map(surface, site, note=note, interval_m=interval_m))):
        (out / name).write_bytes(png)
        lines.append(f"wrote {out / name}")
    bg = background(site, frame_limits(surface, site), imagery, Path(cache_dir) if cache_dir else None, imagery_offset)
    if bg is not None:
        image, extent, credit = bg
        (out / "drainage_photo.png").write_bytes(
            figures.drainage_map(surface, site, note=note, interval_m=interval_m, photo=(image, extent), credit=credit))
        lines.append(f"wrote {out / 'drainage_photo.png'}")
    return lines


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("fused")
    ap.add_argument("site")
    ap.add_argument("out")
    ap.add_argument("--interval", type=float, default=0.05, help="contour interval, m")
    ap.add_argument("--imagery", default=None, help="an imagery provider's name: also a drainage map over it")
    ap.add_argument("--imagery-offset", type=float, nargs=2, default=(0.0, 0.0), metavar=("EAST", "NORTH"))
    ap.add_argument("--cache", default=None, help="folder to cache imagery in")
    a = ap.parse_args(argv)
    for line in maps(a.fused, a.site, a.out, a.interval, a.imagery, tuple(a.imagery_offset), a.cache):
        print(line)


if __name__ == "__main__":
    main(sys.argv[1:])
