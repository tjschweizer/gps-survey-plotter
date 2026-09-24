"""2D plan view.

Draw order, bottom to top: aerial imagery, the derived surface, reference
linework, points, spot markers.

Two deliberate choices about honesty:

  Unmeasured cells are fully transparent, not filled with a plausible colour.
  A hillshade of interpolated fill is indistinguishable from real ground at a
  glance, and the point of the mask is that it is not ground we measured. With
  imagery underneath, the holes now show the actual aerial photo, which reads
  correctly as "we did not survey this".

  Reference linework is drawn dashed and labelled REFERENCE ONLY, because
  parcel polygons are cartographic to a foot or three and must never be
  mistaken for a surveyed boundary.

Points are subsampled above a cap. A few mowing sessions produce far more
points than a scatter plot can draw interactively, and drawing every one of
them communicates nothing that 50k does not.
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel,
                               QSizePolicy, QSlider, QVBoxLayout, QWidget)

from ..model.pointset import E, N, Z, ELEV, FIX, SPEED, SESSION
from ..units import m_to_ft
from .palette import COLORMAPS, colorize

MAX_SCATTER = 50_000

COLOR_BY = {
    "elevation": ELEV,
    "fix quality": FIX,
    "speed": SPEED,
    "session": SESSION,
}


class Map2D(QWidget):
    """Plan view of the surface and the points that produced it."""

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self._subsampled = False
        self._vector_items: list[pg.PlotCurveItem] = []

        pg.setConfigOptions(antialias=True, imageAxisOrder="row-major",
                            background="#f7f7f7", foreground="#303030")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        bar = QHBoxLayout()
        bar.setContentsMargins(6, 4, 6, 0)

        self.show_imagery = QCheckBox("imagery")
        self.show_imagery.setChecked(True)
        self.show_imagery.toggled.connect(self.refresh)
        bar.addWidget(self.show_imagery)

        self.show_surface = QCheckBox("surface")
        self.show_surface.setChecked(True)
        self.show_surface.toggled.connect(self.refresh)
        bar.addWidget(self.show_surface)

        self.opacity = QSlider(Qt.Horizontal)
        self.opacity.setRange(0, 100)
        self.opacity.setValue(85)
        self.opacity.setMaximumWidth(90)
        self.opacity.setToolTip("surface opacity over the imagery")
        self.opacity.valueChanged.connect(self._apply_opacity)
        bar.addWidget(self.opacity)

        self.show_points = QCheckBox("points")
        self.show_points.setChecked(True)
        self.show_points.toggled.connect(self.refresh)
        bar.addWidget(self.show_points)

        self.show_reference = QCheckBox("reference")
        self.show_reference.setChecked(True)
        self.show_reference.toggled.connect(self.refresh)
        bar.addWidget(self.show_reference)

        bar.addSpacing(8)
        bar.addWidget(QLabel("colour"))
        self.color_by = QComboBox()
        self.color_by.addItems(COLOR_BY)
        # Combos otherwise size to their longest item ("fix quality",
        # "Spectral_r"), which the toolbar then imposes on the whole window.
        self.color_by.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.color_by.setMinimumContentsLength(6)
        self.color_by.currentTextChanged.connect(self.refresh)
        bar.addWidget(self.color_by)

        bar.addSpacing(8)
        bar.addWidget(QLabel("map"))
        self.cmap = QComboBox()
        self.cmap.addItems(COLORMAPS)
        self.cmap.setCurrentText("terrain")
        self.cmap.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cmap.setMinimumContentsLength(6)
        self.cmap.currentTextChanged.connect(self.refresh)
        bar.addWidget(self.cmap)

        bar.addStretch(1)
        self.info = QLabel("")
        self.info.setStyleSheet("color: palette(mid);")
        # Ignored horizontal policy: this line carries attribution text long
        # enough to set a 1381 px minimum on the whole central widget, which
        # left no slack for the docks to be resized at all.
        self.info.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        # Ignored policy alone is not enough: Qt still honours minimumSizeHint
        # unless an explicit minimum is set, and this label's text is long.
        self.info.setMinimumWidth(0)
        self.info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bar.addWidget(self.info, 1)
        root.addLayout(bar)

        self.plot = pg.PlotWidget()
        self.plot.setMinimumSize(120, 120)
        self.plot.setAspectLocked(True)
        # Plot in metres from the site's local origin, not raw UTM. Raw
        # northings are ~4.65 million, which pyqtgraph helpfully renders as
        # "4.65 Mm" - technically true and completely unreadable. Local metres
        # also match the frame the Revit export uses.
        self.plot.setLabel("bottom", "east of local origin (m)")
        self.plot.setLabel("left", "north of local origin (m)")
        for ax in ("bottom", "left"):
            self.plot.getAxis(ax).enableAutoSIPrefix(False)
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        root.addWidget(self.plot, 1)

        # One ImageItem per fetched basemap, created on demand. They stack in
        # the order the providers are declared, which puts photographs
        # underneath LiDAR products - the useful direction, since a hillshade
        # at partial opacity over an aerial reads well and the reverse does not.
        self._basemap_items: dict[str, pg.ImageItem] = {}

        self.image = pg.ImageItem()
        self.image.setZValue(-10)
        self.plot.addItem(self.image)

        self.scatter = pg.ScatterPlotItem(pen=None, size=3)
        self.plot.addItem(self.scatter)

        self.markers = pg.ScatterPlotItem(
            pen=pg.mkPen("k", width=1), brush=pg.mkBrush(255, 60, 60, 220),
            size=11, symbol="s")
        self.markers.setZValue(10)
        self.plot.addItem(self.markers)

        self._apply_opacity()

    # --- drawing ---------------------------------------------------------

    def _local(self, e, n):
        """Projected coordinates to metres from the site's local origin."""
        return self.state.site.to_local(e, n)

    def _apply_opacity(self) -> None:
        self.image.setOpacity(self.opacity.value() / 100.0)

    def refresh(self) -> None:
        self._draw_imagery()
        self._draw_surface()
        self._draw_reference()
        self._draw_points()
        self._draw_other_layers()

    def _draw_imagery(self) -> None:
        on = self.show_imagery.isChecked()

        # Drop items for basemaps that have gone away.
        for name in list(self._basemap_items):
            if name not in self.state.basemaps:
                self.plot.removeItem(self._basemap_items.pop(name))

        for depth, (name, bm) in enumerate(self.state.basemaps.items()):
            item = self._basemap_items.get(name)
            if item is None:
                item = pg.ImageItem()
                self._basemap_items[name] = item
                self.plot.addItem(item)

            if not (on and bm.visible):
                item.setVisible(False)
                continue

            # Imagery arrives north-up (row 0 = north); the plot's y axis
            # increases upward, so it has to be flipped to sit the right way
            # round.
            item.setImage(np.flipud(bm.layer.image), autoLevels=False)
            e = bm.layer.extent
            x0, y0 = self._local(e.xmin, e.ymin)
            # The alignment offset moves the PHOTO, never the measurements.
            # The points are the better-known thing; shifting them to agree
            # with an aerial would be backwards.
            off = self.state.imagery_offset
            item.setRect(pg.QtCore.QRectF(x0 + off.de, y0 + off.dn,
                                          e.width, e.height))
            item.setOpacity(bm.opacity)
            item.setZValue(-100 + depth)
            item.setVisible(True)

    def _draw_surface(self) -> None:
        s = self.state.surface
        if s is None or not self.show_surface.isChecked():
            self.image.clear()
            return

        rgba = colorize(s.z_masked, self.cmap.currentText(), mask=s.mask,
                        hillshade=True, px=s.px)
        self.image.setImage(rgba, autoLevels=False)
        e = s.extent
        x0, y0 = self._local(e.xmin, e.ymin)
        self.image.setRect(pg.QtCore.QRectF(x0, y0, e.width, e.height))

    def _draw_reference(self) -> None:
        for item in self._vector_items:
            self.plot.removeItem(item)
        self._vector_items.clear()
        if not self.show_reference.isChecked():
            return

        pen = pg.mkPen((200, 40, 160), width=1.6, style=Qt.DashLine)
        for layer in self.state.vectors.values():
            for ring in layer.rings:
                if len(ring) < 2:
                    continue
                x, y = self._local(ring[:, 0], ring[:, 1])
                item = pg.PlotCurveItem(x, y, pen=pen)
                item.setZValue(5)
                self.plot.addItem(item)
                self._vector_items.append(item)

    def _draw_points(self) -> None:
        ps = self.state.result
        if ps is None or len(ps) == 0 or not self.show_points.isChecked():
            self.scatter.clear()
            self.info.setText(self._info_suffix())
            self.info.setToolTip(self.info.text())
            return

        d = ps.df
        n = len(d)
        if n > MAX_SCATTER:
            idx = np.random.default_rng(0).choice(n, MAX_SCATTER, replace=False)
            d = d.iloc[np.sort(idx)]
            self._subsampled = True
        else:
            self._subsampled = False

        column = COLOR_BY[self.color_by.currentText()]
        if column == ELEV and ELEV not in d.columns:
            column = Z                       # no vertical model applied yet
        brushes = self._brushes(d, column)
        x, y = self._local(d[E].to_numpy(), d[N].to_numpy())
        # Small and semi-transparent: with the surface drawn underneath, the
        # points are there to show coverage and quality, not to restate the
        # elevation the surface already carries.
        self.scatter.setData(x, y, brush=brushes, pen=None, size=2.5)

        note = f"{n:,} points"
        if self._subsampled:
            note += f" (showing {MAX_SCATTER:,})"
        text = note + self._info_suffix()
        self.info.setText(text)
        self.info.setToolTip(text)      # the label elides; the tooltip does not

    def _info_suffix(self) -> str:
        bits = []
        if self.state.surface is not None:
            bits.append(f"{self.state.surface.measured_fraction * 100:.0f}% measured")
        credits = {b.layer.attribution for b in self.state.visible_basemaps
                   if b.layer.attribution}
        bits.extend(sorted(credits))
        if any(not v.survey_grade for v in self.state.vectors.values()):
            bits.append("linework REFERENCE ONLY")
        off = self.state.imagery_offset
        if not off.zero:
            # Say so on the view itself. A silently shifted basemap is the
            # kind of thing that gets forgotten and then trusted.
            bits.append(f"imagery shifted {m_to_ft(off.magnitude_m):.2f} ft")
        return ("  ·  " + "  ·  ".join(bits)) if bits else ""

    def _brushes(self, d, column):
        if column not in d.columns:
            return pg.mkBrush(200, 200, 200, 180)

        v = d[column]
        if column == FIX:
            # Categorical and meaningful: green fixed, orange float, grey other.
            lut = {4: (60, 200, 90), 5: (240, 150, 40)}
            return [pg.mkBrush(*lut.get(int(x) if x == x else -1,
                                        (150, 150, 150)), 200) for x in v]
        if column == SESSION:
            names = sorted(map(str, v.dropna().unique()))
            idx = {nm: i for i, nm in enumerate(names)}
            cm = pg.colormap.get("tab10", source="matplotlib")
            cols = cm.getLookupTable(nPts=max(len(names), 2), alpha=False)
            return [pg.mkBrush(*cols[idx.get(str(x), 0) % len(cols)], 200)
                    for x in v]

        a = v.to_numpy(dtype=float)
        finite = a[np.isfinite(a)]
        if finite.size == 0:
            return pg.mkBrush(200, 200, 200, 180)
        lo, hi = np.percentile(finite, [2, 98])
        norm = np.clip((a - lo) / (hi - lo) if hi > lo else np.zeros_like(a), 0, 1)
        cm = pg.colormap.get(self.cmap.currentText(), source="matplotlib")
        lut = cm.getLookupTable(nPts=256, alpha=False)
        return [pg.mkBrush(*lut[int(x * 255)], 170) for x in norm]

    def _draw_other_layers(self) -> None:
        """Spot layers drawn as distinct markers, not part of the surface."""
        seen: set[tuple[float, float]] = set()
        xs, ys = [], []
        for name, ps in self.state.layers.items():
            if name == self.state.active_layer or not self.state.visible.get(name):
                continue
            if len(ps) == 0 or len(ps) > 500:      # a spot layer, not a track
                continue
            for e, n in zip(ps.df[E].to_numpy(), ps.df[N].to_numpy()):
                # FEATURE_POINTS repeats every spot layer's rows, so the same
                # shot would otherwise be drawn twice.
                key = (round(e, 3), round(n, 3))
                if key in seen:
                    continue
                seen.add(key)
                xs.append(e)
                ys.append(n)
        if xs:
            lx, ly = self._local(np.array(xs), np.array(ys))
            self.markers.setData(lx, ly)
        else:
            self.markers.clear()

    def reset_view(self) -> None:
        """Frame the surveyed ground, not everything on the canvas.

        Reference linework returns whole parcels that merely intersect the
        request box, so some of them run hundreds of metres off-site. Letting
        autoRange include those shrinks the lot to a thumbnail.
        """
        ext = self.state.data_extent(margin=8.0)
        if ext is None:
            self.plot.autoRange()
            return
        x0, y0 = self._local(ext.xmin, ext.ymin)
        x1, y1 = self._local(ext.xmax, ext.ymax)
        self.plot.setRange(xRange=(x0, x1), yRange=(y0, y1), padding=0.0)
