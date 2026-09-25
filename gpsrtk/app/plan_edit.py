"""Editing a shot plan: the rules behind the table and the map.

Division of labour, chosen deliberately: the MAP edits position, the TABLE
edits everything else. Dragging a numbered marker is the one thing a map does
better than a grid; deleting a point or typing a rod reading are things a grid
does better than a map. Both are only views, though. The rules about what an
edit means - that ticking "fixed" makes the point an RTK shot, that a measured
point cannot be dragged, that a latitude typed into the longitude box is caught
- live here, so every view obeys the same ones and they can be tested without
drawing anything.

A marker is drawn at the point's BEST KNOWN position, not at where it was
planned. Once a shot has been measured, the click that placed it is history:
the measurement is what the point is. So a measured point is locked - dragging
it would edit the planned position, which is no longer what is drawn, and the
marker would appear not to move.

Edits that fail raise `PlanEditError` with a message meant for the user. Edits
that are allowed but probably a mistake raise `NeedsConfirmation`; calling
again with `confirm=True` goes ahead.

An outline is edited in the same two places: its corners are markers and
rows like any other shot, and what the outline itself is - its name, its
kind, whether it is keep-out - is edited in the list of outlines.
"""

from __future__ import annotations

import math
import re

from ..plan import (OUTLINE_KINDS, PURPOSES, SIGMA_M, Plan, PlanLine,
                    PlannedPoint, PlannedSetup, Tie, is_guide, is_terrain,
                    outline_slug, purpose_group)
from ..units import M_PER_FT, ft_to_m, m_to_ft, parse_rod

NAVIGATE, ADD_POINT, ADD_LINE, ADD_OUTLINE, ADD_SETUP = (
    "navigate", "add_point", "add_line", "add_outline", "add_setup")
MODES = (NAVIGATE, ADD_POINT, ADD_LINE, ADD_OUTLINE, ADD_SETUP)
MODE_HINTS = {
    ADD_POINT: "Click on the map to place a shot.",
    ADD_LINE: "Click each vertex; double-click to finish the line.",
    ADD_OUTLINE: "Click each corner; double-click the last one to close the "
                 "outline.",
    ADD_SETUP: "Click where the laser will stand.",
    NAVIGATE: "Drag a numbered marker to move it.",
}

# Longest outline name: it has to fit the Line column and the field sheet.
MAX_NAME = 40

METHODS = ["planned", "rtk", "taped", "station_offset"]

# The editable columns of the readings table, in the printed sheet's order.
FIELDS = ("rod", "purpose", "setup", "fix", "method", "line", "note")

# Frames a measured position can be typed in. A receiver read in the field
# gives lat/lon or UTM; a value read off the plan or the field sheet is in
# local feet. Converting by hand is exactly where a sign or a digit gets lost.
COORD_FRAMES = {
    "local ft": ("east (ft)", "north (ft)"),
    "UTM m": ("easting (m)", "northing (m)"),
    "lat/lon": ("longitude", "latitude"),
}

# A typed position this far from the plan is far more likely to be a mistyped
# frame than a real shot, and silently accepting it would move the point
# somewhere unfindable.
FAR_M = 500.0

# Below this the leader is shorter than the marker and just makes a smudge.
MIN_LEADER_M = 0.15

FIXED_WORDS = ("y", "yes", "1", "true", "x", "fixed")

# Rod readings outside this range are allowed but asked about. Under a foot
# is most often feet typed as inches ("5.26"); over 200 in is past the top of
# most rods.
ROD_LOW_IN = 12.0
ROD_HIGH_IN = 200.0


class PlanEditError(ValueError):
    """An edit that was refused, with a title and a message for the user."""

    def __init__(self, title: str, message: str):
        super().__init__(message)
        self.title = title
        self.message = message


class NeedsConfirmation(Exception):
    """An edit that is allowed but looks like a mistake."""

    def __init__(self, title: str, message: str):
        super().__init__(message)
        self.title = title
        self.message = message


def _message(exc: Exception) -> str:
    """An exception's own words. `str(KeyError)` would wrap them in quotes."""
    return str(exc.args[0]) if exc.args else str(exc)


def _point(plan: Plan, number: int) -> PlannedPoint:
    point = plan.by_number(number)
    if point is None:
        raise PlanEditError("Shot plan", f"There is no point {number}.")
    return point


# --- what a point is showing ---------------------------------------------------

