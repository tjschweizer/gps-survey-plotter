"""Window and dock sizing.

Qt propagates minimum widths upward: a single non-wrapping label buried in a
panel becomes that panel's minimum, then that dock's, then the window's. Once
the sum of those minimums exceeds the screen, Qt refuses to move ANY splitter
and every panel becomes unresizable at once - with no error, and no obvious
culprit.

That happened: the window's minimum reached 3197 px, wider than a 1920 px
display. These tests pin the ceiling so it cannot creep back.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt                                # noqa: E402
from PySide6.QtWidgets import (QAbstractScrollArea, QApplication,  # noqa: E402
                               QDockWidget)

# A 1366 px laptop is the smallest thing this needs to run on, and the window
# must fit with room to actually move the splitters.
MAX_WINDOW_MIN_WIDTH = 1360


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, export, tmp_path):
    from gpsrtk.ui.main import MainWindow

    w = MainWindow()
    w.state.cache_dir = tmp_path / "cache"
    w.state.load(str(export.source_path))
    # Populate the panels: empty ones are trivially narrow and prove nothing.
    w.state.plan.add_point(449712.0, 4604565.0)
    w.state.plan.add_line("swale-1", [(449708.0, 4604561.0),
                                      (449712.0, 4604562.5)])
    w._on_plan_changed()
    w.resize(1900, 1100)
    w.show()
    app.processEvents()
    yield w
    w.view3d.close_plotter()


def test_window_fits_on_a_laptop_screen(win):
    """The failure this guards against is total: no panel can be resized."""
    width = win.minimumSizeHint().width()
    assert width <= MAX_WINDOW_MIN_WIDTH, (
        f"window minimum is {width} px. Something inside a panel is refusing "
        "to shrink - usually a QLabel without setWordWrap(True), or a row of "
        "widgets laid out horizontally that should be a grid.")


def test_every_dock_can_be_dragged_narrow(win):
    from gpsrtk.ui.main import DOCK_MIN_WIDTH

    for dock in win.findChildren(QDockWidget):
        assert dock.minimumWidth() <= DOCK_MIN_WIDTH + 2, (
            f"{dock.windowTitle()} cannot be narrowed below "
            f"{dock.minimumWidth()} px")


def test_dock_contents_scroll_rather_than_jam(win):
    """Each dock wraps its panel in a scroll area that may scroll horizontally.
    That is what caps the dock's minimum regardless of what is inside."""
    for dock in win.findChildren(QDockWidget):
        inner = dock.widget()
        assert isinstance(inner, QAbstractScrollArea), (
            f"{dock.windowTitle()} is not wrapped in a scroll area")
        assert inner.horizontalScrollBarPolicy() != Qt.ScrollBarAlwaysOff, (
            f"{dock.windowTitle()} cannot scroll horizontally, so its content "
            "sets a hard floor on its width")


def test_the_central_view_can_shrink(win):
    """If the map refuses to narrow, the docks have nowhere to expand into -
    which is the half of the bug that made the right panels unwidenable."""
    assert win.centralWidget().minimumSizeHint().width() <= 950


def test_docks_start_at_a_usable_width(win):
    """Qt distributes by size hint otherwise, handing the widest panel most of
    the window and leaving the rest slivers."""
    from gpsrtk.ui.main import DOCK_DEFAULT_WIDTH

    for dock in win.findChildren(QDockWidget):
        assert dock.width() >= DOCK_MIN_WIDTH_FLOOR, dock.windowTitle()
        assert dock.width() <= DOCK_DEFAULT_WIDTH * 2.5, dock.windowTitle()


DOCK_MIN_WIDTH_FLOOR = 120


def test_resizing_a_dock_actually_takes_effect(win, app):
    """The end-to-end symptom: dragging did nothing at all."""
    dock = win.findChildren(QDockWidget)[0]
    win.resizeDocks([dock], [260], Qt.Horizontal)
    app.processEvents()
    narrow = dock.width()

    win.resizeDocks([dock], [460], Qt.Horizontal)
    app.processEvents()
    wide = dock.width()

    assert wide > narrow, (
        f"dock width did not change: {narrow} -> {wide}. The layout is "
        "over-constrained somewhere.")
