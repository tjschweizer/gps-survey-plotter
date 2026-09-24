"""Basemap panel.

Every raster that has been fetched, with a checkbox and its own opacity. They
stack bottom to top in the order listed, which puts photographs underneath
LiDAR products - so a hillshade at 40% over an aerial photo is one click and a
slider away, and that combination is the most useful view for spotting break
lines against real ground features.

Sources that returned nothing for this lot are not listed at all. There is no
value in a checkbox that can only ever show an empty tile; what happened to
them is reported once, when the fetch runs.

The alignment group at the bottom shifts every basemap together. County and
state orthos are georeferenced to a foot or two, which is fine for finding the
house and useless for clicking a driveway edge, and the residual error over a
lot this size is essentially a single translation. It is solved from plan
points that were clicked on a feature in the photo and then shot with RTK - see
`gpsrtk.georef`. The shift moves the PHOTO; the measurements never move.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QDoubleSpinBox, QFrame, QGridLayout,
                               QGroupBox, QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QSizePolicy, QSlider, QVBoxLayout,
                               QWidget)

from ..units import ft_to_m, m_to_ft

EMPTY = ("No basemaps fetched.\n\n"
         "Data ▸ Fetch all basemaps tries every source once and reports which "
         "ones actually cover this lot.")


class BasemapRow(QFrame):
    """One raster: visibility, name, resolution, opacity."""

    def __init__(self, state, basemap, parent=None):
        super().__init__(parent)
        self.state = state
        self.name = basemap.provider
        self.setFrameShape(QFrame.StyledPanel)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 4, 6, 4)
        outer.setSpacing(2)

        top = QHBoxLayout()
        self.check = QCheckBox(basemap.provider)
        # Provider names are long; without this the checkbox alone sets a
        # 230 px floor on the dock.
        self.check.setMinimumWidth(60)
        self.check.setChecked(basemap.visible)
        self.check.toggled.connect(self._on_toggle)
        if basemap.terrain:
            self.check.setStyleSheet("font-style: italic;")
            self.check.setToolTip("LiDAR-derived raster, not a photograph")
        top.addWidget(self.check, 1)
        outer.addLayout(top)

        # This is the rate the tile was SAMPLED at, which is set by the request
        # size, not the source's native resolution - asking a 60 cm mosaic for
        # a 1024 px tile of an 86 m lot yields 8.4 cm pixels of upsampled mush.
        # The provider names carry the real figure, so say which one this is.
        gsd = QLabel(f"sampled at {basemap.layer.px * 100:.1f} cm/px")
        gsd.setStyleSheet("color: palette(mid); font-size: 10px;")
        gsd.setToolTip("Pixel spacing of the fetched tile. The native "
                       "resolution of the source is in its name.")
        outer.addWidget(gsd)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("opacity"))
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(int(basemap.opacity * 100))
        self.slider.valueChanged.connect(self._on_opacity)
        row.addWidget(self.slider, 1)
        self.pct = QLabel(f"{int(basemap.opacity * 100)}%")
        self.pct.setMinimumWidth(34)
        row.addWidget(self.pct)
        outer.addLayout(row)

        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)

    def _on_toggle(self, on: bool) -> None:
        self.state.set_basemap_visible(self.name, on)

    def _on_opacity(self, value: int) -> None:
        self.pct.setText(f"{value}%")
        self.state.set_basemap_opacity(self.name, value / 100.0)

    def sync(self, basemap) -> None:
        """Match the widgets to the state without re-emitting their signals.

        Visibility and opacity can be changed from outside this panel - opening
        a project, or fetching everything at once - and without this the
        checkboxes silently disagree with what the map is drawing.
        """
        for widget, value in ((self.check, basemap.visible),
                              (self.slider, int(basemap.opacity * 100))):
            blocked = widget.blockSignals(True)
            try:
                if widget is self.check:
                    widget.setChecked(value)
                else:
                    widget.setValue(value)
            finally:
                widget.blockSignals(blocked)
        self.pct.setText(f"{int(basemap.opacity * 100)}%")


class BasemapPanel(QWidget):
    """The stack of fetched rasters, and how they are aligned to the survey."""

    solveOffsetRequested = Signal()

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self._loading = False
        self._names: list[str] = []
        self._rows: dict[str, BasemapRow] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)

        self.container = QWidget()
        self.stack = QVBoxLayout(self.container)
        self.stack.setContentsMargins(0, 0, 0, 0)
        self.stack.setSpacing(4)
        self.stack.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(self.container)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        root.addWidget(scroll, 1)

        self.empty = QLabel(EMPTY)
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet("color: palette(mid);")
        root.addWidget(self.empty)

        row = QHBoxLayout()
        self.fetch_btn = QPushButton("Fetch all basemaps")
        row.addWidget(self.fetch_btn)
        self.clear_btn = QPushButton("Clear")
        row.addWidget(self.clear_btn)
        root.addLayout(row)

        root.addWidget(self._build_alignment())

    # --- alignment -------------------------------------------------------

    def _build_alignment(self) -> QGroupBox:
        """Shift every basemap together, by hand or solved from points.

        Laid out as a grid with narrow spin boxes: a row of labelled controls
        here is one of the things that historically set an unshrinkable floor
        on the whole window's width.
        """
        box = QGroupBox("Alignment")
        grid = QGridLayout(box)
        grid.setContentsMargins(6, 4, 6, 6)
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(3)

        self.off_e = QDoubleSpinBox()
        self.off_n = QDoubleSpinBox()
        for spin in (self.off_e, self.off_n):
            spin.setRange(-500.0, 500.0)
            spin.setDecimals(2)
            spin.setSingleStep(0.10)
            spin.setSuffix(" ft")
            spin.setMinimumWidth(72)
            spin.valueChanged.connect(self._on_manual)
        grid.addWidget(QLabel("east"), 0, 0)
        grid.addWidget(self.off_e, 0, 1)
        grid.addWidget(QLabel("north"), 1, 0)
        grid.addWidget(self.off_n, 1, 1)
        grid.setColumnStretch(1, 1)

        self.solve_btn = QPushButton("Solve from points")
        self.solve_btn.setToolTip(
            "Uses plan points whose mark was clicked on a feature visible in "
            "the photo and which have since been measured. Select rows in the "
            "shot plan to choose them yourself.")
        self.solve_btn.clicked.connect(self.solveOffsetRequested)
        grid.addWidget(self.solve_btn, 2, 0, 1, 2)

        self.reset_btn = QPushButton("No shift")
        self.reset_btn.clicked.connect(self._on_reset)
        grid.addWidget(self.reset_btn, 3, 0, 1, 2)

        self.offset_note = QLabel("Imagery drawn where it says it is.")
        self.offset_note.setWordWrap(True)
        self.offset_note.setMinimumWidth(80)
        self.offset_note.setStyleSheet("color: palette(mid); font-size: 10px;")
        grid.addWidget(self.offset_note, 4, 0, 1, 2)
        return box

    def _on_manual(self) -> None:
        if self._loading:
            return
        from ..georef import ImageryOffset

        # A hand-typed shift has no points behind it, so it is stored with no
        # statistics rather than inheriting the ones from a previous solve.
        self.state.set_imagery_offset(
            ImageryOffset(de=ft_to_m(self.off_e.value()),
                          dn=ft_to_m(self.off_n.value())))
        self.sync_offset()

    def _on_reset(self) -> None:
        self.state.clear_imagery_offset()
        self.sync_offset()

    def sync_offset(self) -> None:
        """Match the controls and the note to the stored offset."""
        offset = self.state.imagery_offset
        self._loading = True
        try:
            self.off_e.setValue(m_to_ft(offset.de))
            self.off_n.setValue(m_to_ft(offset.dn))
        finally:
            self._loading = False
        self.offset_note.setText(
            "Imagery drawn where it says it is." if offset.zero
            else offset.describe())

    def refresh(self) -> None:
        """Rebuild rows only when the set of basemaps changed, but always sync.

        Tearing the rows down on every slider tick would fight the drag, so the
        widgets are reused. They still have to be synced, because visibility
        and opacity also change from outside this panel - opening a project, or
        fetching everything at once.
        """
        names = list(self.state.basemaps)
        if names != self._names:
            self._names = names
            self._rows = {}
            while self.stack.count() > 1:
                item = self.stack.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            for basemap in self.state.basemaps.values():
                row = BasemapRow(self.state, basemap)
                self._rows[basemap.provider] = row
                self.stack.insertWidget(self.stack.count() - 1, row)
            self.empty.setVisible(not names)

        for name, row in self._rows.items():
            basemap = self.state.basemaps.get(name)
            if basemap is not None:
                row.sync(basemap)

        self.sync_offset()
