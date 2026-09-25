"""Imagery and reference linework.

Network-dependent tests are marked and skipped by default, because a public GIS
service being down is not a defect in this code - it is the condition the
design already assumes. Run them with `-m network` when you want to know
whether the outside world is still there.
"""

import numpy as np
import pytest

from gpsrtk.io.imagery import (ArcGISImageryProvider, LocalRasterProvider,
                               RasterLayer, cache_key, default_providers,
                               fetch_cached)
from gpsrtk.io.vector import VectorLayer, default_vector_providers
from gpsrtk.surface import Extent

EXT = Extent(449676.7, 449762.5, 4604529.5, 4604615.3)


# --- offline behaviour ---------------------------------------------------

def test_provider_health_check_never_raises():
    """A dead service must report, not explode. The UI depends on this."""
    p = ArcGISImageryProvider("nowhere",
                              "https://invalid.example.invalid/arcgis/x/ImageServer")
    ok, msg = p.available()
    assert ok is False
    assert isinstance(msg, str) and msg


def test_missing_local_file_reports_cleanly(tmp_path):
    p = LocalRasterProvider(tmp_path / "nope.tif")
    ok, msg = p.available()
    assert not ok and "not found" in msg


def test_ungeoreferenced_image_is_refused(tmp_path):
    """An image with no CRS cannot be placed on the map, and guessing where it
    goes would be worse than refusing."""
    from PIL import Image

    path = tmp_path / "plain.png"
    Image.fromarray(np.zeros((16, 16, 3), np.uint8)).save(path)
    ok, msg = LocalRasterProvider(path).available()
    assert not ok
    assert "CRS" in msg or "georeferenced" in msg


def test_cache_key_is_stable_and_extent_sensitive():
    a = cache_key("p", EXT, 32615, 1024)
    b = cache_key("p", EXT, 32615, 1024)
    c = cache_key("p", Extent(0, 10, 0, 10), 32615, 1024)
    d = cache_key("p", EXT, 4326, 1024)
    assert a == b
    assert a != c and a != d


def test_cache_round_trip_without_network(tmp_path):
    """A cached image must come back without the provider being called."""
    from PIL import Image

    class Boom(ArcGISImageryProvider):
        def __init__(self):
            super().__init__("cached-src", "https://example.invalid/ImageServer")

        def fetch(self, extent, epsg, size=1024):
            raise AssertionError("network must not be touched on a cache hit")

    provider = Boom()
    key = cache_key(provider.name, EXT, 32615, 64)
    img = np.random.default_rng(0).integers(0, 255, (64, 64, 3), dtype=np.uint8)
    Image.fromarray(img).save(tmp_path / f"imagery_{key}.png")

    layer = fetch_cached(provider, EXT, 32615, 64, cache_dir=tmp_path)
    assert layer.image.shape == (64, 64, 3)
    assert "cached" in layer.source


def test_raster_layer_reports_ground_sample_distance():
    img = np.zeros((100, 100, 3), np.uint8)
    layer = RasterLayer(image=img, extent=Extent(0, 50, 0, 50), epsg=32615,
                        source="t")
    assert layer.px == pytest.approx(0.5)


def test_vector_layer_defaults_to_not_survey_grade():
    """Parcel data is cartographic. Defaulting the other way would invite it to
    be mistaken for a surveyed boundary."""
    layer = VectorLayer(rings=[np.zeros((4, 2))], epsg=32615, source="t")
    assert layer.survey_grade is False
    assert "REFERENCE ONLY" in layer.describe()


def test_all_bundled_vector_providers_are_reference_only():
    for provider in default_vector_providers().values():
        assert not provider.survey_grade


def test_default_providers_are_keyless_public_sources():
    """Google, Bing and Mapbox are excluded on purpose: their terms restrict
    caching and derivative use, which is what this tool does with imagery."""
    names = " ".join(default_providers()).lower()
    for banned in ("google", "bing", "mapbox"):
        assert banned not in names


# --- live services -------------------------------------------------------