def best_position(plan: Plan, number: int) -> tuple[float, float, str, float, str]:
    """`Plan.resolve`, but never raising.

    A derived position can become unresolvable after the fact - a tape tie to
    a point that has since been deleted. The point still has to be drawn and
    listed, so it falls back to its planned mark and says why.
    """
    try:
        e, n, method, sigma = plan.resolve(number)
        return e, n, method, sigma, ""
    except (KeyError, ValueError) as exc:
        p = plan.by_number(number)
        return (p.planned_e, p.planned_n, "planned", SIGMA_M["planned"],
                _message(exc))


def is_locked(plan: Plan, number: int) -> bool:
    """True when the marker sits on a measurement rather than on a click.

    Anything `resolve` answers with other than "planned" was derived from
    field data - an RTK position, a taped trilateration, a station and offset.
    Dragging any of those is meaningless: it would edit the planned position,
    which is not what is drawn.
    """
    if plan.by_number(number) is None:
        return False
    return best_position(plan, number)[2] != "planned"


def line_of(plan: Plan, number: int) -> str:
    return next((ln.line_id for ln in plan.lines if number in ln.numbers), "")


def detail_text(plan: Plan, site, number: int, kept_out_by: str = "") -> str:
    """The one-line description above the position editor.

    `kept_out_by` names the keep-out outline a terrain shot stands inside.
    """
    point = _point(plan, number)
    e, n, method, sigma, problem = best_position(plan, number)
    le, ln = site.to_local(e, n)
    measured = " (measured)" if point.observed_e is not None else ""
    text = (f"Point {point.number}: {method}{measured}, about "
            f"{m_to_ft(sigma):.1f} ft horizontally "
            f"({m_to_ft(le):.1f}, {m_to_ft(ln):.1f} ft from origin)")
    if problem:
        text += f". Could not use its {point.method} position: {problem}"
    if kept_out_by:
        text += (f". It is inside keep-out outline {kept_out_by}, so it is "
                 "left out of the surface")
    return text


def kept_out_terrain(plan: Plan) -> dict[int, str]:
    """Terrain shots standing inside a keep-out outline: number -> outline.

    A spot placed inside the house is almost always a misclick, and one
    inside a bed is not on the lawn the surface describes. Either way it is
    left out, and the point says so rather than silently not counting.
    """
    import numpy as np

    from ..surface import inside_polygons

    areas = plan.keep_out_areas()
    shots = [p for p in plan.points if is_terrain(p.purpose)]
    if not areas or not shots:
        return {}
    xy = np.array([plan.drawn_position(p.number) for p in shots], dtype=float)
    out: dict[int, str] = {}
    for name, poly in areas:
        inside = inside_polygons([poly], xy[:, 0], xy[:, 1])
        for p, hit in zip(shots, inside):
            if hit:
                out.setdefault(p.number, name)
    return out


def selection_hint(count: int) -> str:
    if count == 0:
        return "Select a row."
    return (f"{count} points selected. Delete removes them all; "
            "select a single row to edit its position.")


def coverage_text(plan: Plan) -> str:
    cov = plan.coverage()
    return (f"{cov['observed']} of {cov['planned']} shot"
            + (f" · {cov['outstanding']} outstanding" if cov["outstanding"]
               else " · complete"))


def setup_choices(plan: Plan) -> list[str]:
    named = [s.name for s in plan.setups]
    return [""] + named + sorted({p.setup for p in plan.points if p.setup}
                                 - set(named))


# --- the readings table ----------------------------------------------------------

