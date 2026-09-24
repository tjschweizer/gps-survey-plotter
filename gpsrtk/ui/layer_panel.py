"""Layer panel.

Two independent things per layer, which are easy to conflate:

  visible  whether it is drawn
  active   whether it is the layer the filter chain and surface are built from

Only one layer can be active. Spot layers stay visible as reference while the
surface is built from continuous logging, which is exactly how the laser shots
are meant to be used - as an independent check, not as surface input. Mixing
them injects the antenna-height offset as spikes.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QGridLayout, QLabel,
                               QRadioButton, QSizePolicy, QVBoxLayout, QWidget)


class LayerPanel(QWidget):
    """Visibility toggles and active-layer selection."""

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)

        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(6, 6, 6, 6)
        self.root.setSpacing(4)

        header = QGridLayout()
        header.addWidget(QLabel("<b>show</b>"), 0, 0)
        header.addWidget(QLabel("<b>source</b>"), 0, 1)
        header.addWidget(QLabel("<b>layer</b>"), 0, 2)
        self.root.addLayout(header)

        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(10)
        self.root.addLayout(self.grid)
        self.root.addStretch(1)

        self.empty = QLabel("No data loaded.\nFile ▸ Open export…")
        self.empty.setStyleSheet("color: palette(mid);")
        self.root.addWidget(self.empty)

    def rebuild(self) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            w = item.widget()
            if w is None:
                continue
            if isinstance(w, QRadioButton):
                self.group.removeButton(w)
            w.deleteLater()

        layers = self.state.layers
        self.empty.setVisible(not layers)

        for row, (name, ps) in enumerate(layers.items()):
            show = QCheckBox()
            show.setChecked(self.state.visible.get(name, True))
            show.toggled.connect(
                lambda on, n=name: self.state.set_visible(n, on))
            self.grid.addWidget(show, row, 0)

            active = QRadioButton()
            active.setChecked(name == self.state.active_layer)
            active.setToolTip("build the surface from this layer")
            active.toggled.connect(
                lambda on, n=name: on and self.state.set_active_layer(n))
            self.group.addButton(active)
            self.grid.addWidget(active, row, 1)

            label = QLabel(f"{name}<br><span style='color:gray'>{ps.describe()}</span>")
            label.setTextFormat(Qt.RichText)
            # Without wrapping, the full summary string ("8,194 points | fixed
            # 4,245 / float 3,949 | z 260.890-265.375 m") sets this dock's
            # minimum width to over 800 px, which alone made the whole main
            # window too wide to resize.
            label.setWordWrap(True)
            label.setMinimumWidth(90)
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            self.grid.addWidget(label, row, 2)

        self.grid.setColumnStretch(2, 1)
