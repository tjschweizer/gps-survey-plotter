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
import pandas as pd

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

    known = name in COLORMAPS or name == "magma_r"
    return matplotlib.colormaps[name if known else "terrain"]


# --- points ---------------------------------------------------------------------

def session_palette(names) -> dict[str, tuple[int, int, int]]:
    """A fixed colour per session, from the full list of sessions.

    Assigned from every session in the data, not from those currently
    drawn, so hiding one session never recolours the others - a colour that
    changed meaning between two views would make comparing them worthless.
    """
    import matplotlib

    table = (np.asarray(matplotlib.colormaps["tab10"].colors) * 255).astype(int)
    return {name: tuple(int(c) for c in table[i % len(table)])
            for i, name in enumerate(sorted(map(str, names)))}


def drawn_points(state):
    """The result rows the plan view draws: every shown session."""
    ps = state.shown(state.result)
    return None if ps is None else ps.df


def scatter_frame(d, cap: int | None = None):
    """The rows to draw, and whether they are a subsample.

    The subsample is seeded, so the same data always shows the same points
    and a redraw does not make the scatter shimmer.
    """
    cap = MAX_SCATTER if cap is None else cap
    n = len(d)
    if n > cap:
        idx = np.random.default_rng(0).choice(n, cap, replace=False)
        return d.iloc[np.sort(idx)], True
    return d, False