def edit_cell(plan: Plan, number: int, field: str, text, *,
              confirm: bool = False) -> None:
    """Apply one edited table cell."""
    point = _point(plan, number)
    text = "" if text is None else str(text).strip()

    if field == "rod":
        if not text:
            point.rod_in = None
            return
        try:
            inches = parse_rod(text)
        except ValueError as exc:
            raise PlanEditError(
                "Rod reading",
                f"{_message(exc)[:1].upper()}{_message(exc)[1:]}.\n\nWrite "
                "inches (63 1/4), feet and inches (5' 3 1/4\") or a grade "
                "rod reading (5-3-1/4).") from None
        if inches < ROD_LOW_IN and not confirm:
            raise NeedsConfirmation(
                "Rod reading",
                f"'{text}' reads as {inches:g} inches - less than a foot.\n\n"
                "Did you mean feet? A bare number is inches; feet need a "
                f"unit, as in {text} ft or 5' 3 1/4\". Use {inches:g} in anyway?")
        if inches > ROD_HIGH_IN and not confirm:
            raise NeedsConfirmation(
                "Rod reading",
                f"'{text}' reads as {inches:g} inches "
                f"({inches / 12:.2f} ft), which is longer than "
                "most rods. Use it anyway?")
        point.rod_in = inches
    elif field == "fix":
        # Only a FIXED solution is worth taking over the planned position;
        # a float fix is worse than the click, so anything else clears it.
        fixed = text.lower() in FIXED_WORDS
        point.fix = 4 if fixed else None
        if fixed and point.method == "planned":
            point.method = "rtk"
        elif not fixed and point.method == "rtk":
            point.method = "planned"
    elif field == "setup":
        point.setup = text
    elif field == "note":
        point.observed_note = text
    elif field == "purpose":
        try:
            plan.set_purpose(number, text)
        except ValueError:
            raise PlanEditError("Purpose",
                                f"'{text}' is not a known purpose.") from None
    elif field == "line":
        try:
            plan.assign_line(number, text or None)
        except KeyError as exc:
            raise PlanEditError("Line", _message(exc)) from None
    elif field == "method":
        if text not in METHODS:
            raise PlanEditError("Position method", f"'{text}' is not a method.")
        point.method = text
    else:
        raise PlanEditError("Shot plan", f"'{field}' is not an editable column.")


# --- measured coordinates --------------------------------------------------------

def format_coords(site, frame: str, e: float, n: float) -> tuple[str, str]:
    """A projected position written out in the given frame."""
    if frame == "local ft":
        le, ln = site.to_local(e, n)
        x, y, places = m_to_ft(le), m_to_ft(ln), 2
    elif frame == "UTM m":
        x, y, places = e, n, 3
    elif frame == "lat/lon":
        from pyproj import Transformer

        tx = Transformer.from_crs(f"EPSG:{site.epsg}", "EPSG:4326",
                                  always_xy=True)
        x, y = tx.transform(e, n)
        places = 9
    else:
        raise PlanEditError("Measured position", f"'{frame}' is not a frame.")
    return f"{x:.{places}f}", f"{y:.{places}f}"


def parse_coords(site, frame: str, x_text, y_text) -> tuple[float, float]:
    """Typed coordinates in a frame -> projected easting and northing."""
    try:
        x = float(str(x_text).strip())
        y = float(str(y_text).strip())
    except ValueError:
        raise PlanEditError(
            "Measured position",
            "Both coordinates must be numbers. Leave them blank and press "
            "'Clear, use planned' to fall back to the planned mark.") from None

    if frame == "local ft":
        return site.to_projected(ft_to_m(x), ft_to_m(y))
    if frame == "UTM m":
        return x, y
    if frame == "lat/lon":
        from pyproj import Transformer

        if not (-180 <= x <= 180 and -90 <= y <= 90):
            raise PlanEditError(
                "Measured position",
                "longitude must be -180..180 and latitude -90..90; "
                "the fields may be the wrong way round")
        tx = Transformer.from_crs("EPSG:4326", f"EPSG:{site.epsg}",
                                  always_xy=True)
        return tx.transform(x, y)
    raise PlanEditError("Measured position", f"'{frame}' is not a frame.")


def set_measured_position(plan: Plan, site, number: int, frame: str,
                          x_text, y_text, *, confirm: bool = False) -> str:
    """Record a typed-in measured position. Returns what happened."""
    point = _point(plan, number)
    e, n = parse_coords(site, frame, x_text, y_text)

    away = max(abs(e - point.planned_e), abs(n - point.planned_n))
    if away > FAR_M and not confirm:
        raise NeedsConfirmation(
            "Measured position",
            f"That places point {point.number} {m_to_ft(away):,.0f} ft from "
            "where it was planned.\n\nThat usually means the frame is wrong, "
            "or east and north are swapped. Use it anyway?")

    point.observed_e, point.observed_n = float(e), float(n)
    point.method = "rtk"
    point.fix = 4
    le, ln = site.to_local(e, n)
    moved = math.hypot(e - point.planned_e, n - point.planned_n)
    return (f"Measured position set: {m_to_ft(le):.2f}, {m_to_ft(ln):.2f} ft "
            f"from origin — {m_to_ft(moved):.2f} ft from the planned mark.")


