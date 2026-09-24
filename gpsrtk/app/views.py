"""What the plan view and the 3D view draw, prepared for a browser.

Everything is in metres from the site's local origin, not raw UTM. Raw
northings here are ~4.65 million, which is unreadable on an axis and wastes
float precision in a browser; local metres also match the frame the Revit
export uses.

Colour is decided here, in Python, for both views. The 2D and 3D views must
agree on what a colour means or comparing them is misleading, and computing it
once from matplotlib's colour maps is the simplest way to guarantee that.

Two choices about honesty carry over from the desktop views unchanged:

  Unmeasured cells are fully transparent, not filled with a plausible colour.
  A hillshade of interpolated fill is indistinguishable from real ground at a
  glance, and the point of the mask is that it is not ground we measured.

  Points are subsampled above a cap. A few mowing sessions produce far more
  points than a browser can draw interactively, and drawing every one of them
  communicates nothing that 50k does not.
"""

from __future__ import annotations

import io
import struct

import numpy as np

from ..model.pointset import E, N, Z, ELEV, FIX, SPEED, SESSION
from .palette import COLORMAPS, colorize

MAX_SCATTER = 50_000

COLOR_BY = {
    "elevation": ELEV,
    "fix quality": FIX,
    "speed": SPEED,
    "session": SESSION,
}

GREY = (200, 200, 200, 180)
FIX_COLOURS = {4: (60, 200, 90), 5: (240, 150, 40)}   # fixed green, float orange
OTHER_FIX = (150, 150, 150)

# A spot layer rather than a track: small enough to draw as distinct markers.
MAX_SPOT_LAYER = 500


def _png(rgba: np.ndarray) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(rgba)).save(buf, format="PNG")
    return buf.getvalue()


def _cmap(name: str):
    import matplotlib

    return matplotlib.colormaps[name if name in COLORMAPS else "terrain"]


# --- points ---------------------------------------------------------------------

def scatter_frame(ps, cap: int | None = None):
    """The rows to draw, and whether they are a subsample.

    The subsample is seeded, so the same data always shows the same points
    and a redraw does not make the scatter shimmer.
    """
    cap = MAX_SCATTER if cap is None else cap
    d = ps.df
    n = len(d)
    if n > cap:
        idx = np.random.default_rng(0).choice(n, cap, replace=False)
        return d.iloc[np.sort(idx)], True
    return d, False


def point_colours(d, color_by: str = "elevation",
                  cmap: str = "terrain") -> np.ndarray:
    """An (n, 4) uint8 RGBA colour per row."""
    n = len(d)
    column = COLOR_BY.get(color_by, ELEV)
    if column == ELEV and ELEV not in d.columns:
        column = Z                       # no vertical model applied yet
    if column not in d.columns:
        return np.tile(np.array(GREY, np.uint8), (n, 1))

    v = d[column]
    out = np.empty((n, 4), np.uint8)
    if column == FIX:
        # Categorical and meaningful: green fixed, orange float, grey other.
        codes = v.to_numpy(dtype=float)
        for i, x in enumerate(codes):
            out[i, :3] = FIX_COLOURS.get(int(x) if x == x else -1, OTHER_FIX)
        out[:, 3] = 200
        return out
    if column == SESSION:
        import matplotlib

        names = sorted(map(str, v.dropna().unique()))
        index = {nm: i for i, nm in enumerate(names)}
        table = (np.asarray(matplotlib.colormaps["tab10"].colors) * 255
                 ).astype(np.uint8)
        for i, x in enumerate(v.astype(str)):
            out[i, :3] = table[index.get(x, 0) % len(table)]
        out[:, 3] = 200
        return out

    a = v.to_numpy(dtype=float)
    finite = a[np.isfinite(a)]
    if finite.size == 0:
        return np.tile(np.array(GREY, np.uint8), (n, 1))
    lo, hi = np.percentile(finite, [2, 98])
    norm = np.clip((a - lo) / (hi - lo) if hi > lo else np.zeros_like(a), 0, 1)
    norm = np.where(np.isfinite(norm), norm, 0.0)
    lut = (_cmap(cmap)(np.linspace(0, 1, 256))[:, :3] * 255).astype(np.uint8)
    out[:, :3] = lut[(norm * 255).astype(int)]
    out[:, 3] = 170
    return out


def pack_points(state, color_by: str = "elevation",
                cmap: str = "terrain") -> bytes:
    """The result points as one binary buffer.

    Layout, little-endian: uint32 total, uint32 drawn, then float32 x[drawn],
    float32 y[drawn], uint8 rgba[drawn * 4]. Float32 of local metres keeps
    well under a millimetre across a lot, and the whole thing is a fraction
    of the size of the same numbers as JSON.
    """
    ps = state.result
    if ps is None or len(ps) == 0:
        return struct.pack("<II", 0, 0)
    d, _ = scatter_frame(ps)
    x, y = state.site.to_local(d[E].to_numpy(), d[N].to_numpy())
    rgba = point_colours(d, color_by, cmap)
    return (struct.pack("<II", len(ps), len(d))
            + np.asarray(x, "<f4").tobytes() + np.asarray(y, "<f4").tobytes()
            + rgba.tobytes())


