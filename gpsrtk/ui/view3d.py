"""3D surface view.

Vertical exaggeration is a view setting, never a pipeline setting - the numbers
underneath must stay in real metres. The default of 8x is chosen because the
site has roughly 1.2 m of relief across 46 m; at 1:1 a residential lawn looks
perfectly flat and tells you nothing about drainage.

Unmeasured cells are rendered fully transparent via NaN scalars rather than
being dropped from the mesh, which keeps the geometry a simple regular grid
while still refusing to colour ground that was never surveyed.
"""

from __future__ import annotations

import os

import numpy as np
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QPushButton,
                               QSlider, QVBoxLayout, QWidget)
from PySide6.QtCore import Qt

from .palette import COLORMAPS

try:
    import pyvista as pv
    from pyvistaqt import QtInteractor
    HAVE_3D = True
    _IMPORT_ERROR = None
except Exception as _exc:                                  # noqa: BLE001
    HAVE_3D = False
    _IMPORT_ERROR = _exc

# Qt platforms that cannot give VTK a real OpenGL context. Constructing a
# QtInteractor on one does not fail cleanly - it takes the process down with an
# access violation - so the check has to happen before the widget is made.
# The same guard covers machines with broken GL drivers and remote desktop
# sessions, where the alternative is a hard crash on startup.
HEADLESS_PLATFORMS = {"offscreen", "minimal", "vnc"}


def three_d_available() -> tuple[bool, str]:
    """Whether a 3D view can safely be constructed, and why not if it cannot."""
    if not HAVE_3D:
        return False, f"pyvista/VTK did not import: {_IMPORT_ERROR}"
    if os.environ.get("GPSRTK_NO_3D"):
        return False, "disabled by GPSRTK_NO_3D"
    app = QGuiApplication.instance()
    platform = app.platformName() if app is not None else ""
    if platform in HEADLESS_PLATFORMS:
        return False, f"no OpenGL context on the '{platform}' Qt platform"
    return True, ""


class View3D(QWidget):
    """Interactive 3D render of the derived surface."""

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.plotter = None
        self._mesh_actor = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        self.enabled, why = three_d_available()
        if not self.enabled:
            msg = QLabel(f"3D view unavailable — {why}.\n\n"
                         "The plan view and every export are unaffected.")
            msg.setAlignment(Qt.AlignCenter)
            msg.setWordWrap(True)
            root.addWidget(msg)
            return

        bar = QHBoxLayout()
        bar.setContentsMargins(6, 4, 6, 0)

        bar.addWidget(QLabel("vertical exaggeration"))
        self.ve = QSlider(Qt.Horizontal)
        self.ve.setRange(1, 40)
        self.ve.setValue(8)
        self.ve.setMinimumWidth(60)
        self.ve.setMaximumWidth(160)
        self.ve.valueChanged.connect(self._on_ve)
        bar.addWidget(self.ve)
        self.ve_label = QLabel("8×")
        self.ve_label.setMinimumWidth(30)
        bar.addWidget(self.ve_label)

        bar.addWidget(QLabel("  map"))
        self.cmap = QComboBox()
        self.cmap.addItems(COLORMAPS)
        self.cmap.setCurrentText("terrain")
        self.cmap.currentTextChanged.connect(self.refresh)
        bar.addWidget(self.cmap)

        reset = QPushButton("Reset view")
        reset.clicked.connect(self.reset_view)
        bar.addWidget(reset)
        bar.addStretch(1)
        root.addLayout(bar)

        self.plotter = QtInteractor(self)
        self.plotter.set_background("white")
        root.addWidget(self.plotter.interactor, 1)

    # --- drawing ---------------------------------------------------------

    def _on_ve(self, value: int) -> None:
        self.ve_label.setText(f"{value}×")
        self.refresh()

    def refresh(self) -> None:
        if not self.enabled or self.plotter is None:
            return
        s = self.state.surface
        if self._mesh_actor is not None:
            self.plotter.remove_actor(self._mesh_actor, render=False)
            self._mesh_actor = None
        if s is None:
            self.plotter.render()
            return

        ny, nx = s.z.shape
        grid = pv.ImageData(dimensions=(nx, ny, 1),
                            spacing=(s.px, s.px, 1.0),
                            origin=(s.extent.xmin, s.extent.ymin, 0.0))

        # Row 0 of `z` is the south edge and ImageData runs x fastest, so C
        # order is the correct ravel here. Getting this wrong transposes the
        # terrain silently, which is why it is spelled out.
        grid["warp"] = s.z.ravel(order="C")
        mesh = grid.warp_by_scalar("warp", factor=float(self.ve.value()))
        mesh["elevation"] = s.z_masked.ravel(order="C")

        self._mesh_actor = self.plotter.add_mesh(
            mesh, scalars="elevation", cmap=self.cmap.currentText(),
            nan_opacity=0.0, smooth_shading=True, show_scalar_bar=True,
            # Relief here is barely a metre, so the default label format
            # renders every tick as "263." - useless. Force two decimals.
            scalar_bar_args={"title": "elevation (m)", "color": "black",
                             "vertical": True, "position_x": 0.88,
                             "width": 0.05, "height": 0.6,
                             "fmt": "%.2f", "n_labels": 6},
            lighting=True, ambient=0.25, diffuse=0.7, specular=0.15)
        self.plotter.render()

    def reset_view(self) -> None:
        if self.enabled and self.plotter is not None:
            self.plotter.view_isometric()
            self.plotter.reset_camera()

    def close_plotter(self) -> None:
        """VTK holds an OpenGL context that must be released explicitly."""
        if self.enabled and self.plotter is not None:
            self.plotter.close()