def clear_measured_position(plan: Plan, number: int) -> str:
    point = _point(plan, number)
    point.observed_e = point.observed_n = None
    point.fix = None
    point.method = "planned"
    return "Measured position cleared; the planned mark stands."


# --- derived positions ------------------------------------------------------------

def apply_tape(plan: Plan, number: int, tie_a, dist_a_ft: float,
               tie_b, dist_b_ft: float) -> str:
    """Position from two taped distances. Nothing changes if it cannot solve."""
    point = _point(plan, number)
    before = (list(point.ties), point.method)
    try:
        point.ties = [Tie(int(str(tie_a).strip()), ft_to_m(float(dist_a_ft))),
                      Tie(int(str(tie_b).strip()), ft_to_m(float(dist_b_ft)))]
        point.method = "taped"
        _, _, stats = plan.trilaterate(point)
    except (ValueError, KeyError) as exc:
        point.ties, point.method = before
        raise PlanEditError("Taped position", _message(exc)) from None

    residual = m_to_ft(stats["max_residual_m"]) * 12
    return (f"Solved from {stats['n_ties']} ties, dof {stats['dof']}"
            + (f", worst residual {residual:.1f} in" if stats["dof"] > 0 else
               " — exactly determined, so no check on the tape"))


def apply_station_offset(plan: Plan, number: int, line_id: str,
                         station_ft: float, offset_ft: float) -> str:
    """Position from a station along a line and a perpendicular offset."""
    point = _point(plan, number)
    before = (point.method, point.ref_line, point.station_m, point.offset_m)
    point.method = "station_offset"
    point.ref_line = line_id or ""
    try:
        point.station_m = ft_to_m(float(station_ft))
        point.offset_m = ft_to_m(float(offset_ft))
        plan.station_offset(point)
    except (ValueError, TypeError) as exc:
        (point.method, point.ref_line,
         point.station_m, point.offset_m) = before
        raise PlanEditError("Station and offset", _message(exc)) from None
    return (f"Placed along {point.ref_line}; positive offset is left of travel "
            "from the line's first vertex toward its last.")


# --- adding, moving, removing -----------------------------------------------------

def add_point(plan: Plan, e: float, n: float) -> PlannedPoint:
    return plan.add_point(float(e), float(n), purpose="spot")


def add_setup(plan: Plan, e: float, n: float) -> PlannedSetup:
    """A laser setup, lettered in the order they are placed."""
    setup = PlannedSetup(name=chr(ord("A") + len(plan.setups)),
                         e=float(e), n=float(n))
    plan.setups.append(setup)
    return setup


def add_line(plan: Plan, vertices) -> PlanLine | None:
    """A break line from clicked vertices. A single click is not a line."""
    vertices = [(float(e), float(n)) for e, n in vertices]
    if len(vertices) < 2:
        return None
    index = len(plan.lines) + 1
    while plan.line(f"line-{index}") is not None:
        index += 1
    return plan.add_line(f"line-{index}", vertices)


def move_point(plan: Plan, number: int, e: float, n: float) -> bool:
    """Move a planned mark. A measured point is never moved; returns False.

    The drag is already disabled for a locked marker, but a request that
    arrives anyway must never quietly rewrite the plan.
    """
    point = _point(plan, number)
    if is_locked(plan, number):
        return False
    point.planned_e, point.planned_n = float(e), float(n)
    return True


def delete_points(plan: Plan, numbers, *, confirm: bool = False) -> list[int]:
    """Remove points. Confirmed only in bulk.

    Deleting one point by mistake is obvious and trivially redone; deleting
    thirty is neither, and a run selected by shift-click is easy to get wrong
    by a screenful.
    """
    numbers = [int(n) for n in numbers if plan.by_number(int(n)) is not None]
    if len(numbers) > 1 and not confirm:
        raise NeedsConfirmation(
            "Delete points",
            f"Delete {len(numbers)} points ("
            + ", ".join(f"#{n}" for n in numbers[:8])
            + (", ..." if len(numbers) > 8 else "")
            + ")?\n\nAny readings already typed against them go too. Numbers "
            "are never reused, so the ones left keep the numbers on your "
            "field sheet.")
    for number in numbers:
        plan.remove_point(number)
    return numbers