def unpack_points(buf: bytes) -> dict:
    """The inverse of `pack_points`, for tests and anything else in Python."""
    total, n = struct.unpack_from("<II", buf, 0)
    x = np.frombuffer(buf, "<f4", n, 8)
    y = np.frombuffer(buf, "<f4", n, 8 + 4 * n)
    rgba = np.frombuffer(buf, np.uint8, 4 * n, 8 + 8 * n).reshape(n, 4)
    return {"total": total, "x": x, "y": y, "rgba": rgba}


def points_note(state) -> str:
    ps = state.result
    if ps is None or len(ps) == 0:
        return ""
    note = f"{len(ps):,} points"
    if len(ps) > MAX_SCATTER:
        note += f" (showing {MAX_SCATTER:,})"
    return note


def spot_markers(state) -> list[list[float]]:
    """Spot layers drawn as distinct markers, not part of the surface."""
    seen: set[tuple[float, float]] = set()
    xs, ys = [], []
    for name, ps in state.layers.items():
        if name == state.active_layer or not state.visible.get(name):
            continue
        if len(ps) == 0 or len(ps) > MAX_SPOT_LAYER:
            continue
        for e, n in zip(ps.df[E].to_numpy(), ps.df[N].to_numpy()):
            # FEATURE_POINTS repeats every spot layer's rows, so the same
            # shot would otherwise be drawn twice.
            key = (round(float(e), 3), round(float(n), 3))
            if key in seen:
                continue
            seen.add(key)
            xs.append(e)
            ys.append(n)
    if not xs:
        return []
    lx, ly = state.site.to_local(np.array(xs), np.array(ys))
    return [[float(a), float(b)] for a, b in zip(lx, ly)]


def vector_lines(state) -> list[dict]:
    """Reference linework as polylines in local metres."""
    out = []
    for layer in state.vectors.values():
        for ring in layer.rings:
            if len(ring) < 2:
                continue
            x, y = state.site.to_local(ring[:, 0], ring[:, 1])
            out.append({"source": layer.source,
                        "survey_grade": layer.survey_grade,
                        "xy": np.column_stack([x, y]).round(3).tolist()})
    return out


# --- rasters -------------------------------------------------------------------

def local_extent(site, extent, de: float = 0.0, dn: float = 0.0) -> list[float]:
    x0, y0 = site.to_local(extent.xmin, extent.ymin)
    x1, y1 = site.to_local(extent.xmax, extent.ymax)
    return [float(x0 + de), float(y0 + dn), float(x1 + de), float(y1 + dn)]


def surface_png(surface, cmap: str = "terrain") -> bytes:
    """Hillshaded, colour-mapped surface, north-up, unmeasured transparent."""
    rgba = colorize(surface.z_masked, cmap if cmap in COLORMAPS else "terrain",
                    mask=surface.mask, hillshade=True, px=surface.px)
    # Row 0 of the grid is the SOUTH edge; row 0 of an image is the top.
    return _png(np.flipud(rgba))


def surface_grid(surface, site, cmap: str = "terrain") -> dict:
    """The surface for a 3D plot: local axes, heights, and a colour scale.

    Heights keep NaN for unmeasured cells (sent as null), which a surface
    plot leaves as a hole - the same refusal to colour unsurveyed ground as
    the plan view.
    """
    ny, nx = surface.z.shape
    x0, y0 = site.to_local(surface.extent.xmin, surface.extent.ymin)
    xs = (x0 + np.arange(nx) * surface.px).round(3)
    ys = (y0 + np.arange(ny) * surface.px).round(3)
    z = surface.z_masked
    finite = z[np.isfinite(z)]
    rows = [[None if not np.isfinite(v) else round(float(v), 4) for v in row]
            for row in z]
    return {
        "x": xs.tolist(), "y": ys.tolist(), "z": rows,
        "zmin": float(finite.min()) if finite.size else 0.0,
        "zmax": float(finite.max()) if finite.size else 1.0,
        "colorscale": colorscale(cmap),
        "column": surface.z_column,
    }


def colorscale(cmap: str, n: int = 16) -> list:
    """A matplotlib colour map as a Plotly colour scale."""
    table = _cmap(cmap)(np.linspace(0, 1, n))
    return [[round(float(i / (n - 1)), 4),
             "rgb({},{},{})".format(*(int(c * 255) for c in rgb[:3]))]
            for i, rgb in enumerate(table)]


def basemap_png(basemap) -> bytes:
    """A fetched raster as PNG, encoded once and kept."""
    if basemap.png is None:
        basemap.png = _png(basemap.layer.image)
    return basemap.png


def basemap_payload(state) -> list[dict]:
    """Every basemap in draw order, where it is drawn, and how.

    The alignment offset moves the PHOTO, never the measurements: the points
    are the better-known thing, and shifting them to agree with an aerial
    would be backwards. So it is applied here, to the drawn extent, and
    nowhere else.
    """
    off = state.imagery_offset
    out = []
    for name, bm in state.basemaps.items():
        out.append({
            "name": name,
            "visible": bm.visible,
            "opacity": bm.opacity,
            "terrain": bm.terrain,
            "extent": local_extent(state.site, bm.layer.extent, off.de, off.dn),
            # The rate the tile was SAMPLED at, which is set by the request
            # size, not the source's native resolution. The provider names
            # carry the real figure.
            "sampled": f"sampled at {bm.layer.px * 100:.1f} cm/px",
            "attribution": bm.layer.attribution,
            "key": id(bm.layer),
        })
    return out
