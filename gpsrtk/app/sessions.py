"""Comparing acquisition sessions instead of pooling them.

A merged data set is several outings, each with its own antenna mount, base
selection and sky. One pooled crossover RMS says how good they are together
and nothing about which of them is dragging the number up. This lays the same
evidence out per session:

  * how much each outing logged, and how much of it was RTK fixed;
  * how much of it the filter stack kept;
  * its internal repeatability - crossovers between its own passes;
  * where it sits relative to each other outing it overlaps, and how well a
    single constant describes that - as logged, and again after the solved
    session offsets, which is how to tell the correction worked;
  * the offset the vertical model solved for it, once there is one.

Everything is measured on the filtered, vertically corrected points of every
session, whether or not a session is currently shown: hiding one to look at
the others must not change the numbers that describe it.
"""

from __future__ import annotations

from .. import qc
from ..merge import describe_sessions
from ..model.pointset import ELEV, SESSION, Z
from .views import session_palette


def _cm(stats: dict, key: str):
    value = stats.get(key)
    return None if value is None else value * 100.0


def sessions_payload(state) -> dict:
    """Per-session quality, pairwise differences, and what is shown."""
    source = state.source
    corrected = state.corrected
    names = state.sessions
    if source is None or not names:
        return {"list": [], "pairs": [], "surface_from_shown": state.surface_from_shown,
                "column": None}

    column = ELEV if corrected is not None and corrected.has(ELEV) else Z
    empty = {"within": {}, "between": {}}
    crossovers = (qc.session_crossovers(corrected, column=column)
                  if corrected is not None else empty)
    # The raw antenna heights say how far apart the outings were logged;
    # the corrected ones say what is left after the vertical model.
    raw = (qc.session_crossovers(corrected, column=Z)
           if corrected is not None and column != Z else crossovers)
    kept = (corrected.df[SESSION].astype(str).value_counts().to_dict()
            if corrected is not None and SESSION in corrected.df.columns else {})
    offsets = dict(state.vertical.offsets or {}) if state.vertical is not None else {}
    palette = session_palette(names)
    hidden = state.hiding

    rows, starts = [], {}
    for info in describe_sessions(source):
        within = crossovers["within"].get(info.name, {"n": 0})
        minutes = None
        if info.start is not None and info.end is not None:
            try:
                minutes = (info.end - info.start).total_seconds() / 60.0
            except (TypeError, AttributeError):
                minutes = None
        starts[info.name] = info.start
        rows.append({
            "name": info.name,
            "colour": "rgb({},{},{})".format(*palette[info.name]),
            "shown": info.name not in hidden,
            "points": info.points,
            "fixed_pct": info.fixed_fraction * 100.0,
            "kept": int(kept.get(info.name, 0)),
            "minutes": minutes,
            "pairs": within.get("n", 0),
            "rms_cm": _cm(within, "rms"),
            "median_cm": _cm(within, "median_abs"),
            "offset_cm": (offsets[info.name] * 100.0 if info.name in offsets
                          else None),
        })

    pairs = []
    for (a, b), stats in raw["between"].items():
        diff = stats["bias"]
        after = (crossovers["between"].get((a, b), {}).get("bias")
                 if column != Z else None)
        # Read as "later minus earlier", which is how a change between
        # outings is naturally described.
        if _later(starts.get(a), starts.get(b)):
            a, b = b, a
            diff = -diff
            after = None if after is None else -after
        pairs.append({"earlier": a, "later": b, "n": stats["n"],
                      "diff_cm": diff * 100.0,
                      "after_cm": None if after is None else after * 100.0,
                      "scatter_cm": stats["scatter"] * 100.0})
    pairs.sort(key=lambda p: -p["n"])

    return {"list": rows, "pairs": pairs,
            "surface_from_shown": state.surface_from_shown,
            "column": column}


def _later(a, b) -> bool:
    """True when session start `a` is after `b`. Unknown times keep order."""
    try:
        return a is not None and b is not None and a > b
    except TypeError:
        return False