def insert_vertex(plan: Plan, number: int) -> PlannedPoint:
    """Add a vertex after this one, halfway to the next.

    New numbers always go on the end, but the vertex belongs in the middle of
    the RUN - the sheet is ordered by number, the line by shape. An
    outline's last corner is followed by its first, and a new corner is shot
    as the outline's corners are.
    """
    _point(plan, number)
    line = next((ln for ln in plan.lines if number in ln.numbers), None)
    if line is None:
        raise PlanEditError(
            "Insert vertex",
            "Select a break line vertex or an outline corner. Loose shots "
            "are not part of a line.")
    index = line.numbers.index(number)
    if index + 1 >= len(line.numbers) and not line.closed:
        raise PlanEditError(
            "Insert vertex",
            "That is the last vertex; there is nothing to insert before.")
    a = plan.by_number(line.numbers[index])
    b = plan.by_number(line.numbers[(index + 1) % len(line.numbers)])
    purpose = OUTLINE_KINDS[line.kind][0] if line.is_outline else "breakline"
    new = plan.add_point((a.planned_e + b.planned_e) / 2,
                         (a.planned_n + b.planned_n) / 2, purpose=purpose)
    line.numbers.insert(index + 1, new.number)
    return new


# --- outlines ----------------------------------------------------------------------

def _outline(plan: Plan, line_id: str) -> PlanLine:
    line = plan.line(str(line_id))
    if line is None or not line.is_outline:
        raise PlanEditError("Outline", f"There is no outline named {line_id}.")
    return line


def _outline_name(plan: Plan, kind: str) -> str:
    """The first free "building-1", "property-line-2" for this kind."""
    stem = outline_slug(kind)
    index = 1
    while plan.line(f"{stem}-{index}") is not None:
        index += 1
    return f"{stem}-{index}"


def add_outline(plan: Plan, vertices, kind: str = "building") -> PlanLine | None:
    """An outline from clicked corners. Fewer than three is not an area.

    The map closes a drawn polygon by repeating its first corner; that
    repeat is not a corner of its own.
    """
    if kind not in OUTLINE_KINDS:
        raise PlanEditError("Outline", f"'{kind}' is not a kind of outline.")
    vertices = [(float(e), float(n)) for e, n in vertices]
    if len(vertices) > 1 and vertices[0] == vertices[-1]:
        vertices = vertices[:-1]
    if len(vertices) < 3:
        return None
    return plan.add_outline(_outline_name(plan, kind), vertices, kind)


def rename_outline(plan: Plan, line_id: str, name) -> str:
    """Give an outline a name of your own: "house", "garden shed".

    Anything positioned along the outline by station and offset follows it.
    """
    line = _outline(plan, line_id)
    name = " ".join(str(name or "").split())
    if not name:
        raise PlanEditError("Outline", "An outline needs a name.")
    if len(name) > MAX_NAME:
        raise PlanEditError("Outline",
                            f"Keep the name to {MAX_NAME} characters or fewer.")
    if name == line.line_id:
        return name
    if plan.line(name) is not None:
        raise PlanEditError("Outline",
                            f"There is already a line or outline named {name}.")
    for p in plan.points:
        if p.ref_line == line.line_id:
            p.ref_line = name
    line.line_id = name
    return name


def set_outline_kind(plan: Plan, line_id: str, kind: str) -> str:
    """Say what an outline is. Returns its name, which may have changed.

    Corners still shot as the old kind's corners become the new kind's; a
    corner given a purpose of its own keeps it. Keep-out takes the new
    kind's default, and a name the outline was given automatically
    ("building-1") follows the kind.
    """
    line = _outline(plan, line_id)
    if kind not in OUTLINE_KINDS:
        raise PlanEditError("Outline", f"'{kind}' is not a kind of outline.")
    if kind == line.kind:
        return line.line_id
    old_purpose = OUTLINE_KINDS[line.kind][0]
    purpose, keep_out = OUTLINE_KINDS[kind]
    for number in line.numbers:
        p = plan.by_number(number)
        if p is not None and p.purpose == old_purpose:
            p.purpose = purpose
    automatic = re.fullmatch(rf"{re.escape(outline_slug(line.kind))}-\d+",
                             line.line_id)
    line.kind = kind
    line.keep_out = keep_out and line.closed
    if automatic:
        rename_outline(plan, line.line_id, _outline_name(plan, kind))
    return line.line_id


def set_outline_keep_out(plan: Plan, line_id: str, keep_out: bool) -> None:
    line = _outline(plan, line_id)
    if keep_out and not line.closed:
        raise PlanEditError(
            "Keep-out",
            f"{line.line_id} is open, so it has no inside to keep out. Close "
            "it first.")
    line.keep_out = bool(keep_out)