@pytest.mark.network
@pytest.mark.parametrize("name", [
    "Iowa ortho 2016-2018 (leaf-off, ~22 cm)",
    "Iowa NAIP 2023 (30 cm)",
    "USGS NAIP (nationwide fallback)",
])
def test_imagery_provider_returns_the_requested_extent(name):
    provider = default_providers()[name]
    ok, msg = provider.available()
    if not ok:
        pytest.skip(f"{name} unreachable: {msg}")

    layer = provider.fetch(EXT, 32615, size=256)
    assert layer.image.shape == (256, 256, 3)
    assert layer.epsg == 32615
    assert layer.extent.xmin == pytest.approx(EXT.xmin)
    # A blank or all-black tile means the request landed somewhere wrong.
    assert layer.image.mean() > 20


@pytest.mark.network
def test_story_county_parcels_come_back_in_the_site_crs():
    provider = default_vector_providers()["Story County parcels"]
    ok, msg = provider.available()
    if not ok:
        pytest.skip(f"Story County unreachable: {msg}")

    layer = provider.fetch(EXT, 32615)
    assert len(layer) > 0
    assert not layer.survey_grade
    # Published in EPSG:3417 (Iowa North, ftUS); outSR must have reprojected.
    ring = layer.rings[0]
    assert 400_000 < ring[:, 0].mean() < 500_000
    assert 4_600_000 < ring[:, 1].mean() < 4_700_000


@pytest.mark.network
def test_geoid_service_returns_geoid18():
    from gpsrtk.vertical import fetch_geoid_separation

    try:
        sep = fetch_geoid_separation(41.591087000, -93.603278000)
    except Exception as exc:                                  # noqa: BLE001
        pytest.skip(f"NGS unreachable: {exc}")
    assert sep.model.startswith("GEOID")
    assert -35 < sep.value_m < -25


def test_blank_tile_detection_uses_distinct_values_not_variance(monkeypatch):
    """A 1 m DEM over a lot with ~1.2 m of relief is legitimately almost
    uniform (measured: 3 distinct values, std 1.18). Rejecting it as "no
    coverage" would be wrong, so the test counts distinct values instead."""
    import io as _io

    import numpy as _np
    from PIL import Image as _Image

    from gpsrtk.io import imagery as I

    class FakeResponse:
        def __init__(self, arr):
            buf = _io.BytesIO()
            _Image.fromarray(arr).save(buf, format="PNG")
            self.content = buf.getvalue()
            self.headers = {"Content-Type": "image/png"}

        def raise_for_status(self):
            return None

    def serve(arr):
        def _get(url, params=None, timeout=None, headers=None):
            return FakeResponse(arr)
        return _get

    provider = I.ArcGISImageryProvider("t", "https://example.invalid/ImageServer")
    import requests

    # Uniform tile: genuinely empty.
    blank = _np.full((32, 32, 3), 253, _np.uint8)
    monkeypatch.setattr(requests, "get", serve(blank))
    with pytest.raises(I.NoCoverageError):
        provider.fetch(EXT, 32615, size=32)

    # Low-contrast but real, like a DEM.
    dem = _np.full((32, 32, 3), 82, _np.uint8)
    dem[16:, :, :] = 87
    monkeypatch.setattr(requests, "get", serve(dem))
    layer = provider.fetch(EXT, 32615, size=32)
    assert layer.image.std() < 3.0, "this fixture is deliberately low contrast"


def test_network_tests_stay_off_under_any_other_mark_expression():
    """Any -m replaces pyproject's `-m 'not network'`, so `-m "not
    browser"` used to hit live services. They now run only when asked for."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items() if k != "YARDSURVEY_NETWORK"}
    env["PYTHONPATH"] = str(root)
    run = subprocess.run(
        [sys.executable, "-m", "pytest", str(Path(__file__)), "-m", "not browser",
         "-k", "provider_returns or parcels_come_back or geoid_service",
         "-q", "-p", "no:cacheprovider", "-p", "no:warnings"],
        capture_output=True, text=True, cwd=root, env=env, timeout=120)
    assert "5 skipped" in run.stdout, run.stdout[-800:]
    assert "passed" not in run.stdout and "failed" not in run.stdout
