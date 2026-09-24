"""Drawing and editing a shot plan on the plan view.

Division of labour, chosen deliberately: the MAP edits position, the TABLE
edits everything else. Dragging a numbered marker is the one thing a map does
better than a grid; deleting a point or typing a rod reading are things a grid
does better than a map. Clicking a marker selects it in both, because finding
row 47 of a table by eye when you can see the shot on the aerial is silly.

A marker is drawn at the point's BEST KNOWN position, not at where it was
planned. Once a shot has been measured, the click that placed it is history:
the measurement is what the point is. So a measured marker jumps to its real
position, changes to a square, and stops being draggable - dragging it would
edit the planned position, which is no longer what you are looking at, and the
marker would appear not to move. A faint leader is drawn back to the original
click, because the distance between the two is worth seeing: it is the error in
the plan, and where the plan was clicked off an aerial, it is also a direct
reading of how far that aerial is out.

Positions are stored projected but drawn in local metres, because that is what
the plan view uses. The conversion lives here so nothing downstream has to
think about it.
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QObject, Qt, Signal

from ..plan import purpose_group

NAVIGATE, ADD_POINT, ADD_LINE, ADD_SETUP = "navigate", "add_point", "add_line", "add_setup"

# Coloured by purpose GROUP: terrain, built feature, or control. Individual
# purposes are far too numerous to distinguish by colour legibly.
GROUP_PEN = {"terrain": (192, 57, 43), "feature": (31, 95, 168),
             "control": (43, 122, 61)}
SELECTED = (255, 190, 0)
SETUP_COLOUR = (230, 126, 34)
LEADER = (110, 110, 110)

# Below this the leader is shorter than the marker and just makes a smudge.
MIN_LEADER_M = 0.15

# How close a click has to land, in pixels, to count as hitting a marker.
HIT_PX = 12


class PlanTarget(pg.TargetItem):
    """A numbered marker that reports being clicked.

    TargetItem has no clicked signal of its own, and its `mouseClickEvent`
    only handles the right-click that aborts a drag. Overriding it is the
    reliable way to catch a plain left click - including on a marker that is
    locked, where there is no drag to piggyback on.

    The event is only swallowed when the overlay says selection is live.
    While a tool is placing points or drawing a line, a click that happens to
    land on an existing marker still has to reach the scene, or you could not
    put a shot next to one you already have.
    """

    sigClicked = Signal(object)

    def __init__(self, number: int, **kwargs):
        super().__init__(**kwargs)
        self.number = number
        self.selectable = True

    def mouseClickEvent(self, ev):
        if (self.selectable and ev.button() == Qt.LeftButton
                and not self.moving):
            ev.accept()
            self.sigClicked.emit(self.number)
            return
        super().mouseClickEvent(ev)


class PlanOverlay(QObject):
    """Owns the plan's graphics items and the mouse modes that edit them."""

    changed = Signal()                 # geometry changed; redraw and re-solve
    selected = Signal(object)          # point number, or None
    modeChanged = Signal(str)

    def __init__(self, plot: pg.PlotWidget, state, parent=None):
        super().__init__(parent)
        self.plot = plot
        self.state = state
        self.mode = NAVIGATE
        self.selection: int | None = None

        self._targets: dict[int, PlanTarget] = {}
        self._curves: dict[str, pg.PlotCurveItem] = {}
        self._setups: list = []
        self._pending: list[tuple[float, float]] = []
        self._preview = pg.PlotCurveItem(
            pen=pg.mkPen(SELECTED, width=2.0, style=Qt.DashLine))
        self._preview.setZValue(20)
        self.plot.addItem(self._preview)

        # Every planned-to-measured leader in one item, separated by NaN.
        # One item per point would be dozens of graphics objects redrawn on
        # each refresh for a decoration.
        self._leaders = pg.PlotCurveItem(
            pen=pg.mkPen(LEADER, width=1.0, style=Qt.DotLine), connect="finite")
        self._leaders.setZValue(15)
        self.plot.addItem(self._leaders)

        self.plot.scene().sigMouseClicked.connect(self._on_click)

    # --- modes -----------------------------------------------------------

    def set_mode(self, mode: str) -> None:
        if mode == self.mode:
            return
        if self.mode == ADD_LINE:
            self._finish_line()
        self.mode = mode
        # Dragging a marker while trying to click new ones in is maddening, so
        # markers are only movable when nothing else is being placed - and a
        # measured point is never movable, whatever the mode.
        # TargetItem exposes `movable` as a plain attribute; there is no
        # setMovable() to call.
        for number, t in self._targets.items():
            t.movable = mode == NAVIGATE and not self.is_locked(number)
            t.selectable = mode == NAVIGATE
        self.plot.setCursor(Qt.CrossCursor if mode != NAVIGATE
                            else Qt.ArrowCursor)
        self.modeChanged.emit(mode)

    # --- coordinates -----------------------------------------------------

    def _to_local(self, e, n):
        return self.state.site.to_local(e, n)

    def _to_projected(self, le, ln):
        return self.state.site.to_projected(le, ln)

    # --- what a marker is showing ----------------------------------------

    def is_locked(self, number: int) -> bool:
        """True when the marker sits on a measurement rather than on a click.

        Anything `resolve` answers with other than "planned" was derived from
        field data - an RTK position, a taped trilateration, a station and
        offset. Dragging any of those is meaningless: it would edit the planned
        position, which is not what is drawn, so the marker would spring back.
        """
        point = self.state.plan.by_number(number)
        if point is None:
            return False
        return self.state.plan.resolve(number)[2] != "planned"

    # --- drawing ---------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild every item from the plan."""
        plan = self.state.plan
        wanted = {p.number for p in plan.points}

        for number in list(self._targets):
            if number not in wanted:
                self.plot.removeItem(self._targets.pop(number))

        leader_x: list[float] = []
        leader_y: list[float] = []

        for p in plan.points:
            # The BEST position, not the planned one. For an unmeasured point
            # these are the same; for a measured one this is the whole point.
            e, n, method, _ = plan.resolve(p.number)
            x, y = self._to_local(e, n)
            locked = method != "planned"

            target = self._targets.get(p.number)
            if target is None:
                target = PlanTarget(
                    p.number, pos=(x, y), size=11, symbol="o", movable=False,
                    label=str(p.number),
                    labelOpts={"offset": (10, -10), "color": "k"})
                target.setZValue(25)
                target.sigPositionChangeFinished.connect(
                    lambda t=target, num=p.number: self._on_moved(num, t))
                target.sigPositionChanged.connect(lambda *_: self._redraw_lines())
                target.sigClicked.connect(self._on_target_clicked)
                self.plot.addItem(target)
                self._targets[p.number] = target
            else:
                target.blockSignals(True)
                target.setPos(x, y)
                target.blockSignals(False)

            target.movable = self.mode == NAVIGATE and not locked
            target.selectable = self.mode == NAVIGATE
            # Square for measured, circle for planned. Colour is already
            # carrying the purpose group, so shape is what is left to say
            # whether this position was surveyed or guessed.
            target.setSymbol("s" if locked else "o")
            target.setToolTip(
                f"#{p.number} {p.purpose} — {method}"
                + ("; position measured, drag disabled" if locked
                   else "; planned position, drag to move"))
            self._style(p.number, target, p.purpose, locked)

            if locked:
                px, py = self._to_local(p.planned_e, p.planned_n)
                if np.hypot(px - x, py - y) >= MIN_LEADER_M:
                    leader_x += [px, x, np.nan]
                    leader_y += [py, y, np.nan]

        self._leaders.setData(np.asarray(leader_x, dtype=float),
                              np.asarray(leader_y, dtype=float))
        self._redraw_lines()
        self._redraw_setups()

    def _style(self, number: int, target: "PlanTarget", purpose: str,
               locked: bool = False) -> None:
        chosen = number == self.selection
        colour = (SELECTED if chosen else
                  GROUP_PEN.get(purpose_group(purpose), GROUP_PEN["terrain"]))
        target.setPen(pg.mkPen(colour, width=3 if chosen else 2))
        # A measured marker is filled solidly: it is a fact, not a proposal.
        alpha = (150 if chosen else 110) if locked else (90 if chosen else 40)
        target.setBrush(pg.mkBrush(*colour, alpha))

    def _redraw_lines(self) -> None:
        plan = self.state.plan
        for line_id in list(self._curves):
            if plan.line(line_id) is None:
                self.plot.removeItem(self._curves.pop(line_id))

        for line in plan.lines:
            xs, ys = [], []
            for num in line.numbers:
                target = self._targets.get(num)
                point = plan.by_number(num)
                if target is not None:
                    pos = target.pos()
                    xs.append(pos.x()); ys.append(pos.y())
                elif point is not None:
                    x, y = self._to_local(point.planned_e, point.planned_n)
                    xs.append(x); ys.append(y)
            if line.closed and len(xs) > 2:
                xs.append(xs[0]); ys.append(ys[0])

            curve = self._curves.get(line.line_id)
            if curve is None:
                curve = pg.PlotCurveItem(
                    pen=pg.mkPen(GROUP_PEN["terrain"], width=2.2,
                                 style=Qt.DashLine))
                curve.setZValue(18)
                self.plot.addItem(curve)
                self._curves[line.line_id] = curve
            curve.setData(np.asarray(xs), np.asarray(ys))

    def _redraw_setups(self) -> None:
        for item in self._setups:
            self.plot.removeItem(item)
        self._setups.clear()
        for setup in self.state.plan.setups:
            if setup.e is None or setup.n is None:
                continue
            x, y = self._to_local(setup.e, setup.n)
            item = pg.ScatterPlotItem(
                [x], [y], symbol="t1", size=17,
                brush=pg.mkBrush(*SETUP_COLOUR, 220),
                pen=pg.mkPen("k", width=1.2))
            item.setZValue(22)
            self.plot.addItem(item)
            self._setups.append(item)

    # --- interaction -----------------------------------------------------

    def _on_moved(self, number: int, target: "PlanTarget") -> None:
        point = self.state.plan.by_number(number)
        if point is None:
            return
        # Belt and braces: `movable` is already False for a measured point,
        # but a stray programmatic setPos must never quietly rewrite the plan.
        if self.is_locked(number):
            self.refresh()
            return
        pos = target.pos()
        point.planned_e, point.planned_n = self._to_projected(pos.x(), pos.y())
        self.select(number)
        self.changed.emit()

    def _on_target_clicked(self, number: int) -> None:
        """A marker was clicked: make it the selection, table included."""
        if self.mode != NAVIGATE:
            return
        self.select(number)

    def _on_click(self, event) -> None:
        if self.mode == NAVIGATE or event.button() != Qt.LeftButton:
            return
        vb = self.plot.getPlotItem().vb
        pos = vb.mapSceneToView(event.scenePos())
        e, n = self._to_projected(pos.x(), pos.y())

        if self.mode == ADD_POINT:
            point = self.state.plan.add_point(e, n, purpose="spot")
            event.accept()
            self.select(point.number)
            self.changed.emit()
            return

        if self.mode == ADD_SETUP:
            from ..plan import PlannedSetup

            name = chr(ord("A") + len(self.state.plan.setups))
            self.state.plan.setups.append(PlannedSetup(name=name, e=e, n=n))
            event.accept()
            self.set_mode(NAVIGATE)
            self.changed.emit()
            return

        if self.mode == ADD_LINE:
            event.accept()
            if event.double():
                self._finish_line()
                return
            self._pending.append((pos.x(), pos.y()))
            self._preview.setData(np.array([p[0] for p in self._pending]),
                                  np.array([p[1] for p in self._pending]))

    def finish_line(self) -> None:
        """Commit the line being drawn, if any."""
        self._finish_line()

    def cancel_line(self) -> None:
        self._pending.clear()
        self._preview.setData([], [])

    def _finish_line(self) -> None:
        pending, self._pending = self._pending, []
        self._preview.setData([], [])
        if len(pending) < 2:
            return                      # a stray click is not a line

        plan = self.state.plan
        base = "line"
        index = len(plan.lines) + 1
        while plan.line(f"{base}-{index}") is not None:
            index += 1
        plan.add_line(f"{base}-{index}",
                      [self._to_projected(x, y) for x, y in pending])
        self.changed.emit()

    def select(self, number: int | None) -> None:
        # Re-emitting an unchanged selection is what turns a two-way binding
        # between the table and the map into infinite recursion.
        if number == self.selection:
            return
        previous, self.selection = self.selection, number
        plan = self.state.plan
        for num in (previous, number):
            target = self._targets.get(num) if num is not None else None
            point = plan.by_number(num) if num is not None else None
            if target is not None and point is not None:
                self._style(num, target, point.purpose, self.is_locked(num))
        self.selected.emit(number)

    def clear(self) -> None:
        for target in self._targets.values():
            self.plot.removeItem(target)
        for curve in self._curves.values():
            self.plot.removeItem(curve)
        for item in self._setups:
            self.plot.removeItem(item)
        self._targets.clear()
        self._curves.clear()
        self._setups.clear()
        self._leaders.setData([], [])
        self.cancel_line()