def set_outline_closed(plan: Plan, line_id: str, closed: bool) -> None:
    """Close an outline, or open it into a run - a fence along one side.

    An open outline has no inside, so opening one also ends keep-out.
    """
    line = _outline(plan, line_id)
    line.closed = bool(closed)
    if not line.closed:
        line.keep_out = False


def delete_outline(plan: Plan, line_id: str, *, confirm: bool = False) -> list[int]:
    """Remove an outline and its corners. Always confirmed: it is several
    points at once, and any of them may carry a reading or a measurement."""
    line = _outline(plan, line_id)
    numbers = [n for n in line.numbers if plan.by_number(n) is not None]
    if not confirm:
        measured = [n for n in numbers if plan.by_number(n).has_reading
                    or is_locked(plan, n)]
        raise NeedsConfirmation(
            "Delete outline",
            f"Delete {line.line_id} and its {len(numbers)} corners ("
            + ", ".join(f"#{n}" for n in numbers[:8])
            + (", ..." if len(numbers) > 8 else "") + ")?"
            + (f"\n\n{len(measured)} of them carry a reading or a measured "
               "position, which go too." if measured else "")
            + "\n\nNumbers are never reused, so the other shots keep the "
              "numbers on your field sheet.")
    for number in numbers:
        plan.remove_point(number)
    plan.lines.remove(line)
    return numbers


def outline_summary(plan: Plan, line: PlanLine) -> str:
    """What an outline measures, from its corners where they are best known.

    A four-cornered building or shed also gives its diagonals, which a
    rectangle has equal: the check that found the soffit corners 1.1 ft out.
    Only once all four are located does the difference mean anything about
    the building rather than about the clicks.
    """
    import numpy as np

    numbers = [n for n in line.numbers if plan.by_number(n) is not None]
    located = sum(1 for n in numbers if is_locked(plan, n))
    word = "corner" if line.closed else "vertex"
    plural = "corners" if line.closed else "vertices"
    text = (f"{len(numbers)} {word if len(numbers) == 1 else plural}, "
            f"{located} located")
    xy = np.array(plan.outline_vertices(line), dtype=float)
    if len(xy) < (3 if line.closed else 2):
        return text + (" - needs at least three corners" if line.closed
                       else "")
    ring = np.vstack([xy, xy[:1]]) if line.closed else xy
    length = float(np.hypot(*np.diff(ring, axis=0).T).sum())
    if line.closed:
        x, y = xy[:, 0], xy[:, 1]
        area = 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))
        text += (f" · {area / M_PER_FT ** 2:,.0f} sq ft"
                 f" · perimeter {m_to_ft(length):,.1f} ft")
    else:
        text += f" · length {m_to_ft(length):,.1f} ft"
    if line.closed and line.kind in ("building", "shed") and len(xy) == 4:
        d1 = m_to_ft(float(np.hypot(*(xy[2] - xy[0]))))
        d2 = m_to_ft(float(np.hypot(*(xy[3] - xy[1]))))
        text += (f" · diagonals {d1:.2f} / {d2:.2f} ft "
                 f"({abs(d1 - d2):.2f} ft apart"
                 + ("" if located == 4 else ", from clicks") + ")")
    return text


# --- tie transects -----------------------------------------------------------------

TIE_PURPOSE = "tie transect"
TIE_CELL_M = 0.5            # the surface's bin size
TIE_MAX_GAP_M = 2.0         # a run may jump a gap this long
TIE_MIN_LENGTH_M = 5.0
TIE_PER_AXIS = 2


