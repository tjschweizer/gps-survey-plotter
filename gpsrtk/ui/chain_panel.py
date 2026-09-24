"""Filter stack panel.

Shows the chain as an ordered list of stages, each with its parameters and its
points in/out. The counts are the point of this panel as much as the controls
are: a chain that quietly drops most of the data is the easiest way to produce
a confident, wrong surface, and that should be visible without being asked for.

Parameter editors are generated from each stage's `params()` rather than being
hand-built per stage, so a newly registered stage appears here with working
controls and no UI code.
"""

from __future__ import annotations

import ast

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from ..filters import REGISTRY, FilterChain, Stage


def _parse(text: str):
    """Best-effort literal parse; anything unparseable stays a string."""
    text = text.strip()
    if not text:
        return None
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return text


class StageWidget(QFrame):
    """One stage: enable toggle, parameters, and its throughput."""

    changed = Signal()
    removeRequested = Signal(object)
    moveRequested = Signal(object, int)

    def __init__(self, stage: Stage, parent=None):
        super().__init__(parent)
        self.stage = stage
        self.setFrameShape(QFrame.StyledPanel)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 4, 6, 4)
        outer.setSpacing(3)

        header = QHBoxLayout()
        self.enable = QCheckBox(stage.label)
        self.enable.setChecked(stage.enabled)
        self.enable.setStyleSheet("font-weight: bold;")
        self.enable.toggled.connect(self._on_enable)
        header.addWidget(self.enable)
        header.addStretch(1)

        for text, tip, delta in (("▲", "move up", -1),
                                 ("▼", "move down", +1)):
            b = QPushButton(text)
            b.setFixedWidth(24)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, d=delta: self.moveRequested.emit(self, d))
            header.addWidget(b)

        rm = QPushButton("✕")
        rm.setFixedWidth(24)
        rm.setToolTip("remove stage")
        rm.clicked.connect(lambda: self.removeRequested.emit(self))
        header.addWidget(rm)
        outer.addLayout(header)

        # --- generated parameter editors ---
        self.editors: dict[str, QWidget] = {}
        params = stage.params()
        if params:
            grid = QGridLayout()
            grid.setContentsMargins(14, 0, 0, 0)
            grid.setHorizontalSpacing(6)
            grid.setVerticalSpacing(2)
            for row, (key, value) in enumerate(params.items()):
                grid.addWidget(QLabel(key), row, 0)
                if isinstance(value, bool):
                    w = QCheckBox()
                    w.setChecked(value)
                    w.toggled.connect(self._on_param)
                else:
                    w = QLineEdit("" if value is None else repr(value)
                                  if isinstance(value, str) else str(value))
                    w.setPlaceholderText("none")
                    w.editingFinished.connect(self._on_param)
                self.editors[key] = w
                grid.addWidget(w, row, 1)
            grid.setColumnStretch(1, 1)
            outer.addLayout(grid)

        self.counts = QLabel("")
        f = QFont("Consolas")
        f.setPointSize(8)
        self.counts.setFont(f)
        self.counts.setStyleSheet("color: palette(mid);")
        outer.addWidget(self.counts)

        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)

    def _on_enable(self, on: bool) -> None:
        self.stage.enabled = on
        self._grey()
        self.changed.emit()

    def _on_param(self, *_) -> None:
        for key, w in self.editors.items():
            value = w.isChecked() if isinstance(w, QCheckBox) else _parse(w.text())
            setattr(self.stage, key, value)
        self.changed.emit()

    def _grey(self) -> None:
        self.setEnabled(True)
        op = 1.0 if self.stage.enabled else 0.45
        self.setStyleSheet(f"QFrame {{ }} QLabel {{ color: rgba(0,0,0,{op}); }}")

    def set_result(self, result) -> None:
        if result is None:
            self.counts.setText("")
            return
        if not result.enabled:
            self.counts.setText("disabled")
            return
        self.counts.setText(
            f"{result.n_in:,} → {result.n_out:,}  ({result.kept * 100:.1f}%)")


class ChainPanel(QWidget):
    """The whole stack, plus controls to add stages."""

    changed = Signal()

    def __init__(self, chain: FilterChain, parent=None):
        super().__init__(parent)
        self.chain = chain

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

        add = QHBoxLayout()
        self.picker = QComboBox()
        for kind in sorted(REGISTRY):
            self.picker.addItem(kind)
        add.addWidget(self.picker, 1)
        b = QPushButton("Add")
        b.clicked.connect(self._add)
        add.addWidget(b)
        root.addLayout(add)

        self.total = QLabel("")
        self.total.setStyleSheet("font-weight: bold;")
        self.total.setWordWrap(True)
        root.addWidget(self.total)

        self.rebuild()

    # --- construction ----------------------------------------------------

    def rebuild(self) -> None:
        while self.stack.count() > 1:
            item = self.stack.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for stage in self.chain.stages:
            w = StageWidget(stage)
            w.changed.connect(self._on_changed)
            w.removeRequested.connect(self._remove)
            w.moveRequested.connect(self._move)
            self.stack.insertWidget(self.stack.count() - 1, w)

    def _widgets(self) -> list[StageWidget]:
        return [self.stack.itemAt(i).widget()
                for i in range(self.stack.count() - 1)
                if isinstance(self.stack.itemAt(i).widget(), StageWidget)]

    # --- editing ---------------------------------------------------------

    def _add(self) -> None:
        self.chain.stages.append(REGISTRY[self.picker.currentText()]())
        self.rebuild()
        self._on_changed()

    def _remove(self, widget: StageWidget) -> None:
        self.chain.stages.remove(widget.stage)
        self.rebuild()
        self._on_changed()

    def _move(self, widget: StageWidget, delta: int) -> None:
        stages = self.chain.stages
        i = stages.index(widget.stage)
        j = i + delta
        if 0 <= j < len(stages):
            stages[i], stages[j] = stages[j], stages[i]
            self.rebuild()
            self._on_changed()

    def _on_changed(self) -> None:
        self.changed.emit()

    # --- results ---------------------------------------------------------

    def show_results(self, chain: FilterChain) -> None:
        widgets = self._widgets()
        for w, r in zip(widgets, chain.results):
            w.set_result(r)
        for w in widgets[len(chain.results):]:
            w.set_result(None)

        if chain.results:
            head = chain.results[0].n_in
            tail = chain.results[-1].n_out
            pct = tail / head * 100 if head else 0
            self.total.setText(f"{head:,} → {tail:,} points  ({pct:.1f}% kept)")
        else:
            self.total.setText("")
