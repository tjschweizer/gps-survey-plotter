"""Shot planning: decide in the office, record on paper, type back in.

The workflow this supports:

  1. Click where shots should go, over the aerial. Each gets a NUMBER.
  2. Print a sheet: a plan showing the numbers, and a table of blank rows.
  3. In the field, find each number and write down the rod reading. Shoot the
     GNSS position too where the sky allows; where it does not, the planned
     position stands, or a tape ties it to points that were shot.
  4. Type the readings back in against the numbers.

The number is the identity throughout. Nothing is matched by position, because
position is the least reliable thing about a point under canopy - which is
exactly where this workflow earns its keep.

Why an approximate position is acceptable
-----------------------------------------
Measured over this lot, median surface slope is 6.1% and p90 is 13%. Horizontal
error therefore costs roughly that fraction in height:

    1 ft out  ->  1.9 cm at median slope,  3.9 cm at p90

Float GNSS carries 27.6 cm of bias plus 29.2 cm of noise. So a laser height at a
position merely clicked on a map beats a float fix at a surveyed position by an
order of magnitude, and you would have to be ~15 ft out before that stopped
being true. Under the west canopy, guessing the position and measuring the
height properly is the better trade.

The laser's own position is NOT control
---------------------------------------
The level network solves `HI(setup) - elevation(point) = rod`. There is no
geometry in it: a rotary laser sweeps a horizontal plane, so the height of that
plane is one unknown wherever the tripod stands. Setup positions are recorded
here for planning only - line of sight, and range, since an imperfectly levelled
laser accumulates error with distance.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np

from .fileio import write_text_atomic
from .units import M_PER_FT, M_PER_IN, ft_to_m, m_to_ft

SUFFIX = ".yardplan"
VERSION = 1

# How each position method is expected to perform horizontally. These are not
# measured - they are honest order-of-magnitude priors, used to weight points
# and to warn when a position is too rough for the local slope.
SIGMA_M = {
    "planned": 0.60,          # a click on 22 cm imagery, generously
    "rtk": 0.03,              # a fixed solution
    "rtk_float": 0.50,
    "taped": 0.15,            # a steel tape over short distances
    "station_offset": 0.20,
}

# What a shot is FOR, grouped by what the answer is used for. The group is not
# decoration: only terrain shots describe the ground surface. A building corner
# is a real, useful, accurately placed point that must never be fed to the
# interpolator as if it were lawn.
PURPOSE_GROUPS: dict[str, tuple[str, ...]] = {
    "terrain": ("spot", "breakline", "swale bottom", "crown",
                "top of slope", "toe of slope", "drainage outlet"),
    "feature": ("building corner", "foundation", "driveway edge",
                "sidewalk edge", "curb flowline", "wall", "step",
                "fence", "tree", "utility"),
    "control": ("plat reference", "monument", "benchmark", "check"),
    # Not shots at all: marks to walk or mow along, so the next outing
    # shares ground with the last. Never read with the rod.
    "guide": ("tie transect",),
}

PURPOSES: tuple[str, ...] = tuple(
    p for group in PURPOSE_GROUPS.values() for p in group)

TERRAIN_PURPOSES = frozenset(PURPOSE_GROUPS["terrain"])
CONTROL_PURPOSES = frozenset(PURPOSE_GROUPS["control"])
GUIDE_PURPOSES = frozenset(PURPOSE_GROUPS["guide"])

DEFAULT_LINE_PURPOSE = "breakline"

# A SW Maps record named after a plan shot fills it, but never overwrites it.
# Beyond these it disagrees, and the disagreement is reported instead.
FILL_POSITION_TOLERANCE_M = 0.10
FILL_ROD_TOLERANCE_IN = 1e-4


def purpose_group(purpose: str) -> str:
    for name, members in PURPOSE_GROUPS.items():
        if purpose in members:
            return name
    return "feature"


def is_terrain(purpose: str) -> bool:
    """Whether a shot describes the ground surface itself."""
    return purpose in TERRAIN_PURPOSES


def is_guide(purpose: str) -> bool:
    """Whether a point only guides the next outing (a tie transect vertex)."""
    return purpose in GUIDE_PURPOSES


@dataclass
class Tie:
    """A taped distance from a reference point, for trilateration."""

    ref_number: int
    distance_m: float

    @property
    def distance_ft(self) -> float:
        return m_to_ft(self.distance_m)


@dataclass
class PlannedSetup:
    """Where the laser is planned to stand. Planning aid, not control."""

    name: str
    e: float | None = None
    n: float | None = None
    note: str = ""
    max_range_m: float = 60.0


@dataclass
class PlanLine:
    """An ordered run of planned points - a break line, or a transect.

    A break line is not a separate kind of object from a shot; it is a sequence
    of shots that happen to lie along a feature. Keeping it that way means one
    numbering scheme, one field sheet, and one import path.
    """

    line_id: str
    kind: str = "breakline"       # breakline | transect | boundary
    numbers: list[int] = field(default_factory=list)
    note: str = ""
    closed: bool = False


@dataclass
class PlannedPoint:
    """One numbered observation: where to go, and what came back."""

    number: int
    planned_e: float
    planned_n: float
    purpose: str = "spot"
    setup: str = ""
    note: str = ""

    # --- filled in from the field ---
    rod_in: float | None = None
    observed_e: float | None = None
    observed_n: float | None = None
    fix: int | None = None                 # 4 fixed, 5 float
    method: str = "planned"                # see SIGMA_M
    ties: list[Tie] = field(default_factory=list)
    ref_line: str = ""                     # for station_offset
    station_m: float | None = None
    offset_m: float | None = None
    observed_note: str = ""

    @property
    def has_reading(self) -> bool:
        return self.rod_in is not None

    def sigma_m(self) -> float:
        return SIGMA_M.get(self.method, SIGMA_M["planned"])


@dataclass
class FillReport:
    """What SW Maps records named "P12" filled in, and where they disagreed."""

    filled: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.filled)

    def describe(self) -> str:
        lines = []
        if self.filled:
            lines.append("Filled from SW Maps records named after plan shots:")
            lines += [f"  {f}" for f in self.filled]
        if self.conflicts:
            if lines:
                lines.append("")
            lines.append("SW Maps records that disagree with the plan (the "
                         "plan was kept; check which is right):")
            lines += [f"  {c}" for c in self.conflicts]
        return "\n".join(lines)


@dataclass
class Plan:
    """A set of numbered observations, their lines, and the laser setups."""

    points: list[PlannedPoint] = field(default_factory=list)
    lines: list[PlanLine] = field(default_factory=list)
    setups: list[PlannedSetup] = field(default_factory=list)
    epsg: int = 0
    name: str = "shot plan"
    version: int = VERSION

    # --- lookup ----------------------------------------------------------

    def by_number(self, number: int) -> PlannedPoint | None:
        for p in self.points:
            if p.number == number:
                return p
        return None

    def next_number(self) -> int:
        return (max((p.number for p in self.points), default=0)) + 1

    def line(self, line_id: str) -> PlanLine | None:
        for ln in self.lines:
            if ln.line_id == line_id:
                return ln
        return None

    # --- editing ---------------------------------------------------------

    def add_point(self, e: float, n: float, purpose: str = "spot",
                  setup: str = "", note: str = "") -> PlannedPoint:
        if purpose not in PURPOSES:
            raise ValueError(f"purpose must be one of {PURPOSES}, got {purpose!r}")
        p = PlannedPoint(number=self.next_number(), planned_e=e, planned_n=n,
                         purpose=purpose, setup=setup, note=note)
        self.points.append(p)
        return p

    def add_line(self, line_id: str, vertices, kind: str = "breakline",
                 note: str = "", closed: bool = False,
                 purpose: str | None = None) -> PlanLine:
        """Create a line from a sequence of (e, n), numbering each vertex.

        Vertices inherit the line's purpose, so a swale line produces swale
        shots and a foundation line produces foundation shots. That matters
        downstream: only terrain purposes reach the ground surface.
        """
        if self.line(line_id) is not None:
            raise ValueError(f"line {line_id!r} already exists")
        purpose = purpose or (kind if kind in PURPOSES else DEFAULT_LINE_PURPOSE)
        ln = PlanLine(line_id=line_id, kind=kind, note=note, closed=closed)
        for e, n in vertices:
            p = self.add_point(e, n, purpose=purpose)
            ln.numbers.append(p.number)
        self.lines.append(ln)
        return ln

    def set_purpose(self, number: int, purpose: str) -> None:
        if purpose not in PURPOSES:
            raise ValueError(f"unknown purpose {purpose!r}")
        point = self.by_number(number)
        if point is None:
            raise KeyError(f"no planned point numbered {number}")
        point.purpose = purpose

    def assign_line(self, number: int, line_id: str | None) -> None:
        """Move a point onto a line, or off every line when line_id is falsy."""
        for ln in self.lines:
            if number in ln.numbers and ln.line_id != line_id:
                ln.numbers.remove(number)
        if not line_id:
            return
        line = self.line(line_id)
        if line is None:
            raise KeyError(f"no line named {line_id!r}")
        if number not in line.numbers:
            line.numbers.append(number)

    def remove_point(self, number: int) -> None:
        self.points = [p for p in self.points if p.number != number]
        for ln in self.lines:
            if number in ln.numbers:
                ln.numbers.remove(number)
        # Numbers are never reused or renumbered: a printed sheet in a pocket
        # is the source of truth, and silently shifting numbers under it would
        # mis-file every reading taken after the edit.

    # --- position resolution ---------------------------------------------

    def resolve(self, number: int) -> tuple[float, float, str, float]:
        """Best available position for a point: (e, n, method, sigma_m)."""
        p = self.by_number(number)
        if p is None:
            raise KeyError(f"no planned point numbered {number}")

        if p.method == "rtk" and p.observed_e is not None:
            return p.observed_e, p.observed_n, "rtk", p.sigma_m()

        if p.method == "taped" and len(p.ties) >= 2:
            e, n, _ = self.trilaterate(p)
            return e, n, "taped", p.sigma_m()

        if p.method == "station_offset" and p.ref_line:
            e, n = self.station_offset(p)
            return e, n, "station_offset", p.sigma_m()

        # An observed position with no explicit method still beats the plan.
        if p.observed_e is not None and p.observed_n is not None:
            method = "rtk" if p.fix == 4 else "rtk_float"
            return p.observed_e, p.observed_n, method, SIGMA_M[method]

        return p.planned_e, p.planned_n, "planned", SIGMA_M["planned"]

    def reference_position(self, number: int) -> tuple[float, float]:
        """Position of a point used as a tape reference.

        Deliberately does not recurse into other taped points: chaining tape
        ties compounds error invisibly, and a reference should be something you
        actually shot.
        """
        p = self.by_number(number)
        if p is None:
            raise KeyError(f"reference point {number} does not exist")
        if p.observed_e is not None and p.observed_n is not None:
            return p.observed_e, p.observed_n
        return p.planned_e, p.planned_n

    def trilaterate(self, point: PlannedPoint, *, iterations: int = 12,
                    tol: float = 1e-4) -> tuple[float, float, dict]:
        """Solve a position from taped distances to numbered references.

        Gauss-Newton, started from the planned position. Starting there is what
        resolves the two-solution ambiguity of a two-distance fix without any
        extra bookkeeping: the planned click already says which side you are on.
        """
        if len(point.ties) < 2:
            raise ValueError(
                f"point {point.number} needs at least two taped distances; "
                f"has {len(point.ties)}")

        refs = np.array([self.reference_position(t.ref_number)
                         for t in point.ties], dtype=float)
        obs = np.array([t.distance_m for t in point.ties], dtype=float)
        x = np.array([point.planned_e, point.planned_n], dtype=float)

        for _ in range(iterations):
            delta = x - refs
            calc = np.hypot(delta[:, 0], delta[:, 1])
            calc = np.where(calc < 1e-9, 1e-9, calc)
            jac = delta / calc[:, None]
            step, *_ = np.linalg.lstsq(jac, obs - calc, rcond=None)
            x = x + step
            if np.linalg.norm(step) < tol:
                break

        delta = x - refs
        residuals = obs - np.hypot(delta[:, 0], delta[:, 1])
        dof = len(obs) - 2
        stats = {
            "n_ties": len(obs),
            "dof": dof,
            "residuals_m": residuals,
            "max_residual_m": float(np.abs(residuals).max()),
            "rms_m": float(np.sqrt((residuals ** 2).mean())),
        }
        return float(x[0]), float(x[1]), stats

    def station_offset(self, point: PlannedPoint) -> tuple[float, float]:
        """Position from a distance along a reference line plus a perpendicular.

        Offset sign follows the line's own direction: positive is to the LEFT
        of travel from the line's first vertex toward its last. Stated because
        a sign convention that is only implied gets guessed wrong in the field.
        """
        line = self.line(point.ref_line)
        if line is None or len(line.numbers) < 2:
            raise ValueError(
                f"point {point.number} references line {point.ref_line!r}, "
                "which needs at least two vertices")
        if point.station_m is None or point.offset_m is None:
            raise ValueError(
                f"point {point.number} needs both a station and an offset")

        a = np.array(self.reference_position(line.numbers[0]))
        b = np.array(self.reference_position(line.numbers[-1]))
        span = b - a
        length = float(np.hypot(*span))
        if length < 1e-9:
            raise ValueError(f"line {point.ref_line!r} has zero length")

        along = span / length
        left = np.array([-along[1], along[0]])
        pos = a + along * point.station_m + left * point.offset_m
        return float(pos[0]), float(pos[1])

    # --- filling from the field app ----------------------------------------

    def fill_from_records(self, records) -> FillReport:
        """Fill plan shots from SW Maps records recorded as "P12".

        `records` are dicts with `number`, `e`, `n`, `fix`, `rod_in`, `setup`
        and a `label` to report them by, earliest first. A record:

          * gives its position and fix only if it is RTK FIXED and the shot
            has no measured position yet - a float fix is worse than the
            planned click, which is the field sheet's own rule;
          * gives its rod reading, and its setup, only to an empty cell.

        Nothing is ever overwritten. A fixed position more than 10 cm from
        the measured one, or a different reading on the same setup, is
        reported instead: one of them is wrong, and only a person can say
        which. Until now every position had to be retyped from the sheet.
        """
        report = FillReport()
        for r in records:
            p = self.by_number(int(r["number"]))
            if p is None:
                continue
            name, label = f"P{p.number}", r.get("label", "a SW Maps record")
            e, n = r.get("e"), r.get("n")
            fixed = (r.get("fix") == 4 and e is not None and n is not None
                     and math.isfinite(e) and math.isfinite(n))
            if fixed:
                if p.observed_e is None or p.observed_n is None:
                    p.observed_e, p.observed_n = float(e), float(n)
                    p.fix, p.method = 4, "rtk"
                    report.filled.append(f"{name}: RTK fixed position from {label}")
                else:
                    away = math.hypot(e - p.observed_e, n - p.observed_n)
                    if away > FILL_POSITION_TOLERANCE_M:
                        report.conflicts.append(
                            f"{name}: {label} is {away:.2f} m from the "
                            "measured position")

            rod = r.get("rod_in")
            if rod is None or not math.isfinite(rod):
                continue
            setup = str(r.get("setup") or "")
            if p.rod_in is None:
                p.rod_in = float(rod)
                if not p.setup and setup:
                    p.setup = setup
                report.filled.append(
                    f"{name}: rod {rod:g} in"
                    + (f", setup {setup}" if setup else "") + f" from {label}")
            elif abs(p.rod_in - rod) <= FILL_ROD_TOLERANCE_IN:
                if not p.setup and setup:
                    p.setup = setup
                    report.filled.append(f"{name}: setup {setup} from {label}")
            elif not p.setup or not setup or p.setup == setup:
                report.conflicts.append(
                    f"{name}: {label} reads {rod:g} in"
                    + (f" on setup {setup}" if setup else "")
                    + f"; the plan has {p.rod_in:g} in")
        return report

    # --- handing off to the rest of the pipeline -------------------------

    def to_frame(self):
        """Resolved observations as a DataFrame, ready to become a PointSet."""
        import pandas as pd

        rows = []
        for p in self.points:
            if not p.has_reading or is_guide(p.purpose):
                continue
            e, n, method, sigma = self.resolve(p.number)
            rows.append({
                "point_id": p.number, "station": f"P{p.number}",
                "e": e, "n": n,
                "rod_in": p.rod_in, "setup": p.setup or "0",
                # `kind` drives the surfacing filters. Only terrain shots are
                # lawn; a building corner is an accurate point that would be a
                # lie if interpolated as ground.
                "kind": "lawn" if is_terrain(p.purpose) else p.purpose,
                "purpose": p.purpose,
                "purpose_group": purpose_group(p.purpose),
                "terrain": is_terrain(p.purpose),
                "position_method": method, "position_sigma_m": sigma,
                "fix": p.fix, "notes": p.observed_note or p.note,
            })
        return pd.DataFrame(rows)

    def coverage(self) -> dict:
        shots = [p for p in self.points if not is_guide(p.purpose)]
        done = [p for p in shots if p.has_reading]
        return {
            "planned": len(shots),
            "observed": len(done),
            "outstanding": len(shots) - len(done),
            "by_method": {m: sum(1 for p in done if p.method == m)
                          for m in sorted({p.method for p in done})},
        }

    # --- persistence -----------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "version": self.version, "name": self.name, "epsg": self.epsg,
            "points": [asdict(p) for p in self.points],
            "lines": [asdict(ln) for ln in self.lines],
            "setups": [asdict(s) for s in self.setups],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        version = int(d.get("version", 0))
        if version > VERSION:
            raise ValueError(
                f"plan format {version} is newer than this build understands "
                f"({VERSION})")
        points = []
        for raw in d.get("points", []):
            raw = dict(raw)
            raw["ties"] = [Tie(**t) for t in raw.get("ties", [])]
            points.append(PlannedPoint(**raw))
        return cls(
            points=points,
            lines=[PlanLine(**ln) for ln in d.get("lines", [])],
            setups=[PlannedSetup(**s) for s in d.get("setups", [])],
            epsg=int(d.get("epsg", 0)), name=d.get("name", "shot plan"),
            version=version)

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        if path.suffix != SUFFIX:
            path = path.with_suffix(SUFFIX)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, json.dumps(self.to_dict(), indent=2))
        return path

    @classmethod
    def load(cls, path: str | Path) -> "Plan":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
