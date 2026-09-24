"""What the application says, as text.

The words matter here as much as the numbers. "97.9 ft" and "860.4 ft" look
equally plausible on screen and only one of them is on the datum you think you
are working in, so every readout says what its heights mean; a merge that
cannot be reconciled says so while it is still possible to go back out; an
export on an arbitrary datum says that too. Keeping the text in one place means
the browser, the tests and anything else all say the same thing.
"""

from __future__ import annotations

import numpy as np

from .. import qc
from ..model.pointset import ELEV, SESSION
from ..units import m_to_ft

UNSOLVED = (
    "No vertical model applied.\n\n"
    "Elevations are RAW ELLIPSOIDAL HEIGHT of the antenna - roughly 860 ft "
    "here, not the site's 100 ft datum, and not height above ground.\n\n"
    "Datum ▸ Solve local datum ties the walked data to the laser spots, "
    "which is what puts the surface on the benchmark."
)

EMPTY_BASEMAPS = ("No basemaps fetched.\n\n"
                  "Data ▸ Fetch all basemaps tries every source once and "
                  "reports which ones actually cover this lot.")


# --- the vertical datum panel ---------------------------------------------------

def merge_warning(source) -> str:
    """Name the hazard that only exists once data has been merged.

    Several sessions with no model applied is not the same situation as one
    session with no model applied: the second is merely on the wrong datum,
    the first is on several at once, and the surface steps where they meet.
    """
    if source is None or SESSION not in source.df.columns:
        return ""
    names = sorted(set(source.df[SESSION].astype(str)))
    if len(names) < 2:
        return ""
    return ("\n\nTHIS LAYER SPANS {} SESSIONS:\n  ".format(len(names))
            + "\n  ".join(names)
            + "\n\nEach was surveyed with its own antenna mount and base "
              "selection, so they sit on different datums. "
              "Datum ▸ Solve session offsets only removes the step "
              "between them without touching the benchmark.")


def vertical_text(state) -> str:
    """What the elevations currently mean, or that nothing has been solved."""
    model = state.vertical
    if model is None:
        return UNSOLVED + merge_warning(state.source)
    parts = [model.describe()]
    if model.level is not None:
        parts += ["", model.level.describe()]
    if model.sessions is not None:
        parts += ["", model.sessions.describe()]
    return "\n".join(parts)


def solve_notice(model) -> str:
    """The report shown after a solve, with anything worth acting on."""
    warnings = list(model.notes)
    if model.sessions and model.sessions.unresolved:
        warnings.append(
            "Sessions with no overlapping ground cannot be tied. Re-cover "
            "some previously surveyed ground, or shoot a permanent "
            "benchmark, on the next outing.")
    if model.level is not None and not model.level.adjustment.has_redundancy:
        warnings.append(
            "The laser network has no redundancy, so a mis-read rod would "
            "be invisible. Shoot at least two points from each pair of "
            "setups next time.")
    return model.describe() + ("\n\n" + "\n\n".join(warnings) if warnings else "")


# --- the QC readout ----------------------------------------------------------------

def qc_text(state) -> str:
    """Measured QC for what is on screen.

    Not an afterthought - measured QC beats the receiver's own accuracy
    estimates, so crossover statistics belong next to the surface they
    describe rather than in a log file somewhere.
    """
    ps = state.result
    if ps is None or len(ps) < 2:
        return ""

    column = ELEV if ps.has(ELEV) else None
    stats = (qc.crossover_stats(ps, column=column) if column
             else qc.crossover_stats(ps))
    lines = [
        "Measured QC — crossover residuals (pairs within 30 cm, >60 s apart)",
        "  " + qc.format_stats(stats),
    ]
    if stats.get("n"):
        lines.append(f"  systematic bias {stats['bias'] * 100:+.2f} cm")
    lines.append("")

    s = state.surface
    if s is not None:
        v = state.site.vertical
        if s.z_column != ELEV:
            datum = "RAW ELLIPSOIDAL (no vertical model)"
        elif v.tied_to_model:
            datum = f"tied to {v.model_frame}"
        else:
            datum = "local datum, ARBITRARY origin"
        # nan-aware: masked cells are NaN and would poison plain min/max.
        lo = float(np.nanmin(s.z_masked))
        hi = float(np.nanmax(s.z_masked))
        lines.append(
            f"Surface   {s.n_points:,} pts → {s.n_cells:,} cells → "
            f"{s.z.shape[0]}² preview   GSD {s.px * 100:.1f} cm/px   "
            f"{s.measured_fraction * 100:.1f}% measured, "
            f"{(1 - s.measured_fraction) * 100:.1f}% interpolated fill")
        lines.append(
            f"Heights   {m_to_ft(lo):.2f} – {m_to_ft(hi):.2f} ft   "
            f"relief {m_to_ft(hi - lo) * 12:.1f} in   [{datum}]")
    return "\n".join(lines)


