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
"""

from __future__ import annotations

import math

from ..plan import (PURPOSES, SIGMA_M, Plan, PlanLine, PlannedPoint,
                    PlannedSetup, Tie, purpose_group)
from ..units import ft_to_m, m_to_ft, parse_rod

NAVIGATE, ADD_POINT, ADD_LINE, ADD_SETUP = ("navigate", "add_point",
                                            "add_line", "add_setup")
MODES = (NAVIGATE, ADD_POINT, ADD_LINE, ADD_SETUP)
MODE_HINTS = {
    ADD_POINT: "Click on the map to place a shot.",
    ADD_LINE: "Click each vertex; double-click to finish the line.",
    ADD_SETUP: "Click where the laser will stand.",
    NAVIGATE: "Drag a numbered marker to move it.",
}

METHODS = ["planned", "rtk", "taped", "station_offset"]

# The editable columns of the readings table, in the printed sheet's order.
FIELDS = ("purpose", "line", "setup", "rod", "fix", "method", "note")

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


def detail_text(plan: Plan, site, number: int) -> str:
    """The one-line description above the position editor."""
    point = _point(plan, number)
    e, n, method, sigma, problem = best_position(plan, number)
    le, ln = site.to_local(e, n)
    measured = " (measured)" if point.observed_e is not None else ""
    text = (f"Point {point.number}: {method}{measured}, about "
            f"{m_to_ft(sigma):.1f} ft horizontally "
            f"({m_to_ft(le):.1f}, {m_to_ft(ln):.1f} ft from origin)")
    if problem:
        text += f". Could not use its {point.method} position: {problem}"
    return text


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
    the RUN - the sheet is ordered by number, the line by shape.
    """
    _point(plan, number)
    line = next((ln for ln in plan.lines if number in ln.numbers), None)
    if line is None:
        raise PlanEditError(
            "Insert vertex",
            "Select a break line vertex. Loose shots are not part of a line.")
    index = line.numbers.index(number)
    if index + 1 >= len(line.numbers):
        raise PlanEditError(
            "Insert vertex",
            "That is the last vertex; there is nothing to insert before.")
    a = plan.by_number(line.numbers[index])
    b = plan.by_number(line.numbers[index + 1])
    new = plan.add_point((a.planned_e + b.planned_e) / 2,
                         (a.planned_n + b.planned_n) / 2, purpose="breakline")
    line.numbers.insert(index + 1, new.number)
    return new


# --- what the views draw -----------------------------------------------------------

def plan_payload(plan: Plan, site) -> dict:
    """Everything the table and the map need, in local metres."""
    points, leaders = [], []
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
            "locked": locked,
            "position_method": method,
            "x": x, "y": y, "px": px, "py": py,
            "tooltip": (f"#{p.number} {p.purpose} — {method}"
                        + ("; position measured, drag disabled" if locked
                           else "; planned position, drag to move")),
            "detail": detail_text(plan, site, p.number),
            "observed": observed,
            "ties": [{"ref": t.ref_number, "ft": t.distance_ft}
                     for t in p.ties],
            "ref_line": p.ref_line,
            "station_ft": m_to_ft(p.station_m or 0.0),
            "offset_ft": m_to_ft(p.offset_m or 0.0),
        })

    drawn = {pt["number"]: (pt["x"], pt["y"]) for pt in points}
    lines = []
    for ln in plan.lines:
        xy = [list(drawn[num]) for num in ln.numbers if num in drawn]
        if ln.closed and len(xy) > 2:
            xy.append(xy[0])
        lines.append({"line_id": ln.line_id, "numbers": list(ln.numbers),
                      "closed": ln.closed, "xy": xy})

    setups = []
    for s in plan.setups:
        if s.e is None or s.n is None:
            continue
        x, y = site.to_local(s.e, s.n)
        setups.append({"name": s.name, "x": x, "y": y})

    return {
        "points": points,
        "lines": lines,
        "setups": setups,
        "leaders": leaders,
        "coverage": coverage_text(plan),
        "line_ids": [ln.line_id for ln in plan.lines],
        "setup_choices": setup_choices(plan),
        "purposes": list(PURPOSES),
        "methods": METHODS,
        "frames": {k: list(v) for k, v in COORD_FRAMES.items()},
        "hints": MODE_HINTS,
    }
