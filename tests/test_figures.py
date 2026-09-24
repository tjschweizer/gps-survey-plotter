"""The printed slope and contour maps.

Pixel-exact comparisons of figures are brittle across matplotlib versions, so
these check what would make a figure wrong at a glance instead: that it
renders, that the height colours span the scale rather than collapsing to one
end of it (which a units slip between the colour scale and the data does,
silently), and that the drainage arrows are drawn.
"""

import io

import numpy as np
import pytest
from PIL import Image

from gpsrtk.io import figures


def _pixels(png: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(png)).convert("RGB")).astype(int)


@pytest.fixture
def figure_surface(state):
    return state.figure_surface()


def test_figures_are_gridded_by_ground_resolution(state, figure_surface):
    from gpsrtk import terrain

    dx, _ = terrain.node_spacing(figure_surface)
    assert 0.05 < dx < 0.12, "about 8 cm, whatever the lot's size"


def test_the_slope_map_renders_with_its_arrows(state, figure_surface):
    img = _pixels(figures.slope_map(figure_surface, state.site, note="test"))
    assert img.shape[1] > 900 and img.shape[0] > 600
    cyan = (np.abs(img - np.array([31, 180, 200])).sum(axis=2) < 60).sum()
    assert cyan > 2000, "the drainage arrows are missing"


def test_the_heightmap_uses_the_whole_colour_scale(state, figure_surface):
    """The failure this guards against drew the whole lot in the bottom
    colour of the scale, because the heights and the scale were in
    different units. Both ends of turbo must appear."""
    img = _pixels(figures.heightmap(figure_surface, state.site, interval_m=0.05))
    warm = ((img[..., 0] > 180) & (img[..., 2] < 90)).sum()
    cool = ((img[..., 2] > 180) & (img[..., 0] < 90)).sum()
    assert warm > 5000 and cool > 5000


def test_a_steeper_slope_scale_draws_the_same_ground_paler(state, figure_surface):
    """The scale is the reader's to choose; the ground does not change."""
    def darkness(slope_max):
        img = _pixels(figures.slope_map(figure_surface, state.site,
                                        slope_max_pct=slope_max))
        return (img.sum(axis=2) < 300).sum()

    assert darkness(20.0) < darkness(4.0)
