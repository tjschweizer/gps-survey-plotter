"""Shot plan panel: the table you type the paper sheet into.

Columns mirror the printed sheet, in the same order, so transcribing is a
straight read across rather than a hunt. The rod reading is the only field
that must be filled in; everything else has a working default.

Position method gets its own column because it decides how much the point can
be trusted horizontally, and the two methods that need extra numbers - taped
distances and station/offset - reveal an editor underneath rather than trying
to cram them into cells.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox,
                               QComboBox, QDoubleSpinBox, QGridLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QHeaderView, QPushButton, QStyledItemDelegate,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from ..plan import PURPOSE_GROUPS, PURPOSES, SIGMA_M, Tie, purpose_group
from ..units import ft_to_m, m_to_ft

COLUMNS = ["#", "Purpose", "Line", "Setup", "Rod (in)", "Fixed", "Method", "Notes"]
COL_NUM, COL_PURPOSE, COL_LINE, COL_SETUP, COL_ROD, COL_FIX, COL_METHOD, COL_NOTE = range(8)

METHODS = ["planned", "rtk", "taped", "station_offset"]
DONE_BG = QColor(226, 243, 228)

# Frames a measured position can be typed in. A receiver read in the field
# gives lat/lon or UTM; a value read off the plan or the field sheet is in
# local feet. Converting by hand is exactly where a sign or a digit gets lost.
COORD_FRAMES = {
    "local ft": ("east (ft)", "north (ft)"),
    "UTM m": ("easting (m)", "northing (m)"),
    "lat/lon": ("longitude", "latitude"),
}

GROUP_TINT = {
    "terrain": QColor(0, 0, 0, 0),
    "feature": QColor(232, 240, 252),
    "control": QColor(252, 243, 226),
}


class ChoiceDelegate(QStyledItemDelegate):
    """A combo box editor for a column with a fixed set of values.

    Free text is the wrong control for a purpose: the value drives whether the
    shot reaches the ground surface, so a typo would silently reclassify a
    building corner as lawn.
    """

    def __init__(self, choices, parent=None, editable=False):
        super().__init__(parent)
        self._choices = list(choices)
        self._editable = editable

    def set_choices(self, choices) -> None:
        self._choices = list(choices)

    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        combo.setEditable(self._editable)
        combo.addItems(self._choices)
        return combo

    def setEditorData(self, editor, index):
        value = index.data(Qt.DisplayRole) or ""
        pos = editor.findText(value)
        if pos >= 0:
            editor.setCurrentIndex(pos)
        elif self._editable:
            editor.setEditText(value)

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText(), Qt.EditRole)


class PlanPanel(QWidget):
    """Table of planned shots plus the position-method editor."""

    changed = Signal()
    selected = Signal(object)
    modeRequested = Signal(str)

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self._loading = False
        self._active = "navigate"

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(6)

        tools = QGridLayout()
        tools.setHorizontalSpacing(4)
        # Exclusive, so exactly one tool is active and the active one is
        # obvious. Clicking the tool you are already in drops back to Navigate,
        # which is the only way to "put the tool down" without hunting for it.
        self.tool_group = QButtonGroup(self)
        self.tool_group.setExclusive(True)
        for label, mode, tip in (
                ("+ Point", "add_point", "Click on the map to place a shot"),
                ("+ Line", "add_line", "Click vertices; double-click to finish"),
                ("+ Laser", "add_setup", "Where the laser will stand"),
                ("Navigate", "navigate",
                 "Pan, zoom and drag markers. Esc returns here.")):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, m=mode: self._tool_clicked(m))
            self.tool_group.addButton(b)
            index = len(self.tool_group.buttons()) - 1
            tools.addWidget(b, index // 2, index % 2)
            setattr(self, f"btn_{mode}", b)
        self.btn_navigate.setChecked(True)
        root.addLayout(tools)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        # Extended, so a run of vertices or a whole mis-placed line can go in
        # one action. Ctrl-click adds, shift-click takes a range.
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        # Without SelectedClicked, a click only selects the row and a second
        # click does nothing, so cells look permanently read-only. This is what
        # makes a single click on an already-selected cell start editing.
        self.table.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked
            | QAbstractItemView.EditKeyPressed | QAbstractItemView.AnyKeyPressed)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._on_selection)
        # Del on the table deletes the selected rows. Scoped to the table
        # rather than the window so it cannot fire while the map has focus,
        # where Del means nothing and a silent deletion would be alarming.
        delete = QShortcut(QKeySequence.Delete, self.table)
        delete.setContext(Qt.WidgetShortcut)
        delete.activated.connect(self._delete_selected)
        font = QFont("Consolas")
        font.setPointSize(9)
        self.table.setFont(font)
        self.table.setMinimumWidth(160)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)

        self.purpose_delegate = ChoiceDelegate(PURPOSES, self.table)
        self.table.setItemDelegateForColumn(COL_PURPOSE, self.purpose_delegate)
        self.line_delegate = ChoiceDelegate([""], self.table)
        self.table.setItemDelegateForColumn(COL_LINE, self.line_delegate)
        self.setup_delegate = ChoiceDelegate([""], self.table, editable=True)
        self.table.setItemDelegateForColumn(COL_SETUP, self.setup_delegate)
        self.method_delegate = ChoiceDelegate(METHODS, self.table)
        self.table.setItemDelegateForColumn(COL_METHOD, self.method_delegate)

        # Sizing columns to their contents leaves every column as narrow as
        # its emptiest cell and dumps the leftover width to the right of the
        # table. Give each a workable width and let Notes absorb the slack.
        header = self.table.horizontalHeader()
        header.setMinimumSectionSize(34)
        for col, width, mode in (
                (COL_NUM, 38, QHeaderView.Fixed),
                (COL_PURPOSE, 112, QHeaderView.Interactive),
                (COL_LINE, 80, QHeaderView.Interactive),
                (COL_SETUP, 52, QHeaderView.Interactive),
                (COL_ROD, 68, QHeaderView.Interactive),
                (COL_FIX, 46, QHeaderView.Interactive),
                (COL_METHOD, 92, QHeaderView.Interactive),
                (COL_NOTE, 110, QHeaderView.Stretch)):
            header.setSectionResizeMode(col, mode)
            self.table.setColumnWidth(col, width)
        self.table.verticalHeader().setDefaultSectionSize(22)
        root.addWidget(self.table, 1)

        self.detail = QGroupBox("Position")
        self._build_detail()
        root.addWidget(self.detail)

        row = QGridLayout()
        self.coverage = QLabel("")
        self.coverage.setStyleSheet("font-weight: bold;")
        self.coverage.setWordWrap(True)
        row.addWidget(self.coverage, 0, 0, 1, 3)
        self.btn_insert = QPushButton("Insert vertex")
        self.btn_insert.setToolTip(
            "Add a vertex after the selected one, halfway to the next")
        self.btn_insert.clicked.connect(self._insert_vertex)
        row.addWidget(self.btn_insert, 1, 0)
        self.btn_delete = QPushButton("Delete")
        self.btn_delete.clicked.connect(self._delete_selected)
        row.addWidget(self.btn_delete, 1, 1)
        self.btn_sheet = QPushButton("Field sheet…")
        row.addWidget(self.btn_sheet, 1, 2)
        root.addLayout(row)

    def _tool_clicked(self, mode: str) -> None:
        """Clicking the active tool again drops back to Navigate."""
        button = getattr(self, f"btn_{mode}", None)
        if mode != "navigate" and button is not None and self._active == mode:
            self.modeRequested.emit("navigate")
            return
        self.modeRequested.emit(mode)

    def set_active_tool(self, mode: str) -> None:
        self._active = mode
        button = getattr(self, f"btn_{mode}", None)
        if button is not None:
            button.setChecked(True)

    # --- the position-method editor --------------------------------------

    def _build_detail(self) -> None:
        lay = QVBoxLayout(self.detail)
        lay.setContentsMargins(6, 4, 6, 6)
        lay.setSpacing(4)

        self.detail_hint = QLabel("Select a row.")
        self.detail_hint.setWordWrap(True)
        self.detail_hint.setStyleSheet("color: palette(mid);")
        self.detail_hint.setMinimumWidth(80)
        lay.addWidget(self.detail_hint)

        # Grid rather than one long row. Laid out horizontally these two
        # editors demanded nearly 1000 px, which was most of why the docks
        # could not be resized at all.
        self.tape_row = QWidget()
        tape = QGridLayout(self.tape_row)
        tape.setContentsMargins(0, 0, 0, 0)
        tape.setHorizontalSpacing(4)
        tape.setVerticalSpacing(3)
        self.tie_a = QLineEdit(); self.tie_a.setMaximumWidth(52)
        self.dist_a = QDoubleSpinBox(); self.dist_a.setRange(0, 10000)
        self.dist_a.setDecimals(2); self.dist_a.setSuffix(" ft")
        self.tie_b = QLineEdit(); self.tie_b.setMaximumWidth(52)
        self.dist_b = QDoubleSpinBox(); self.dist_b.setRange(0, 10000)
        self.dist_b.setDecimals(2); self.dist_b.setSuffix(" ft")
        for row, (tie, dist) in enumerate(((self.tie_a, self.dist_a),
                                           (self.tie_b, self.dist_b))):
            tape.addWidget(QLabel("from #"), row, 0)
            tape.addWidget(tie, row, 1)
            tape.addWidget(dist, row, 2)
        apply_tape = QPushButton("Solve")
        apply_tape.clicked.connect(self._apply_tape)
        tape.addWidget(apply_tape, 2, 0, 1, 3)
        tape.setColumnStretch(2, 1)
        lay.addWidget(self.tape_row)

        self.offset_row = QWidget()
        off = QGridLayout(self.offset_row)
        off.setContentsMargins(0, 0, 0, 0)
        off.setHorizontalSpacing(4)
        off.setVerticalSpacing(3)
        self.ref_line = QComboBox(); self.ref_line.setMinimumWidth(80)
        self.station = QDoubleSpinBox(); self.station.setRange(-10000, 10000)
        self.station.setDecimals(2); self.station.setSuffix(" ft")
        self.offset = QDoubleSpinBox(); self.offset.setRange(-10000, 10000)
        self.offset.setDecimals(2); self.offset.setSuffix(" ft")
        for row, (text, widget) in enumerate((("along line", self.ref_line),
                                              ("station", self.station),
                                              ("offset (+ left)", self.offset))):
            off.addWidget(QLabel(text), row, 0)
            off.addWidget(widget, row, 1)
        apply_off = QPushButton("Apply")
        apply_off.clicked.connect(self._apply_offset)
        off.addWidget(apply_off, 3, 0, 1, 2)
        off.setColumnStretch(1, 1)
        lay.addWidget(self.offset_row)

        # Typing in a measured position. The field sheet has a column for the
        # receiver's coordinates, so there has to be somewhere to put them -
        # a receiver read in the field gives lat/lon or UTM, while a value read
        # off the plan is in local feet, so all three frames are accepted
        # rather than forcing a conversion by hand.
        self.rtk_row = QWidget()
        rtk = QGridLayout(self.rtk_row)
        rtk.setContentsMargins(0, 0, 0, 0)
        rtk.setHorizontalSpacing(4)
        rtk.setVerticalSpacing(3)
        rtk.addWidget(QLabel("frame"), 0, 0)
        self.coord_frame = QComboBox()
        self.coord_frame.addItems(list(COORD_FRAMES))
        self.coord_frame.currentTextChanged.connect(self._on_frame_changed)
        rtk.addWidget(self.coord_frame, 0, 1)
        self.coord_x_label = QLabel("east")
        self.coord_y_label = QLabel("north")
        self.coord_x = QLineEdit()
        self.coord_y = QLineEdit()
        for row, (label, field) in enumerate(
                ((self.coord_x_label, self.coord_x),
                 (self.coord_y_label, self.coord_y)), start=1):
            rtk.addWidget(label, row, 0)
            rtk.addWidget(field, row, 1)
        apply_rtk = QPushButton("Set measured position")
        apply_rtk.clicked.connect(self._apply_coords)
        rtk.addWidget(apply_rtk, 3, 0, 1, 2)
        self.clear_rtk = QPushButton("Clear, use planned")
        self.clear_rtk.clicked.connect(self._clear_coords)
        rtk.addWidget(self.clear_rtk, 4, 0, 1, 2)
        rtk.setColumnStretch(1, 1)
        lay.addWidget(self.rtk_row)

        self.detail_result = QLabel("")
        self.detail_result.setWordWrap(True)
        lay.addWidget(self.detail_result)

    # --- table -----------------------------------------------------------

    def refresh(self) -> None:
        plan = self.state.plan
        self._loading = True
        try:
            self.table.setRowCount(len(plan.points))
            for row, p in enumerate(sorted(plan.points, key=lambda q: q.number)):
                line = next((ln.line_id for ln in plan.lines
                             if p.number in ln.numbers), "")
                self._set(row, COL_NUM, str(p.number), editable=False)
                self._set(row, COL_PURPOSE, p.purpose)
                self._set(row, COL_LINE, line)
                self._set(row, COL_SETUP, p.setup)
                self._set(row, COL_ROD, "" if p.rod_in is None else f"{p.rod_in:g}")
                self._set(row, COL_FIX, "yes" if p.fix == 4 else "")
                self._set(row, COL_METHOD, p.method)
                self._set(row, COL_NOTE, p.observed_note or p.note)

                # Shot rows go green; the purpose group tints the whole row so
                # control and feature shots are distinguishable at a glance
                # from the terrain they must not be mixed into.
                tint = GROUP_TINT.get(purpose_group(p.purpose))
                background = (QBrush(DONE_BG) if p.has_reading
                              else QBrush(tint) if tint and tint.alpha() else None)
                if background is not None:
                    for col in range(len(COLUMNS)):
                        self.table.item(row, col).setBackground(background)
        finally:
            self._loading = False

        cov = self.state.plan.coverage()
        self.coverage.setText(
            f"{cov['observed']} of {cov['planned']} shot"
            + (f" · {cov['outstanding']} outstanding" if cov["outstanding"] else
               " · complete"))

        line_ids = [ln.line_id for ln in plan.lines]
        self.ref_line.clear()
        self.ref_line.addItems(line_ids)
        self.line_delegate.set_choices([""] + line_ids)
        self.setup_delegate.set_choices(
            [""] + [s.name for s in plan.setups]
            + sorted({p.setup for p in plan.points if p.setup} -
                     {s.name for s in plan.setups}))

    def _set(self, row: int, col: int, text: str, editable: bool = True) -> None:
        item = self.table.item(row, col)
        if item is None:
            item = QTableWidgetItem()
            self.table.setItem(row, col, item)
        item.setText(text)
        flags = item.flags()
        item.setFlags(flags | Qt.ItemIsEditable if editable
                      else flags & ~Qt.ItemIsEditable)

    def _row_number(self, row: int) -> int | None:
        item = self.table.item(row, COL_NUM)
        return int(item.text()) if item else None

    def selected_numbers(self) -> list[int]:
        """Every selected row's point number, in table order."""
        model = self.table.selectionModel()
        if model is None:
            return []
        numbers = [self._row_number(index.row())
                   for index in sorted(model.selectedRows(),
                                       key=lambda i: i.row())]
        return [n for n in numbers if n is not None]

    def current_number(self) -> int | None:
        """The one row the detail editor acts on.

        With several rows selected there is no single point to edit, so the
        detail panel goes quiet rather than silently applying a typed
        coordinate to whichever row happened to be first.
        """
        numbers = self.selected_numbers()
        return numbers[0] if len(numbers) == 1 else None

    def _on_item_changed(self, item) -> None:
        if self._loading:
            return
        number = self._row_number(item.row())
        point = self.state.plan.by_number(number) if number is not None else None
        if point is None:
            return
        text = item.text().strip()
        col = item.column()

        if col == COL_ROD:
            if not text:
                point.rod_in = None
            else:
                try:
                    point.rod_in = float(text)
                except ValueError:
                    QMessageBox.warning(self, "Rod reading",
                                        f"'{text}' is not a number of inches.")
                    self.refresh()
                    return
        elif col == COL_FIX:
            # Only a FIXED solution is worth taking over the planned position;
            # a float fix is worse than the click, so anything else clears it.
            fixed = text.lower() in ("y", "yes", "1", "true", "x", "fixed")
            point.fix = 4 if fixed else None
            if fixed and point.method == "planned":
                point.method = "rtk"
            elif not fixed and point.method == "rtk":
                point.method = "planned"
        elif col == COL_SETUP:
            point.setup = text
        elif col == COL_NOTE:
            point.observed_note = text
        elif col == COL_PURPOSE:
            try:
                self.state.plan.set_purpose(number, text)
            except ValueError:
                QMessageBox.warning(
                    self, "Purpose", f"'{text}' is not a known purpose.")
                self.refresh()
                return
        elif col == COL_LINE:
            try:
                self.state.plan.assign_line(number, text or None)
            except KeyError as exc:
                QMessageBox.warning(self, "Line", str(exc))
                self.refresh()
                return
        elif col == COL_METHOD:
            if text not in METHODS:
                QMessageBox.warning(
                    self, "Position method", f"'{text}' is not a method.")
                self.refresh()
                return
            point.method = text

        self.changed.emit()

    def _on_selection(self) -> None:
        numbers = self.selected_numbers()
        number = numbers[0] if len(numbers) == 1 else None
        # The map highlights one point; with a multi-row selection there is
        # nothing sensible to highlight, so it clears.
        self.selected.emit(number)
        self._show_detail(number)
        if len(numbers) > 1:
            self.detail_hint.setText(
                f"{len(numbers)} points selected. Delete removes them all; "
                "select a single row to edit its position.")
        self.btn_delete.setText(
            f"Delete {len(numbers)}" if len(numbers) > 1 else "Delete")
        self.btn_delete.setEnabled(bool(numbers))

    # --- detail ----------------------------------------------------------

    def _show_detail(self, number: int | None) -> None:
        point = self.state.plan.by_number(number) if number is not None else None
        if point is None:
            self.detail_hint.setText("Select a row.")
            self.tape_row.setVisible(False)
            self.offset_row.setVisible(False)
            self.rtk_row.setVisible(False)
            self.detail_result.setText("")
            return

        e, n, method, sigma = self.state.plan.resolve(point.number)
        le, ln = self.state.site.to_local(e, n)
        measured = " (measured)" if point.observed_e is not None else ""
        self.detail_hint.setText(
            f"Point {point.number}: {method}{measured}, about "
            f"{m_to_ft(sigma):.1f} ft horizontally "
            f"({m_to_ft(le):.1f}, {m_to_ft(ln):.1f} ft from origin)")

        self.tape_row.setVisible(point.method == "taped")
        self.offset_row.setVisible(point.method == "station_offset")
        self.rtk_row.setVisible(point.method == "rtk")
        self.clear_rtk.setEnabled(point.observed_e is not None)
        if point.method == "rtk":
            self._fill_coords(point)

        if point.method == "taped" and len(point.ties) >= 2:
            self.tie_a.setText(str(point.ties[0].ref_number))
            self.dist_a.setValue(point.ties[0].distance_ft)
            self.tie_b.setText(str(point.ties[1].ref_number))
            self.dist_b.setValue(point.ties[1].distance_ft)
        if point.method == "station_offset":
            if point.ref_line:
                self.ref_line.setCurrentText(point.ref_line)
            self.station.setValue(m_to_ft(point.station_m or 0.0))
            self.offset.setValue(m_to_ft(point.offset_m or 0.0))

    # --- measured coordinates --------------------------------------------

    def _on_frame_changed(self, frame: str) -> None:
        self.coord_x_label.setText(COORD_FRAMES[frame][0])
        self.coord_y_label.setText(COORD_FRAMES[frame][1])
        point = self.state.plan.by_number(self.current_number())
        if point is not None and point.observed_e is not None:
            self._fill_coords(point)

    def _fill_coords(self, point) -> None:
        """Show the stored position in whichever frame is selected."""
        if point.observed_e is None:
            self.coord_x.clear()
            self.coord_y.clear()
            return
        frame = self.coord_frame.currentText()
        if frame == "local ft":
            le, ln = self.state.site.to_local(point.observed_e, point.observed_n)
            x, y, places = m_to_ft(le), m_to_ft(ln), 2
        elif frame == "UTM m":
            x, y, places = point.observed_e, point.observed_n, 3
        else:
            from pyproj import Transformer

            tx = Transformer.from_crs(f"EPSG:{self.state.site.epsg}",
                                      "EPSG:4326", always_xy=True)
            lon, lat = tx.transform(point.observed_e, point.observed_n)
            x, y, places = lon, lat, 9
        self.coord_x.setText(f"{x:.{places}f}")
        self.coord_y.setText(f"{y:.{places}f}")

    def _apply_coords(self) -> None:
        point = self.state.plan.by_number(self.current_number())
        if point is None:
            return
        try:
            x = float(self.coord_x.text().strip())
            y = float(self.coord_y.text().strip())
        except ValueError:
            QMessageBox.warning(
                self, "Measured position",
                "Both coordinates must be numbers. Leave them blank and press "
                "'Clear, use planned' to fall back to the planned mark.")
            return

        site = self.state.site
        frame = self.coord_frame.currentText()
        try:
            if frame == "local ft":
                e, n = site.to_projected(ft_to_m(x), ft_to_m(y))
            elif frame == "UTM m":
                e, n = x, y
            else:
                from pyproj import Transformer

                if not (-180 <= x <= 180 and -90 <= y <= 90):
                    raise ValueError(
                        "longitude must be -180..180 and latitude -90..90; "
                        "the fields may be the wrong way round")
                tx = Transformer.from_crs("EPSG:4326", f"EPSG:{site.epsg}",
                                          always_xy=True)
                e, n = tx.transform(x, y)
        except ValueError as exc:
            QMessageBox.warning(self, "Measured position", str(exc))
            return

        # A position far outside the surveyed area is far more likely to be a
        # mistyped frame than a real shot, and silently accepting it would move
        # the point somewhere unfindable.
        away = max(abs(e - point.planned_e), abs(n - point.planned_n))
        if away > 500.0:
            answer = QMessageBox.question(
                self, "Measured position",
                f"That places point {point.number} {m_to_ft(away):,.0f} ft from "
                f"where it was planned.\n\nThat usually means the frame is "
                f"wrong, or east and north are swapped. Use it anyway?")
            if answer != QMessageBox.Yes:
                return

        point.observed_e, point.observed_n = float(e), float(n)
        point.method = "rtk"
        point.fix = 4
        le, ln = site.to_local(e, n)
        moved = ((e - point.planned_e) ** 2 + (n - point.planned_n) ** 2) ** 0.5
        self.detail_result.setText(
            f"Measured position set: {m_to_ft(le):.2f}, {m_to_ft(ln):.2f} ft "
            f"from origin — {m_to_ft(moved):.2f} ft from the planned mark.")
        self.changed.emit()

    def _clear_coords(self) -> None:
        point = self.state.plan.by_number(self.current_number())
        if point is None:
            return
        point.observed_e = point.observed_n = None
        point.fix = None
        point.method = "planned"
        self.detail_result.setText(
            "Measured position cleared; the planned mark stands.")
        self.changed.emit()

    def _apply_tape(self) -> None:
        point = self.state.plan.by_number(self.current_number())
        if point is None:
            return
        try:
            point.ties = [Tie(int(self.tie_a.text()), ft_to_m(self.dist_a.value())),
                          Tie(int(self.tie_b.text()), ft_to_m(self.dist_b.value()))]
            point.method = "taped"
            e, n, stats = self.state.plan.trilaterate(point)
        except (ValueError, KeyError) as exc:
            QMessageBox.warning(self, "Taped position", str(exc))
            return

        residual = m_to_ft(stats["max_residual_m"]) * 12
        self.detail_result.setText(
            f"Solved from {stats['n_ties']} ties, dof {stats['dof']}"
            + (f", worst residual {residual:.1f} in" if stats["dof"] > 0 else
               " — exactly determined, so no check on the tape"))
        self.changed.emit()

    def _apply_offset(self) -> None:
        point = self.state.plan.by_number(self.current_number())
        if point is None:
            return
        point.method = "station_offset"
        point.ref_line = self.ref_line.currentText()
        point.station_m = ft_to_m(self.station.value())
        point.offset_m = ft_to_m(self.offset.value())
        try:
            self.state.plan.station_offset(point)
        except ValueError as exc:
            QMessageBox.warning(self, "Station and offset", str(exc))
            return
        self.detail_result.setText(
            f"Placed along {point.ref_line}; positive offset is left of travel "
            "from the line's first vertex toward its last.")
        self.changed.emit()

    # --- editing ---------------------------------------------------------

    def _delete_selected(self) -> None:
        numbers = self.selected_numbers()
        if not numbers:
            return
        # Confirm only in bulk. Deleting one point by mistake is obvious and
        # trivially redone; deleting thirty is neither, and a run selected by
        # shift-click is easy to get wrong by a screenful.
        if len(numbers) > 1:
            answer = QMessageBox.question(
                self, "Delete points",
                f"Delete {len(numbers)} points ("
                + ", ".join(f"#{n}" for n in numbers[:8])
                + (", ..." if len(numbers) > 8 else "")
                + ")?\n\n"
                "Any readings already typed against them go too. Numbers are "
                "never reused, so the ones left keep the numbers on your "
                "field sheet.")
            if answer != QMessageBox.Yes:
                return
        for number in numbers:
            self.state.plan.remove_point(number)
        self.changed.emit()

    def _insert_vertex(self) -> None:
        plan = self.state.plan
        number = self.current_number()
        if number is None:
            return
        line = next((ln for ln in plan.lines if number in ln.numbers), None)
        if line is None:
            QMessageBox.information(
                self, "Insert vertex",
                "Select a break line vertex. Loose shots are not part of a line.")
            return

        index = line.numbers.index(number)
        if index + 1 >= len(line.numbers):
            QMessageBox.information(
                self, "Insert vertex",
                "That is the last vertex; there is nothing to insert before.")
            return

        a = plan.by_number(line.numbers[index])
        b = plan.by_number(line.numbers[index + 1])
        new = plan.add_point((a.planned_e + b.planned_e) / 2,
                             (a.planned_n + b.planned_n) / 2,
                             purpose="breakline")
        # New numbers always go on the end, but the vertex belongs in the
        # middle of the RUN - the sheet is ordered by number, the line by shape.
        line.numbers.insert(index + 1, new.number)
        self.changed.emit()