def tie_transect_runs(e, n, sessions=None, keep_out=()) -> list[dict]:
    """Up to two east-west and two north-south runs over well-covered ground.

    Each candidate is a row (or column) of 0.5 m cells, clipped to its
    longest run of covered cells - gaps of up to 2 m allowed, 5 m long at
    least - and scored by how many sessions cover its cells, so ground every
    outing reached is preferred. The two best of each direction are chosen
    at least a third of the lot apart (5 m at least), so they cross the lot
    rather than bunching on one well-mown strip. Positions are cell centres,
    anchored at 0,0 like the site origin.

    `keep_out` polygons are ground a run must not use or cross: points inside
    them do not count as cover, and a gap is not jumped where it runs
    through one - a 1.5 m bed is narrower than the gap allowed.
    """
    import numpy as np
    import pandas as pd

    from ..surface import inside_polygons

    e, n = np.asarray(e, dtype=float), np.asarray(n, dtype=float)
    ok = np.isfinite(e) & np.isfinite(n)
    if keep_out:
        ok &= ~inside_polygons(keep_out, e, n)
    if not ok.any():
        return []
    frame = pd.DataFrame({
        "ix": np.floor(e[ok] / TIE_CELL_M).astype(np.int64),
        "iy": np.floor(n[ok] / TIE_CELL_M).astype(np.int64),
        "s": (np.asarray(sessions, dtype=object)[ok] if sessions is not None
              else 0)})
    cover = frame.groupby(["ix", "iy"])["s"].nunique().reset_index()
    gap_cells = int(round(TIE_MAX_GAP_M / TIE_CELL_M))

    runs = []
    for axis, along, across in (("E-W", "ix", "iy"), ("N-S", "iy", "ix")):
        candidates = []
        for row, part in cover.groupby(across):
            part = part.sort_values(along)
            pos = part[along].to_numpy()
            weight = part["s"].to_numpy()
            step = np.diff(pos) - 1
            breaks = np.flatnonzero(step > gap_cells) + 1
            if keep_out:
                crossing = [i + 1 for i in np.flatnonzero((step > 0) & (step <= gap_cells))
                            if _gap_is_kept_out(keep_out, axis, row, pos[i], pos[i + 1])]
                breaks = np.union1d(breaks, crossing).astype(int)
            for idx in np.split(np.arange(len(pos)), breaks):
                length = (pos[idx[-1]] - pos[idx[0]] + 1) * TIE_CELL_M
                if length >= TIE_MIN_LENGTH_M:
                    candidates.append((int(weight[idx].sum()), length, int(row),
                                       int(pos[idx[0]]), int(pos[idx[-1]])))
        if not candidates:
            continue
        rows = cover[across]
        span = (rows.max() - rows.min() + 1) * TIE_CELL_M
        spacing = max(5.0, span / 3.0)
        chosen = []
        for c in sorted(candidates, key=lambda c: (-c[0], -c[1], c[2])):
            if all(abs(c[2] - o[2]) * TIE_CELL_M >= spacing for o in chosen):
                chosen.append(c)
            if len(chosen) == TIE_PER_AXIS:
                break
        for _, length, row, first, last in sorted(chosen, key=lambda c: c[2]):
            mid = (row + 0.5) * TIE_CELL_M
            a, b = (first + 0.5) * TIE_CELL_M, (last + 0.5) * TIE_CELL_M
            ends = [(a, mid), (b, mid)] if axis == "E-W" else [(mid, a), (mid, b)]
            runs.append({"axis": axis, "vertices": ends, "length_m": length})
    return runs


def _gap_is_kept_out(keep_out, axis: str, row: int, first: int, last: int) -> bool:
    """Whether any cell between two covered cells of a row is kept out."""
    import numpy as np

    from ..surface import inside_polygons

    along = (np.arange(first + 1, last) + 0.5) * TIE_CELL_M
    across = np.full(len(along), (row + 0.5) * TIE_CELL_M)
    x, y = (along, across) if axis == "E-W" else (across, along)
    return bool(inside_polygons(keep_out, x, y).any())


def add_tie_transects(plan: Plan, e, n, sessions=None) -> str:
    """Replace the plan's tie transects with new ones. Returns what happened.

    Overlap between outings is luck unless it is planned, and an outing
    without it cannot be recovered. Walking or mowing these lines first
    makes the next outing share ground with the ones already loaded. They
    keep off the plan's keep-out outlines.
    """
    runs = tie_transect_runs(e, n, sessions,
                             keep_out=[xy for _, xy in plan.keep_out_areas()])
    if not runs:
        raise PlanEditError(
            "Tie transects",
            f"No run of covered ground {TIE_MIN_LENGTH_M:g} m long was found. "
            "Load an outing first.")
    for ln in [ln for ln in plan.lines if ln.kind == "transect"
               and all(is_guide(getattr(plan.by_number(k), "purpose", ""))
                       for k in ln.numbers)]:
        for number in list(ln.numbers):
            plan.remove_point(number)
        plan.lines.remove(ln)
    counts: dict[str, int] = {}
    made = []
    for run in runs:
        key = "EW" if run["axis"] == "E-W" else "NS"
        counts[key] = counts.get(key, 0) + 1
        line_id = f"tie-{key}-{counts[key]}"
        plan.add_line(line_id, run["vertices"], kind="transect",
                      purpose=TIE_PURPOSE,
                      note="walk or mow this first on the next outing")
        made.append(f"{line_id} ({run['axis']}, {run['length_m']:.1f} m)")
    return (f"{len(made)} tie transects through the best-covered ground: "
            + ", ".join(made) + ".\n\nWalk or mow these first on the next "
            "outing. That gives it ground in common with the outings already "
            "loaded, so its offset can be solved. They are guides, not shots: "
            "they have no rod rows and never enter the level network.")


