"""Datum tie dialog.

Tying the survey to the Revit model is not a correction applied on top of the
local datum - it is a change to which point the whole network hangs from and
what value that point is held at. Holding an arbitrary spot at 100.000 ft and
holding a garage slab shot at its real project elevation are the same operation
with different inputs.

The dialog therefore edits exactly two things - which point, and what
elevation - plus a flag recording whether that elevation means anything outside
this project. That flag is the important one: an export reading 0.25 ft and one
reading 100.00 ft look equally plausible, and only the flag distinguishes a
tied datum from a chosen number.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFormLayout, QLabel, QLineEdit,
                               QVBoxLayout)

HELP = (
    "The survey hangs from one shot. Until that shot is a KNOWN feature held "
    "at its real elevation, the surface is self-consistent but sits nowhere in "
    "particular.\n\n"
    "To tie it: shoot a feature whose elevation you know in the Revit model — "
    "a garage slab, a door threshold — then pick that point here and enter its "
    "project elevation."
)


class DatumTieDialog(QDialog):
    """Edits the site's benchmark point and elevation."""

    def __init__(self, site, spot_ids: list[int], parent=None):
        super().__init__(parent)
        self.site = site
        self.setWindowTitle("Datum tie")
        self.setMinimumWidth(430)

        root = QVBoxLayout(self)
        blurb = QLabel(HELP)
        blurb.setWordWrap(True)
        blurb.setStyleSheet("color: palette(mid);")
        root.addWidget(blurb)

        form = QFormLayout()
        v = site.vertical

        self.point = QComboBox()
        self.point.setEditable(True)
        for pid in spot_ids:
            self.point.addItem(str(pid))
        self.point.setCurrentText(str(v.benchmark_point_id))
        self.point.setToolTip("The rod shot taken on the known feature")
        form.addRow("Benchmark point", self.point)

        self.elev = QDoubleSpinBox()
        self.elev.setRange(-100000.0, 100000.0)
        self.elev.setDecimals(4)
        self.elev.setSuffix("  ft")
        self.elev.setValue(v.benchmark_elev_ft)
        form.addRow("Held at elevation", self.elev)

        self.note = QLineEdit(v.benchmark_note)
        self.note.setPlaceholderText("garage slab at the overhead door, say")
        form.addRow("What was shot", self.note)

        self.frame = QLineEdit(v.model_frame or "Revit project")
        form.addRow("Reference frame", self.frame)

        self.tied = QCheckBox(
            "This is a real elevation in that frame, not a chosen number")
        self.tied.setChecked(v.tied_to_model)
        self.tied.toggled.connect(self._on_tied)
        form.addRow("", self.tied)
        root.addLayout(form)

        self.warning = QLabel()
        self.warning.setWordWrap(True)
        root.addWidget(self.warning)
        self._on_tied(self.tied.isChecked())

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _on_tied(self, on: bool) -> None:
        if on:
            self.warning.setText(
                "Exports will be labelled as tied. Only check this once the "
                "elevation above really is the feature's elevation in the model.")
            self.warning.setStyleSheet("color: #1f5fa8;")
        else:
            self.warning.setText(
                "Datum stays local and arbitrary. Exports will say so.")
            self.warning.setStyleSheet("color: palette(mid);")

    def apply_to_site(self) -> None:
        v = self.site.vertical
        try:
            v.benchmark_point_id = int(float(self.point.currentText()))
        except ValueError as exc:
            raise ValueError(
                f"'{self.point.currentText()}' is not a point id") from exc
        v.benchmark_elev_ft = float(self.elev.value())
        v.benchmark_note = self.note.text().strip()
        v.model_frame = self.frame.text().strip() or "Revit project"
        v.tied_to_model = self.tied.isChecked()
        v.name = (f"tied to {v.model_frame}" if v.tied_to_model
                  else "local arbitrary")