# --- the plan view's info line --------------------------------------------------------

def info_bits(state) -> list[str]:
    """Caveats that belong on the map itself, not in a dialog."""
    bits = []
    if state.surface is not None:
        bits.append(f"{state.surface.measured_fraction * 100:.0f}% measured")
    credits = {b.layer.attribution for b in state.visible_basemaps
               if b.layer.attribution}
    bits.extend(sorted(credits))
    if any(not v.survey_grade for v in state.vectors.values()):
        bits.append("linework REFERENCE ONLY")
    off = state.imagery_offset
    if not off.zero:
        # Say so on the view itself. A silently shifted basemap is the kind
        # of thing that gets forgotten and then trusted.
        bits.append(f"imagery shifted {m_to_ft(off.magnitude_m):.2f} ft")
    return bits


# --- after an export ---------------------------------------------------------------------

def revit_notice(res: dict, site) -> tuple[str, bool]:
    """What to say after writing a Revit points file, and whether to warn."""
    notes = []
    if res["z_column"] != ELEV:
        notes.append(
            "WARNING: no vertical model applied. These are raw ellipsoidal "
            "antenna heights (~860 ft), not elevations on the site datum. "
            "Solve the local datum before importing into Revit.")
    elif not site.vertical.tied_to_model:
        notes.append(
            "NOTE: the datum is local and arbitrary. The elevations are "
            "self-consistent but sit nowhere in particular, so the "
            "toposolid will not land at the right height in the model. "
            "Datum ▸ Datum tie… once you have shot a known feature.")
    if res["over_threshold"]:
        notes.append(
            "This many points will make Revit's importer very slow. "
            "Consider a larger bin cell.")
    text = (f"{res['n_points']:,} points\n"
            f"Heights from: {res['z_column']}\n"
            f"Origin sidecar: {res['sidecar'].name}"
            + ("\n\n" + "\n\n".join(notes) if notes else ""))
    return text, res["z_column"] != ELEV


def heightmap_notice(surface) -> str:
    return (f"{surface.describe()}\n\n"
            f"Heights from: {surface.z_column}\n\n"
            "The 16-bit raster contains interpolated fill in unmeasured cells; "
            "the true mask is in the .npy alongside it.")


def field_sheet_notice(has_basemap: bool) -> str:
    return ("Print it from the browser. It is self-contained, so it needs no "
            "network in the field."
            + ("" if has_basemap else
               "\n\nNo basemap was visible, so the map is blank. Fetch "
               "imagery first if you want to find the points on the ground."))


def linework_note(layer) -> str:
    note = layer.describe()
    if not layer.survey_grade:
        note += (" - cartographic, typically 1-3 ft of positional "
                 "uncertainty. Not a surveyed boundary.")
    return note


def services_report(imagery: dict, vectors: dict) -> str:
    """Which public services answer right now. Makes network calls."""
    lines = ["Imagery:"]
    for name, p in imagery.items():
        ok, msg = p.available()
        lines.append(f"  {'OK  ' if ok else 'DOWN'}  {name} — {msg}")
    lines += ["", "Reference linework:"]
    for name, p in vectors.items():
        ok, msg = p.available()
        lines.append(f"  {'OK  ' if ok else 'DOWN'}  {name} — {msg}")
    return "\n".join(lines)
