"""The datum tie.

Tying the survey to the Revit model is not a correction applied on top of the
local datum - it is a change to which point the whole network hangs from and
what value that point is held at. Holding an arbitrary spot at 100.000 ft and
holding a garage slab shot at its real project elevation are the same operation
with different inputs.

So the tie edits exactly two things - which point, and what elevation - plus a
flag recording whether that elevation means anything outside this project.
That flag is the important one: an export reading 0.25 ft and one reading
100.00 ft look equally plausible, and only the flag distinguishes a tied datum
from a chosen number.
"""

from __future__ import annotations

HELP = (
    "The survey hangs from one shot. Until that shot is a KNOWN feature held "
    "at its real elevation, the surface is self-consistent but sits nowhere in "
    "particular.\n\n"
    "To tie it: shoot a feature whose elevation you know in the Revit model — "
    "a garage slab, a door threshold — then pick that point here and enter its "
    "project elevation."
)

TIED_NOTE = ("Exports will be labelled as tied. Only check this once the "
             "elevation above really is the feature's elevation in the model.")
LOCAL_NOTE = "Datum stays local and arbitrary. Exports will say so."


def tie_form(site, spot_ids: list) -> dict:
    """What the datum tie dialog shows."""
    v = site.vertical
    return {
        "help": HELP,
        "point": v.benchmark_point_id,
        "points": list(spot_ids),
        "elev_ft": v.benchmark_elev_ft,
        "note": v.benchmark_note,
        "frame": v.model_frame or "Revit project",
        "tied": v.tied_to_model,
        "tied_note": TIED_NOTE,
        "local_note": LOCAL_NOTE,
    }


def apply_tie(site, *, point, elev_ft: float, note: str = "",
              frame: str = "", tied: bool = False) -> None:
    """Write the tie onto the site. Raises ValueError for a bad point id.

    The point is a station: a bare integer is a SW Maps ID and is stored as
    an int, as before; anything else ("P12", "BM1") is stored as its
    canonical name.
    """
    from ..vertical import station_key

    v = site.vertical
    point_id = station_key(point)
    if point_id is None:
        raise ValueError(f"'{point}' is not a point id")
    try:
        elev = float(elev_ft)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"'{elev_ft}' is not an elevation") from exc

    v.benchmark_point_id = point_id
    v.benchmark_elev_ft = elev
    v.benchmark_note = (note or "").strip()
    v.model_frame = (frame or "").strip() or "Revit project"
    v.tied_to_model = bool(tied)
    v.name = (f"tied to {v.model_frame}" if v.tied_to_model
              else "local arbitrary")