# --- what the views draw -----------------------------------------------------------

def plan_payload(plan: Plan, site) -> dict:
    """Everything the table and the map need, in local metres."""
    points, leaders = [], []
    kept_out = kept_out_terrain(plan)
    for p in sorted(plan.points, key=lambda q: q.number):
        e, n, method, sigma, problem = best_position(plan, p.number)
        x, y = site.to_local(e, n)
        px, py = site.to_local(p.planned_e, p.planned_n)
        locked = method != "planned"
        if locked and math.hypot(px - x, py - y) >= MIN_LEADER_M:
            leaders.append([[px, py], [x, y]])

        observed = None
        if p.observed_e is not None and p.observed_n is not None:
            observed = {frame: format_coords(site, frame, p.observed_e,
                                             p.observed_n)
                        for frame in COORD_FRAMES}
        points.append({
            "number": p.number,
            "purpose": p.purpose,
            "group": purpose_group(p.purpose),
            "line": line_of(plan, p.number),
            "setup": p.setup,
            "rod": "" if p.rod_in is None else f"{p.rod_in:g}",
            "fix": "yes" if p.fix == 4 else "",
            "method": p.method,
            "note": p.observed_note or p.note,
            "has_reading": p.has_reading,
            "guide": is_guide(p.purpose),
            "locked": locked,
            "position_method": method,
            "x": x, "y": y, "px": px, "py": py,
            "tooltip": (f"#{p.number} {p.purpose} — {method}"
                        + ("; position measured, drag disabled" if locked
                           else "; planned position, drag to move")),
            "detail": detail_text(plan, site, p.number, kept_out.get(p.number, "")),
            "kept_out": p.number in kept_out,
            "observed": observed,
            "ties": [{"ref": t.ref_number, "ft": t.distance_ft}
                     for t in p.ties],
            "ref_line": p.ref_line,
            "station_ft": m_to_ft(p.station_m or 0.0),
            "offset_ft": m_to_ft(p.offset_m or 0.0),
        })

    drawn = {pt["number"]: (pt["x"], pt["y"]) for pt in points}
    lines, outlines = [], []
    for ln in plan.lines:
        xy = [list(drawn[num]) for num in ln.numbers if num in drawn]
        if ln.closed and len(xy) > 2:
            xy.append(xy[0])
        lines.append({"line_id": ln.line_id, "numbers": list(ln.numbers),
                      "closed": ln.closed, "xy": xy, "kind": ln.kind,
                      "outline": ln.is_outline,
                      "keep_out": ln.is_outline and ln.keep_out and ln.closed})
        if ln.is_outline:
            outlines.append({"line_id": ln.line_id, "kind": ln.kind,
                             "keep_out": ln.keep_out, "closed": ln.closed,
                             "numbers": list(ln.numbers),
                             "summary": outline_summary(plan, ln)})

    setups = []
    for s in plan.setups:
        if s.e is None or s.n is None:
            continue
        x, y = site.to_local(s.e, s.n)
        setups.append({"name": s.name, "x": x, "y": y})

    return {
        "points": points,
        "lines": lines,
        "outlines": outlines,
        "outline_kinds": [{"kind": k, "keep_out": keep}
                          for k, (_, keep) in OUTLINE_KINDS.items()],
        "setups": setups,
        "leaders": leaders,
        "coverage": coverage_text(plan),
        "line_ids": [ln.line_id for ln in plan.lines],
        "setup_choices": setup_choices(plan),
        # Guide purposes are placed by Plan > Add tie transects, not typed.
        "purposes": [p for p in PURPOSES if not is_guide(p)],
        "methods": METHODS,
        "frames": {k: list(v) for k, v in COORD_FRAMES.items()},
        "hints": MODE_HINTS,
    }
