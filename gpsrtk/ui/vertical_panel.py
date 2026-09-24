"""Vertical datum panel.

Shows what the elevations currently mean, because "97.9 ft" and "860.4 ft" look
equally plausible on screen and only one of them is on the datum you think you
are working in. When no model has been solved, this says so in as many words
rather than leaving the reader to infer it.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from ..model.pointset import SESSION

UNSOLVED = (
    "No vertical model applied.\n\n"
    "Elevations are RAW ELLIPSOIDAL HEIGHT of the antenna - roughly 860 ft "
    "here, not the site's 100 ft datum, and not height above ground.\n\n"
    "Datum ▸ Solve local datum ties the walked data to the laser spots, "
    "which is what puts the surface on the benchmark."
)


class VerticalPanel(QWidget):
    """Reports the solved vertical model, or its absence."""

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        self.body = QLabel(UNSOLVED)
        self.body.setWordWrap(True)
        self.body.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        f = QFont("Consolas")
        f.setPointSize(8)
        self.body.setFont(f)

        scroll = QScrollArea()
        scroll.setWidget(self.body)
        scroll.setWidgetResizable(True)
        root.addWidget(scroll, 1)

        row = QHBoxLayout()
        self.solve_btn = QPushButton("Solve local datum")
        self.solve_btn.setToolTip(
            "Level the laser network, solve per-session offsets, then tie the "
            "walked data to the laser ground surface")
        row.addWidget(self.solve_btn)
        self.clear_btn = QPushButton("Clear")
        row.addWidget(self.clear_btn)
        root.addLayout(row)

    def refresh(self) -> None:
        model = self.state.vertical
        if model is None:
            self.body.setText(UNSOLVED + self._merge_warning())
            self.clear_btn.setEnabled(False)
            return

        self.clear_btn.setEnabled(True)
        parts = [model.describe()]
        if model.level is not None:
            parts.append("")
            parts.append(model.level.describe())
        if model.sessions is not None:
            parts.append("")
            parts.append(model.sessions.describe())
        self.body.setText("\n".join(parts))

    def _merge_warning(self) -> str:
        """Name the hazard that only exists once data has been merged.

        Several sessions with no model applied is not the same situation as
        one session with no model applied: the second is merely on the wrong
        datum, the first is on several at once, and the surface steps where
        they meet.
        """
        source = self.state.source
        if source is None or SESSION not in source.df.columns:
            return ""
        names = sorted(set(source.df[SESSION].astype(str)))
        if len(names) < 2:
            return ""
        return ("\n\nTHIS LAYER SPANS {} SESSIONS:\n  ".format(len(names))
                + "\n  ".join(names)
                + "\n\nEach was surveyed with its own antenna mount and base "
                  "selection, so they sit on different datums. "
                  "Datum \u25b8 Solve session offsets only removes the step "
                  "between them without touching the benchmark.")