def point_colours(d, color_by: str = "elevation",
                  cmap: str = "terrain", sessions=None) -> np.ndarray:
    """An (n, 4) uint8 RGBA colour per row.

    `sessions` is the full list of session names, so that session colours
    stay fixed whichever of them are drawn.
    """
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
        palette = session_palette(sessions if sessions is not None
                                  else v.dropna().unique())
        for i, x in enumerate(v.astype(str)):
            out[i, :3] = palette.get(x, OTHER_FIX)
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
    shown = drawn_points(state)
    if shown is None or len(shown) == 0:
        return struct.pack("<II", 0, 0)
    d, _ = scatter_frame(shown)
    x, y = state.site.to_local(d[E].to_numpy(), d[N].to_numpy())
    rgba = point_colours(d, color_by, cmap, sessions=state.sessions)
    return (struct.pack("<II", len(shown), len(d))
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
    shown = drawn_points(state)
    n = len(shown)
    note = f"{len(ps):,} points"
    if state.surface_from_shown and state.hiding:
        total = len(state.sessions)
        note += f" from {total - len(state.hiding)} of {total} sessions"
    elif n < len(ps):
        hidden = len(state.hiding)
        note = (f"{n:,} of {len(ps):,} points - {hidden} "
                f"session{'s' if hidden != 1 else ''} hidden")
    if n > MAX_SCATTER:
        note += f" (drawing {MAX_SCATTER:,})"
    return note


def spot_markers(state) -> list[dict]:
    """Spot layers drawn as distinct markers, not part of the surface.

    Each marker says which station it is - the ID the datum dialog asks for
    - with its kind, rod reading and date, so a shot can be found on the map
    and picked by what it is rather than by a number remembered from the
    phone.
    """
    from ..model.pointset import KIND, ROD_IN, TIME
    from ..vertical import stations

    markers: dict[tuple[float, float], dict] = {}
    marks = state.site.mark_names
    for name, ps in state.layers.items():
        if name == state.active_layer or not state.visible.get(name):
            continue
        if len(ps) == 0 or len(ps) > MAX_SPOT_LAYER:
            continue
        d = ps.df
        keys = stations(ps, marks)
        for i in d.index:
            e, n = float(d[E].at[i]), float(d[N].at[i])
            # FEATURE_POINTS repeats every spot layer's rows, so the same
            # shot would otherwise be drawn twice; the copy with the custom
            # attributes fills in what the other lacks.
            key = (round(e, 3), round(n, 3))
            m = markers.setdefault(key, {"e": e, "n": n, "station": str(keys.at[i]),
                                         "kind": "", "rod": None, "date": ""})
            if not m["kind"] and KIND in d.columns and isinstance(d[KIND].at[i], str):
                m["kind"] = d[KIND].at[i]
            if m["rod"] is None and ROD_IN in d.columns and pd.notna(d[ROD_IN].at[i]):
                m["rod"] = float(d[ROD_IN].at[i])
            if not m["date"] and TIME in d.columns and pd.notna(d[TIME].at[i]):
                m["date"] = f"{d[TIME].at[i]:%Y-%m-%d}"
    out = []
    for m in markers.values():
        x, y = state.site.to_local(m.pop("e"), m.pop("n"))
        out.append({"x": float(x), "y": float(y), **m})
    return out


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


def surface_grid(surface, site, cmap: str = "terrain", datum: str = "") -> dict:
    """The surface for a 3D plot: local axes, heights, and a colour scale.

    In feet from the local origin, like the plan view's legend, and with
    the datum named: the 3D view used to say metres with no datum at all.
    All three axes are feet, so the vertical exaggeration stays true.

    Heights keep NaN for unmeasured cells (sent as null), which a surface
    plot leaves as a hole - the same refusal to colour unsurveyed ground as
    the plan view.
    """
    from .. import terrain
    from ..units import m_to_ft

    gx, gy = terrain.grid_axes(surface)
    xs = m_to_ft(np.asarray(site.to_local(gx, 0.0)[0])).round(3)
    ys = m_to_ft(np.asarray(site.to_local(0.0, gy)[1])).round(3)
    z = m_to_ft(surface.z_masked)
    finite = z[np.isfinite(z)]
    rows = [[None if not np.isfinite(v) else round(float(v), 4) for v in row]
            for row in z]
    return {
        "x": xs.tolist(), "y": ys.tolist(), "z": rows,
        "zmin": float(finite.min()) if finite.size else 0.0,
        "zmax": float(finite.max()) if finite.size else 1.0,
        "colorscale": colorscale(cmap),
        "column": surface.z_column,
        "units": "ft",
        "datum": datum,
    }


def layer_summary(ps) -> str:
    """A layer's one-line summary for the Layers panel, heights in feet.

    `PointSet.describe` stays in metres for the reports and tests that read
    it; the page shows heights in feet everywhere else, so the panel does
    too.
    """
    from ..model.pointset import FIX_FLOAT, FIX_RTK, elevation_column
    from ..units import m_to_ft

    d = ps.df
    parts = [f"{len(d):,} points"]
    if FIX in d:
        vc = d[FIX].value_counts()
        parts.append(f"fixed {int(vc.get(FIX_RTK, 0)):,} / "
                     f"float {int(vc.get(FIX_FLOAT, 0)):,}")
    if SESSION in d and len(d):
        n_sessions = d[SESSION].astype(str).nunique()
        if n_sessions > 1:
            parts.append(f"{n_sessions} sessions")
    if len(d):
        values = d[elevation_column(ps)].dropna()
        if len(values):
            parts.append(f"z {m_to_ft(values.min()):.2f}-"
                         f"{m_to_ft(values.max()):.2f} ft")
        else:
            parts.append("no height yet")
    return "  |  ".join(parts)


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


# --- terrain overlays --------------------------------------------------------------
# The same slope, drainage and contours as the printed maps (gpsrtk.io.figures),
# drawn live on the plan view instead.

SLOPE_CMAP = "magma_r"


def slope_png(surface, slope_max_pct: float = 10.0, field=None) -> bytes:
    """Slope in percent grade, north-up, unmeasured transparent."""
    import matplotlib

    from .. import terrain

    field = field or terrain.slope(surface)
    pct = field.slope_pct
    norm = np.clip(np.nan_to_num(pct, nan=0.0) / max(slope_max_pct, 1e-6), 0, 1)
    rgba = (matplotlib.colormaps[SLOPE_CMAP](norm) * 255).astype(np.uint8)
    rgba[..., 3] = np.where(np.isfinite(pct), 255, 0)
    return _png(np.flipud(rgba))


def contours_payload(surface, site, interval_m: float) -> dict:
    """Contour polylines in local metres, labelled in cm above the low point."""
    from .. import terrain

    levels = []
    for lv in terrain.contours(surface, interval_m):
        lines = []
        for line in lv.lines:
            x, y = site.to_local(line[:, 0], line[:, 1])
            lines.append(np.column_stack([x, y]).round(3).tolist())
        levels.append({"cm": round(lv.above_low_m * 100.0, 3),
                       "major": lv.major, "lines": lines})
    low, _ = terrain.relief(surface)
    return {"interval_cm": interval_m * 100.0, "low_m": low, "levels": levels}


def drainage_payload(surface, site, spacing_m: float, field=None) -> dict:
    """Downhill arrows: [x, y, down_e, down_n, slope %] in local metres."""
    from .. import terrain

    arrows = terrain.drainage_arrows(surface, spacing_m, field)
    out = []
    for a in arrows:
        x, y = site.to_local(a.e, a.n)
        out.append([round(float(x), 3), round(float(y), 3), round(a.down_e, 4),
                    round(a.down_n, 4), round(a.slope_pct, 2)])
    return {"spacing_m": spacing_m, "arrows": out}


def terrain_payload(state, field=None) -> dict | None:
    """Numbers the plan view's legend and the terrain controls need."""
    from .. import terrain
    from ..units import m_to_ft
    from ..vertical import height_label

    s = state.surface
    if s is None:
        return None
    field = field or terrain.slope(s)
    low, high = terrain.relief(s)
    measured = s.z[~s.mask]
    lo, hi = (np.percentile(measured, [0.5, 99.5]) if measured.size
              else (low, high))
    datum = height_label(state.vertical, state.site, s.z_column, short=True)
    return {
        "relief_cm": (high - low) * 100.0,
        "mapped_m2": terrain.mapped_area_m2(s),
        "slope": field.stats(),
        # The elevation colours span these heights (the percentile clip the
        # surface image uses), so the legend can say what they mean.
        "elevation": {"lo_ft": m_to_ft(float(lo)), "hi_ft": m_to_ft(float(hi)),
                      "datum": datum},
    }


def legend_scales(n: int = 11) -> dict:
    """Colour stops for every colour map the plan view can show."""
    return {name: colorscale(name, n) for name in [*COLORMAPS, SLOPE_CMAP]}
