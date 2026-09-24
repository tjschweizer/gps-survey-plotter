"""The wait cursor must always come back.

Qt's override cursor is a stack: every setOverrideCursor needs a matching
restoreOverrideCursor. An unbalanced pair leaves the hourglass on screen
permanently, and the app looks hung when it is idle. This happened for real -
fetch_imagery pushed twice (health check, then fetch) and popped once.

These tests drive the real menu actions with the network stubbed out, then
assert the cursor stack is empty. They run offscreen, so no window appears.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtGui import QGuiApplication          # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from gpsrtk.io.imagery import ArcGISImageryProvider, NoCoverageError  # noqa: E402
from gpsrtk.surface import Extent                  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, export, monkeypatch, tmp_path):
    """A main window with data loaded and every dialog silenced."""
    from gpsrtk.ui.main import MainWindow

    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name,
                            staticmethod(lambda *a, **k: QMessageBox.Ok))

    win = MainWindow()
    # Never write into the project's real cache directory from a test, and
    # never let one test's cached tile satisfy another test's fetch.
    win.state.cache_dir = tmp_path
    win.state.load(str(export.source_path))
    # Drain anything left over from construction before each case.
    while QGuiApplication.overrideCursor() is not None:
        QGuiApplication.restoreOverrideCursor()
    yield win
    win.view3d.close_plotter()


class _Provider(ArcGISImageryProvider):
    """Stands in for a web service, with controllable behaviour."""

    def __init__(self, *, reachable=True, outcome="ok"):
        super().__init__("stub", "https://example.invalid/ImageServer")
        self._reachable = reachable
        self._outcome = outcome

    def available(self):
        return (True, "ok") if self._reachable else (False, "unreachable")

    def fetch(self, extent, epsg, size=1024):
        import numpy as np

        from gpsrtk.io.imagery import RasterLayer

        if self._outcome == "blank":
            raise NoCoverageError("no coverage for this extent")
        if self._outcome == "boom":
            raise RuntimeError("service exploded")
        rng = np.random.default_rng(0)
        return RasterLayer(
            image=rng.integers(0, 255, (32, 32, 3), dtype=np.uint8),
            extent=extent, epsg=epsg, source="stub")


@pytest.mark.parametrize("outcome", ["ok", "blank", "boom"])
def test_cursor_is_restored_after_fetching_imagery(window, outcome):
    """The original bug: two _busy pushes, one pop, on the success path."""
    window.imagery_providers["stub"] = _Provider(outcome=outcome)
    window.fetch_imagery("stub")
    assert window._cursor_is_clear(), (
        f"wait cursor left on screen after a '{outcome}' imagery fetch")


def test_cursor_is_restored_when_the_provider_is_unreachable(window):
    """The early-return path skipped the pop entirely."""
    window.imagery_providers["stub"] = _Provider(reachable=False)
    window.fetch_imagery("stub")
    assert window._cursor_is_clear()


def test_a_successful_fetch_actually_stores_the_layer(window):
    """Guard against the cursor test passing because nothing happened."""
    window.imagery_providers["stub"] = _Provider(outcome="ok")
    window.fetch_imagery("stub")
    assert "stub" in window.state.basemaps
    assert window.state.basemaps["stub"].visible


def test_cursor_is_restored_after_fetching_linework(window):
    class VProvider:
        name = "stubvec"
        survey_grade = False

        def available(self):
            return True, "ok"

        def fetch(self, extent, epsg):
            from gpsrtk.io.vector import VectorLayer
            import numpy as np
            return VectorLayer(rings=[np.zeros((4, 2))], epsg=epsg,
                               source="stubvec")

    window.vector_providers["stubvec"] = VProvider()
    window.fetch_vectors("stubvec")
    assert window._cursor_is_clear()


def test_cursor_is_restored_after_solving_the_datum(window):
    window.solve_vertical("local")
    assert window._cursor_is_clear()
    assert window.state.vertical is not None


def test_cursor_is_restored_when_solving_fails(window, monkeypatch):
    monkeypatch.setattr(window.state, "solve_vertical",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("nope")))
    window.solve_vertical("local")
    assert window._cursor_is_clear()


def test_nested_busy_blocks_stay_balanced(window):
    """Belt and braces: nesting must still unwind completely."""
    with window._busy("outer"):
        with window._busy("inner"):
            assert not window._cursor_is_clear()
        assert not window._cursor_is_clear()
    assert window._cursor_is_clear()
