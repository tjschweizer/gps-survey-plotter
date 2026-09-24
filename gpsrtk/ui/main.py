"""Main window.

Layout: layers, the filter stack, and the vertical datum on the left; the views
in a tabbed centre; a QC readout at the bottom. The QC panel is not an
afterthought - CLAUDE.md is explicit that measured QC beats the receiver's own
accuracy estimates, so crossover statistics belong on screen next to the
surface they describe rather than in a log file somewhere.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QFont, QGuiApplication
from PySide6.QtWidgets import (QApplication, QDockWidget, QFileDialog, QFrame,
                               QLabel, QMainWindow, QMessageBox, QScrollArea,
                               QSizePolicy, QSplitter, QTabWidget,
                               QVBoxLayout, QWidget)

from .. import qc
from ..io.imagery import NoCoverageError, default_providers, terrain_providers
from ..io.vector import default_vector_providers
from ..model.pointset import ELEV
from ..units import m_to_ft
from ..project import SUFFIX as PROJECT_SUFFIX
from .basemap_panel import BasemapPanel
from .plan_overlay import PlanOverlay
from .plan_panel import PlanPanel
from .chain_panel import ChainPanel
from .layer_panel import LayerPanel
from .map2d import Map2D
from .state import AppState
from .vertical_panel import VerticalPanel
from .view3d import View3D


# Narrow enough that three docks and the map fit on a 1920 px screen with
# room to spare, wide enough that the panels are still usable at that size.
DOCK_MIN_WIDTH = 210
DOCK_DEFAULT_WIDTH = 330


class MainWindow(QMainWindow):
    def __init__(self, state: AppState | None = None):
        super().__init__()
        self._docks: list[QDockWidget] = []
        self.state = state or AppState()
        self.setWindowTitle("Yard Survey")
        self.resize(1560, 980)

        self.imagery_providers = {**default_providers(), **terrain_providers()}
        self.photo_providers = default_providers()
        self.terrain_rasters = terrain_providers()
        self.vector_providers = default_vector_providers()

        self.map2d = Map2D(self.state)
        self.view3d = View3D(self.state)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.map2d, "Plan")
        self.tabs.addTab(self.view3d, "3D")

        self.qc_label = QLabel("")
        self.qc_label.setFont(QFont("Consolas", 9))
        self.qc_label.setContentsMargins(8, 6, 8, 6)
        self.qc_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # Long monospace lines with no wrapping set the central widget's
        # minimum width to 1300 px, which was the remaining reason the docks
        # could not be widened on a 1920 px screen.
        self.qc_label.setWordWrap(True)
        self.qc_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.qc_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)

        centre = QSplitter(Qt.Vertical)
        centre.addWidget(self.tabs)
        holder = QWidget()
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(self.qc_label)
        centre.addWidget(holder)
        centre.setStretchFactor(0, 1)
        centre.setSizes([760, 150])
        self.setCentralWidget(centre)

        self.layer_panel = LayerPanel(self.state)
        self._dock("Layers", self.layer_panel, Qt.LeftDockWidgetArea)

        self.chain_panel = ChainPanel(self.state.chain)
        self.chain_panel.changed.connect(self._on_chain_changed)
        self._dock("Filter stack", self.chain_panel, Qt.LeftDockWidgetArea)

        self.basemap_panel = BasemapPanel(self.state)
        self.basemap_panel.fetch_btn.clicked.connect(self.fetch_all_imagery)
        self.basemap_panel.clear_btn.clicked.connect(self.state.clear_imagery)
        self.basemap_panel.solveOffsetRequested.connect(self.solve_imagery_offset)
        self._dock("Basemaps", self.basemap_panel, Qt.RightDockWidgetArea)

        self.plan_panel = PlanPanel(self.state)
        self.plan_panel.changed.connect(self._on_plan_changed)
        self.plan_panel.selected.connect(self._on_plan_selected)
        self.plan_panel.modeRequested.connect(self._set_plan_mode)
        self.plan_panel.btn_sheet.clicked.connect(self.write_field_sheet)
        self._dock("Shot plan", self.plan_panel, Qt.RightDockWidgetArea)

        self.plan_overlay = PlanOverlay(self.map2d.plot, self.state)
        self.plan_overlay.changed.connect(self._on_plan_changed)
        # Selection flows one way: the table decides, the map reflects. Wiring
        # the overlay's own selection back into the table closes a loop.
        self.plan_overlay.selected.connect(self._on_overlay_selected)

        self.vertical_panel = VerticalPanel(self.state)
        self.vertical_panel.solve_btn.clicked.connect(
            lambda: self.solve_vertical("local"))
        self.vertical_panel.clear_btn.clicked.connect(self.clear_vertical)
        self._dock("Vertical datum", self.vertical_panel, Qt.RightDockWidgetArea)

        self._menus()
        # Without an explicit starting size Qt distributes by size hint, which
        # hands the widest panel most of the window.
        if self._docks:
            self.resizeDocks(self._docks,
                             [DOCK_DEFAULT_WIDTH] * len(self._docks),
                             Qt.Horizontal)

        self.state.layersChanged.connect(self._on_layers_changed)
        self.state.resultChanged.connect(self._on_result_changed)
        self.state.selectionChanged.connect(self.map2d.refresh)
        self.state.verticalChanged.connect(self.vertical_panel.refresh)
        self.state.imageryChanged.connect(self.map2d.refresh)
        self.state.imageryChanged.connect(self.basemap_panel.refresh)
        self.state.statusMessage.connect(self.statusBar().showMessage)
        self.statusBar().showMessage("Ready. File ▸ Open export… to begin.")

    def _dock(self, title, widget, area):
        """Dock a panel so that it can always be resized.

        Every dock's minimum width propagates up from its contents, and a
        single non-wrapping label deep inside one is enough to make the whole
        main window unresizable - the sum of the minimums exceeded the screen,
        so Qt simply refused to move any splitter.

        Wrapping each panel in a scroll area caps that at the scroll area's own
        minimum (about 70 px), so a dock can always be dragged narrower and the
        content scrolls instead of jamming. The panels are also laid out to fit
        in DOCK_MIN_WIDTH, so the scrollbars are a fallback rather than the
        normal state.
        """
        scroll = QScrollArea()
        scroll.setWidget(widget)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setMinimumWidth(DOCK_MIN_WIDTH)

        d = QDockWidget(title, self)
        d.setWidget(scroll)
        d.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.addDockWidget(area, d)
        self._docks.append(d)
        return d

    def _menus(self) -> None:
        f = self.menuBar().addMenu("&File")
        a = QAction("&Open export…", self)
        a.setShortcut("Ctrl+O")
        a.setToolTip("Replace everything loaded with this export")
        a.triggered.connect(self.open_export)
        f.addAction(a)
        m = QAction("&Add export…", self)
        m.setShortcut("Ctrl+Shift+A")
        m.setToolTip("Merge another outing into what is already loaded")
        m.triggered.connect(self.add_export)
        f.addAction(m)
        f.addSeparator()
        po = QAction("Open &project…", self)
        po.setShortcut("Ctrl+Shift+O")
        po.triggered.connect(self.open_project)
        f.addAction(po)
        ps = QAction("&Save project", self)
        ps.setShortcut("Ctrl+S")
        ps.triggered.connect(self.save_project)
        f.addAction(ps)
        pa = QAction("Save project &as…", self)
        pa.setShortcut("Ctrl+Shift+S")
        pa.triggered.connect(lambda: self.save_project(ask=True))
        f.addAction(pa)
        f.addSeparator()
        q = QAction("E&xit", self)
        q.setShortcut("Ctrl+Q")
        q.triggered.connect(self.close)
        f.addAction(q)

        d = self.menuBar().addMenu("&Data")
        allb = QAction("Fetch &all basemaps", self)
        allb.setToolTip("Try every imagery and LiDAR source once, then toggle "
                        "them in the Basemaps panel")
        allb.triggered.connect(self.fetch_all_imagery)
        d.addAction(allb)
        d.addSeparator()
        img = d.addMenu("Fetch one &imagery source")
        for name in self.photo_providers:
            act = QAction(name, self)
            act.setToolTip(self.photo_providers[name].description)
            act.triggered.connect(lambda _=False, n=name: self.fetch_imagery(n))
            img.addAction(act)
        terr = d.addMenu("Fetch &LiDAR raster")
        for name in self.terrain_rasters:
            act = QAction(name, self)
            act.setToolTip(self.terrain_rasters[name].description)
            act.triggered.connect(lambda _=False, n=name: self.fetch_imagery(n))
            terr.addAction(act)
        local = QAction("Load imagery &file…", self)
        local.triggered.connect(self.load_imagery_file)
        d.addAction(local)
        clr = QAction("Clear imagery", self)
        clr.triggered.connect(self.state.clear_imagery)
        d.addAction(clr)
        d.addSeparator()
        ref = d.addMenu("Fetch &reference linework")
        for name in self.vector_providers:
            act = QAction(name, self)
            act.triggered.connect(lambda _=False, n=name: self.fetch_vectors(n))
            ref.addAction(act)
        clrv = QAction("Clear reference linework", self)
        clrv.triggered.connect(self.state.clear_vectors)
        d.addAction(clrv)
        d.addSeparator()
        chk = QAction("Check service availability…", self)
        chk.triggered.connect(self.check_services)
        d.addAction(chk)
        d.addSeparator()
        ses = QAction("&Sessions and overlap…", self)
        ses.setToolTip("Which outings are loaded, and whether their vertical "
                       "offsets can be solved")
        ses.triggered.connect(self.show_sessions)
        d.addAction(ses)

        v = self.menuBar().addMenu("Da&tum")
        tie = QAction("Datum &tie…", self)
        tie.setToolTip("Which shot the survey hangs from, and at what elevation")
        tie.triggered.connect(self.edit_datum_tie)
        v.addAction(tie)
        v.addSeparator()
        loc = QAction("Solve &local datum", self)
        loc.setToolTip("Tie the walked data to the laser spots")
        loc.triggered.connect(lambda: self.solve_vertical("local"))
        v.addAction(loc)
        nav = QAction("Solve &NAVD88 (orthometric)", self)
        nav.triggered.connect(lambda: self.solve_vertical("navd88"))
        v.addAction(nav)
        ses = QAction("Solve &session offsets only", self)
        ses.triggered.connect(lambda: self.solve_vertical("ellipsoidal"))
        v.addAction(ses)
        v.addSeparator()
        cl = QAction("&Clear vertical model", self)
        cl.triggered.connect(self.clear_vertical)
        v.addAction(cl)

        pm = self.menuBar().addMenu("&Plan")
        for label, mode in (("Add &shot", "add_point"),
                            ("Add &line", "add_line"),
                            ("Add la&ser setup", "add_setup"),
                            ("&Navigate", "navigate")):
            act = QAction(label, self)
            act.triggered.connect(lambda _=False, m=mode: self._set_plan_mode(m))
            pm.addAction(act)
        pm.addSeparator()
        fs = QAction("Print &field sheet…", self)
        fs.triggered.connect(self.write_field_sheet)
        pm.addAction(fs)
        pm.addSeparator()
        for label, slot in (("&Open plan…", self.open_plan),
                            ("Sa&ve plan…", self.save_plan)):
            act = QAction(label, self)
            act.triggered.connect(slot)
            pm.addAction(act)

        e = self.menuBar().addMenu("&Export")
        h = QAction("&Heightmap raster…", self)
        h.triggered.connect(self.export_heightmap)
        e.addAction(h)
        r = QAction("&Revit points file…", self)
        r.triggered.connect(self.export_revit)
        e.addAction(r)

        vm = self.menuBar().addMenu("&View")
        z = QAction("Reset &2D view", self)
        z.triggered.connect(self.map2d.reset_view)
        vm.addAction(z)
        z3 = QAction("Reset &3D view", self)
        z3.triggered.connect(self.view3d.reset_view)
        vm.addAction(z3)

    # --- helpers ---------------------------------------------------------

    @contextmanager
    def _busy(self, message: str):
        """Wait cursor and status message for the duration of a blocking call.

        A context manager rather than a busy/done pair, because Qt's override
        cursor is a STACK: every setOverrideCursor needs its own matching
        restoreOverrideCursor. A pair is easy to unbalance - an early return, a
        second busy() before the first done(), or an exception in between - and
        the result is an hourglass that never goes away. The `finally` here
        also means the cursor is restored before any error dialog appears,
        rather than the dialog inheriting the wait cursor.
        """
        self.statusBar().showMessage(message)
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            yield
        finally:
            QGuiApplication.restoreOverrideCursor()
            QApplication.processEvents()

    @staticmethod
    def _cursor_is_clear() -> bool:
        """True when no override cursor is left on the stack."""
        return QGuiApplication.overrideCursor() is None

    def _require_data(self) -> bool:
        if self.state.result is None:
            QMessageBox.information(self, "No data", "Load an export first.")
            return False
        return True

    # --- actions ---------------------------------------------------------

    def open_export(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open survey export", "", "Survey exports (*.zip *.swmz *.swm2 *.csv);;SW Maps project (*.swmz *.swm2);;SW Maps CSV export (*.zip *.csv);;All files (*)")
        if not path:
            return
        try:
            with self._busy("Reading export…"):
                report = self.state.load(path)
        except Exception as exc:                            # noqa: BLE001
            QMessageBox.critical(self, "Could not open", str(exc))
            return
        if report.reprojected:
            QMessageBox.information(
                self, "Coordinate system",
                "These layers were re-projected into the site's CRS "
                f"(EPSG:{self.state.site.epsg}):\n\n  "
                + "\n  ".join(report.reprojected))

    def add_export(self) -> None:
        """Merge another outing into what is already loaded."""
        if not self.state.layers:
            self.open_export()
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Add survey export", "", "Survey exports (*.zip *.swmz *.swm2 *.csv);;SW Maps project (*.swmz *.swm2);;SW Maps CSV export (*.zip *.csv);;All files (*)")
        if not path:
            return
        try:
            with self._busy("Merging export…"):
                report = self.state.add_export(path)
        except Exception as exc:                            # noqa: BLE001
            QMessageBox.critical(self, "Could not add", str(exc))
            return

        self.statusBar().showMessage(
            f"Merged {Path(path).name}: {report.added:,} points added")
        self._show_merge_report(report, title="Export merged")

    def show_sessions(self) -> None:
        if not self._require_data():
            return
        with self._busy("Measuring overlap…"):
            report = self.state.session_report()
        self._show_merge_report(report, title="Sessions and overlap",
                                offer_solve=False)

    def _show_merge_report(self, report, *, title: str,
                           offer_solve: bool = True) -> None:
        """Say what happened, and whether the result can be reconciled.

        A merge that cannot be reconciled is the one outcome worth a warning
        rather than a note: an unlinked session's offset is not recoverable
        later, so the only fix is to walk overlapping ground on the next
        outing, and that has to be known now.
        """
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setIcon(QMessageBox.Warning if not report.reconcilable
                    else QMessageBox.Information)
        box.setText(report.describe() or "Nothing loaded.")

        solve = None
        if offer_solve and report.reconcilable and len(report.sessions) > 1:
            box.setInformativeText(
                "The sessions overlap, so their vertical offsets can be "
                "solved. Until that is done the surface steps between them.")
            solve = box.addButton("Solve session offsets",
                                  QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Close)
        box.exec()
        if solve is not None and box.clickedButton() is solve:
            self.solve_vertical("ellipsoidal")

    def fetch_all_imagery(self) -> None:
        if not self._require_data():
            return
        try:
            with self._busy("Fetching basemaps…") :
                report = self.state.fetch_all_imagery(
                    progress=lambda n: self.statusBar().showMessage(
                        f"Fetching {n}…"))
        except Exception as exc:                            # noqa: BLE001
            QMessageBox.critical(self, "Fetch failed", str(exc))
            return

        self.statusBar().showMessage(
            f"{len(report.fetched)} basemaps available - toggle them in the "
            "Basemaps panel")
        QMessageBox.information(self, "Basemaps", report.describe())

    def fetch_imagery(self, name: str) -> None:
        if not self._require_data():
            return
        provider = self.imagery_providers[name]
        terrain = name in self.terrain_rasters
        with self._busy(f"Checking {name}…"):
            ok, msg = provider.available()
        if not ok:
            QMessageBox.warning(
                self, "Imagery unavailable",
                f"{name} is not reachable:\n\n{msg}\n\n"
                "Public GIS services go down; try another provider, or load a "
                "georeferenced file instead.")
            return
        try:
            with self._busy(f"Fetching imagery from {name}…"):
                layer = self.state.fetch_imagery(provider, terrain=terrain)
        except NoCoverageError as exc:
            QMessageBox.warning(self, "No coverage here", str(exc))
            return
        except Exception as exc:                            # noqa: BLE001
            QMessageBox.critical(self, "Imagery failed", str(exc))
            return
        self.statusBar().showMessage(layer.describe())

    def load_imagery_file(self) -> None:
        if not self._require_data():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open georeferenced image", "",
            "Rasters (*.tif *.tiff *.png *.jpg);;All files (*)")
        if not path:
            return
        try:
            with self._busy("Warping imagery…"):
                layer = self.state.load_imagery_file(path)
        except Exception as exc:                            # noqa: BLE001
            QMessageBox.critical(self, "Could not load imagery", str(exc))
            return
        self.statusBar().showMessage(layer.describe())

    # --- projects --------------------------------------------------------

    def _view_state(self) -> dict:
        v = {"colormap": self.map2d.cmap.currentText(),
             "color_by": self.map2d.color_by.currentText(),
             "show_imagery": self.map2d.show_imagery.isChecked(),
             "show_surface": self.map2d.show_surface.isChecked(),
             "show_points": self.map2d.show_points.isChecked(),
             "show_reference": self.map2d.show_reference.isChecked(),
             "surface_opacity": self.map2d.opacity.value(),
             "tab": self.tabs.currentIndex()}
        if self.view3d.enabled:
            v["vertical_exaggeration"] = self.view3d.ve.value()
        return v

    def _apply_view_state(self, v: dict) -> None:
        if not v:
            return
        self.map2d.cmap.setCurrentText(v.get("colormap", "terrain"))
        self.map2d.color_by.setCurrentText(v.get("color_by", "elevation"))
        for key, widget in (("show_imagery", self.map2d.show_imagery),
                            ("show_surface", self.map2d.show_surface),
                            ("show_points", self.map2d.show_points),
                            ("show_reference", self.map2d.show_reference)):
            if key in v:
                widget.setChecked(bool(v[key]))
        if "surface_opacity" in v:
            self.map2d.opacity.setValue(int(v["surface_opacity"]))
        if self.view3d.enabled and "vertical_exaggeration" in v:
            self.view3d.ve.setValue(int(v["vertical_exaggeration"]))
        if "tab" in v:
            self.tabs.setCurrentIndex(int(v["tab"]))

    def save_project(self, ask: bool = False) -> None:
        if self.state.source is None:
            QMessageBox.information(self, "Nothing to save", "Load data first.")
            return
        path = self.state.project_path
        if ask or path is None:
            suggested = str(path) if path else (
                str(Path(self.state.source_path or "project").with_suffix(
                    PROJECT_SUFFIX)))
            chosen, _ = QFileDialog.getSaveFileName(
                self, "Save project", suggested,
                f"Yard survey project (*{PROJECT_SUFFIX})")
            if not chosen:
                return
            path = chosen
        try:
            saved = self.state.save_project(path, self._view_state())
        except Exception as exc:                            # noqa: BLE001
            QMessageBox.critical(self, "Could not save", str(exc))
            return
        self.setWindowTitle(f"Yard Survey — {saved.name}")
        self.statusBar().showMessage(f"Saved {saved}")

    def open_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open project", "",
            f"Yard survey project (*{PROJECT_SUFFIX});;All files (*)")
        if not path:
            return
        try:
            with self._busy("Opening project…"):
                project, warnings = self.state.load_project(path)
        except Exception as exc:                            # noqa: BLE001
            QMessageBox.critical(self, "Could not open project", str(exc))
            return

        self._apply_view_state(project.view)
        self.setWindowTitle(f"Yard Survey — {Path(path).name}")
        self.statusBar().showMessage(f"Opened {path}")
        if warnings:
            QMessageBox.warning(
                self, "Project opened with warnings",
                "The project loaded, but some parts could not be restored:\n\n"
                + "\n".join(f"  {w}" for w in warnings)
                + "\n\nThe filter chain and vertical model are unaffected.")

    def fetch_vectors(self, name: str) -> None:
        if not self._require_data():
            return
        provider = self.vector_providers[name]
        try:
            with self._busy(f"Fetching {name}…"):
                layer = self.state.fetch_vectors(provider)
        except Exception as exc:                            # noqa: BLE001
            QMessageBox.critical(self, "Could not fetch linework", str(exc))
            return
        note = layer.describe()
        if not layer.survey_grade:
            note += (" - cartographic, typically 1-3 ft of positional "
                     "uncertainty. Not a surveyed boundary.")
        self.statusBar().showMessage(note)

    def check_services(self) -> None:
        lines: list[str] = []
        with self._busy("Checking services…"):
            lines.append("Imagery:")
            for name, p in self.imagery_providers.items():
                ok, msg = p.available()
                lines.append(f"  {'OK  ' if ok else 'DOWN'}  {name} — {msg}")
            lines.append("")
            lines.append("Reference linework:")
            for name, p in self.vector_providers.items():
                ok, msg = p.available()
                lines.append(f"  {'OK  ' if ok else 'DOWN'}  {name} — {msg}")
        self.statusBar().showMessage("Service check complete")
        QMessageBox.information(self, "Service availability", "\n".join(lines))

    def edit_datum_tie(self) -> None:
        from .datum_dialog import DatumTieDialog

        spots = self.state.spots
        ids: list[int] = []
        if spots is not None and "point_id" in spots.df.columns:
            ids = sorted(int(i) for i in spots.df["point_id"].dropna().unique())

        dialog = DatumTieDialog(self.state.site, ids, self)
        if dialog.exec() != dialog.Accepted:
            return
        try:
            dialog.apply_to_site()
        except ValueError as exc:
            QMessageBox.warning(self, "Datum tie", str(exc))
            return

        self.statusBar().showMessage(self.state.site.vertical.describe())
        if self.state.vertical is not None:
            # The benchmark changed, so the solved model is stale. Re-solving
            # is better than leaving a surface that claims a datum it no longer
            # sits on.
            self.solve_vertical(self.state.vertical.mode)
        else:
            self.vertical_panel.refresh()

    def solve_vertical(self, mode: str) -> None:
        if not self._require_data():
            return
        try:
            with self._busy(f"Solving vertical model ({mode})…"):
                model = self.state.solve_vertical(mode)
        except Exception as exc:                            # noqa: BLE001
            QMessageBox.critical(self, "Could not solve", str(exc))
            return
        self.statusBar().showMessage("Vertical model solved")

        warnings = list(model.notes)
        if model.sessions and model.sessions.unresolved:
            warnings.append(
                "Sessions with no overlapping ground cannot be tied. Re-cover "
                "some previously surveyed ground, or shoot a permanent "
                "benchmark, on the next outing.")
        if model.level is not None and not model.level.adjustment.has_redundancy:
            warnings.append(
                "The laser network has no redundancy, so a mis-read rod would "
                "be invisible. Shoot at least two points from each pair of "
                "setups next time.")
        QMessageBox.information(
            self, f"Vertical model ({mode})",
            model.describe() + ("\n\n" + "\n\n".join(warnings) if warnings else ""))

    def clear_vertical(self) -> None:
        self.state.clear_vertical()
        self.statusBar().showMessage(
            "Vertical model cleared; elevations are raw ellipsoidal height.")

    def export_heightmap(self) -> None:
        if not self._require_data():
            return
        outdir = QFileDialog.getExistingDirectory(self, "Choose output folder")
        if not outdir:
            return
        from ..io.raster import write_heightmap

        try:
            with self._busy("Building full-resolution surface…"):
                surf = self.state.export_surface()
                res = (write_heightmap(surf, outdir, tag="_fixed")
                       if surf is not None else None)
        except Exception as exc:                            # noqa: BLE001
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        if surf is None:
            QMessageBox.warning(self, "Export failed", "Too few points to grid.")
            return
        self.statusBar().showMessage(f"Wrote {res['paths']['png16'].name}")
        QMessageBox.information(
            self, "Heightmap written",
            f"{Path(outdir).name}\n\n{surf.describe()}\n\n"
            f"Heights from: {surf.z_column}\n\n"
            "The 16-bit raster contains interpolated fill in unmeasured cells; "
            "the true mask is in the .npy alongside it.")

    def export_revit(self) -> None:
        if not self._require_data():
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Write Revit points file", "revit_points_ft.csv", "CSV (*.csv)")
        if not path:
            return
        from ..filters import BinToCell
        from ..io.revit import write_points

        binned = BinToCell(cell=self.state.site.surface.bin_cell_m).for_site(
            self.state.site).apply(self.state.result)
        res = write_points(binned, self.state.site, path)

        notes = []
        if res["z_column"] != ELEV:
            notes.append(
                "WARNING: no vertical model applied. These are raw ellipsoidal "
                "antenna heights (~860 ft), not elevations on the site datum. "
                "Solve the local datum before importing into Revit.")
        elif not self.state.site.vertical.tied_to_model:
            notes.append(
                "NOTE: the datum is local and arbitrary. The elevations are "
                "self-consistent but sit nowhere in particular, so the "
                "toposolid will not land at the right height in the model. "
                "Datum ▸ Datum tie… once you have shot a known feature.")
        if res["over_threshold"]:
            notes.append(
                "This many points will make Revit's importer very slow. "
                "Consider a larger bin cell.")
        QMessageBox.information(
            self, "Revit points written",
            f"{res['n_points']:,} points\n"
            f"Heights from: {res['z_column']}\n"
            f"Origin sidecar: {res['sidecar'].name}"
            + ("\n\n" + "\n\n".join(notes) if notes else ""))

    # --- shot plan -------------------------------------------------------

    def _set_plan_mode(self, mode: str) -> None:
        self.plan_overlay.set_mode(mode)
        self.plan_panel.set_active_tool(mode)
        hints = {
            "add_point": "Click on the map to place a shot.",
            "add_line": "Click each vertex; double-click to finish the line.",
            "add_setup": "Click where the laser will stand.",
            "navigate": "Drag a numbered marker to move it.",
        }
        self.statusBar().showMessage(hints.get(mode, ""))

    def _on_plan_changed(self) -> None:
        self.plan_overlay.refresh()
        self.plan_panel.refresh()
        self.state.refresh_plan_layer()
        # Rod readings feed the level network, so the datum may have moved.
        if self.state.vertical is not None:
            try:
                self.state.solve_vertical(self.state.vertical.mode)
            except Exception as exc:                        # noqa: BLE001
                self.statusBar().showMessage(f"Could not re-solve: {exc}")

    def _on_plan_selected(self, number) -> None:
        """Table selection changed: highlight it on the map."""
        self.plan_overlay.select(number)

    def _on_overlay_selected(self, number) -> None:
        """A point placed on the map should become the table's current row."""
        if number is None:
            return
        table = self.plan_panel.table
        for row in range(table.rowCount()):
            if self.plan_panel._row_number(row) == number:
                table.selectRow(row)
                table.scrollToItem(table.item(row, 0))
                break

    def solve_imagery_offset(self) -> None:
        """Fit the basemap shift from plan points.

        Selected rows win when there are any: selecting them is the user
        saying "these ones", which is more informative than the purpose
        column. With nothing selected the solver falls back to every
        photo-identifiable feature that has been measured.
        """
        from ..georef import residual_report

        numbers = self.plan_panel.selected_numbers() or None
        try:
            offset = self.state.solve_imagery_offset(numbers)
        except ValueError as exc:
            QMessageBox.information(self, "Align imagery", str(exc))
            return

        self.basemap_panel.sync_offset()
        report = residual_report(offset)
        QMessageBox.information(
            self, "Align imagery",
            offset.describe()
            + ("\n\nPoints used, worst residual first:\n  "
               + "\n  ".join(report) if len(report) > 1 else "")
            + "\n\nThe imagery moved; the survey points did not.")
        self.statusBar().showMessage(offset.describe())

    def write_field_sheet(self) -> None:
        from ..io.fieldsheet import write_field_sheet

        if not self.state.plan.points:
            QMessageBox.information(self, "Field sheet",
                                    "Place some shots first.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Write field sheet", "field_sheet.html", "HTML (*.html)")
        if not path:
            return
        basemap = next((b for b in self.state.visible_basemaps), None)
        try:
            with self._busy("Rendering field sheet…"):
                out = write_field_sheet(
                    self.state.plan, path, basemap=basemap,
                    site=self.state.site,
                    title=f"{self.state.site.name} — shot plan",
                    imagery_offset=(self.state.imagery_offset.de,
                                    self.state.imagery_offset.dn))
        except Exception as exc:                            # noqa: BLE001
            QMessageBox.critical(self, "Field sheet", str(exc))
            return
        QMessageBox.information(
            self, "Field sheet",
            f"Wrote {out.name}.\n\n"
            "Open it in a browser and print. It is self-contained, so it "
            "needs no network in the field."
            + ("" if basemap else
               "\n\nNo basemap was visible, so the map is blank. Fetch "
               "imagery first if you want to find the points on the ground."))

    def save_plan(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save shot plan", "shot_plan.yardplan",
            "Shot plan (*.yardplan)")
        if not path:
            return
        self.state.plan.epsg = self.state.site.epsg
        saved = self.state.plan.save(path)
        self.statusBar().showMessage(f"Saved {saved.name}")

    def open_plan(self) -> None:
        from ..plan import Plan

        path, _ = QFileDialog.getOpenFileName(
            self, "Open shot plan", "", "Shot plan (*.yardplan);;All files (*)")
        if not path:
            return
        try:
            plan = Plan.load(path)
        except Exception as exc:                            # noqa: BLE001
            QMessageBox.critical(self, "Could not open plan", str(exc))
            return
        if plan.epsg and plan.epsg != self.state.site.epsg:
            QMessageBox.warning(
                self, "Different CRS",
                f"That plan was made in EPSG:{plan.epsg} but this site is "
                f"EPSG:{self.state.site.epsg}. The positions will be wrong.")
            return
        self.state.plan = plan
        self.plan_overlay.clear()
        self._on_plan_changed()
        self.statusBar().showMessage(f"Opened {Path(path).name}")

    # --- state changes ---------------------------------------------------

    def _on_chain_changed(self) -> None:
        self.state.recompute()

    def _on_layers_changed(self) -> None:
        self.layer_panel.rebuild()

    def _on_result_changed(self) -> None:
        self.chain_panel.show_results(self.state.chain)
        self.vertical_panel.refresh()
        self.map2d.refresh()
        self.view3d.refresh()
        self._update_qc()
        if self.state.surface is not None:
            self.map2d.reset_view()
            self.view3d.reset_view()

    def _update_qc(self) -> None:
        ps = self.state.result
        if ps is None or len(ps) < 2:
            self.qc_label.setText("")
            return

        column = ELEV if ps.has(ELEV) else None
        stats = (qc.crossover_stats(ps, column=column) if column
                 else qc.crossover_stats(ps))
        lines = [
            "Measured QC — crossover residuals (pairs within 30 cm, >60 s apart)",
            "  " + qc.format_stats(stats),
        ]
        if stats.get("n"):
            lines.append(f"  systematic bias {stats['bias'] * 100:+.2f} cm")
        lines.append("")

        s = self.state.surface
        if s is not None:
            v = self.state.site.vertical
            if s.z_column != ELEV:
                datum = "RAW ELLIPSOIDAL (no vertical model)"
            elif v.tied_to_model:
                datum = f"tied to {v.model_frame}"
            else:
                datum = "local datum, ARBITRARY origin"
            # nan-aware: masked cells are NaN and would poison plain min/max.
            lo = float(np.nanmin(s.z_masked))
            hi = float(np.nanmax(s.z_masked))
            lines.append(
                f"Surface   {s.n_points:,} pts → {s.n_cells:,} cells → "
                f"{s.z.shape[0]}² preview   GSD {s.px * 100:.1f} cm/px   "
                f"{s.measured_fraction * 100:.1f}% measured, "
                f"{(1 - s.measured_fraction) * 100:.1f}% interpolated fill")
            lines.append(
                f"Heights   {m_to_ft(lo):.2f} – {m_to_ft(hi):.2f} ft   "
                f"relief {m_to_ft(hi - lo) * 12:.1f} in   [{datum}]")
        self.qc_label.setText("\n".join(lines))

    def keyPressEvent(self, event):
        """Escape puts the current drawing tool down.

        Without it the only way out of a placing mode is to find the Navigate
        button, which is the wrong reflex when you have just misclicked.
        """
        if event.key() == Qt.Key_Escape:
            if self.plan_overlay.mode != "navigate":
                self.plan_overlay.cancel_line()
                self._set_plan_mode("navigate")
                event.accept()
                return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        self.view3d.close_plotter()
        super().closeEvent(event)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    app = QApplication(argv)
    app.setApplicationName("Yard Survey")

    win = MainWindow()
    win.show()

    args = [a for a in argv[1:] if not a.startswith("-")]
    if args:
        try:
            win.state.load(args[0])
        except Exception as exc:                            # noqa: BLE001
            QMessageBox.critical(win, "Could not open", str(exc))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
