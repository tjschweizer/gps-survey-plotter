"""Aerial imagery: local files and web services.

Providers are pluggable and every one of them is health-checked before use,
because public GIS services go down and stay down. At the time of writing,
of the sources that looked obvious on paper:

  USGS NAIPPlus ImageServer     works, no key, nationwide - the default
  USGS ImageryOnly MapServer    works, no key
  Iowa DOT statewide E911       returns "Service not started"
  ISU ortho WMS endpoints       404; the published endpoint list is stale
  USDA APFO NAIP                TLS handshake failure

So the design assumption is that any given source is probably broken, and the
UI should say which ones actually answered rather than failing silently.

ArcGIS services reproject server-side via `imageSR`, so imagery arrives already
in the site CRS and no client-side warping is needed. Local files are warped
with rasterio because they arrive in whatever CRS they were written in.
"""

from __future__ import annotations

import hashlib
import io
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from ..surface import Extent

USER_AGENT = "yardsurvey/0.1 (+terrain survey tooling)"
TIMEOUT = 60.0

# A mosaic whose published extent covers the state will still return a blank
# tile for a county that has not been flown yet, and an ImageServer with a
# minimum scale returns blank when asked for a small extent. Both come back
# HTTP 200 with a valid PNG, so the only way to tell is to look at it.
#
# The test is the count of distinct pixel values, not the standard deviation.
# A 1 m DEM over a lot with 1.2 m of relief is legitimately almost uniform
# (measured: 3 distinct values, std 1.18) and rejecting it as "no coverage"
# would be wrong. A genuinely empty tile has exactly one value, since PNG is
# lossless and introduces no noise of its own.
MIN_DISTINCT_VALUES = 2


class NoCoverageError(RuntimeError):
    """The service answered, but with no actual imagery for this extent."""


@dataclass
class RasterLayer:
    """A georeferenced image in a known CRS."""

    image: np.ndarray            # (h, w, 3|4) uint8, row 0 = NORTH
    extent: Extent
    epsg: int
    source: str
    attribution: str = ""
    fetched: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def px(self) -> float:
        return self.extent.width / self.image.shape[1]

    def describe(self) -> str:
        h, w = self.image.shape[:2]
        return (f"{self.source}: {w}x{h}, {self.px * 100:.1f} cm/px, "
                f"EPSG:{self.epsg}")


class ImageryProvider(ABC):
    """Something that can supply imagery for a bounding box."""

    name: str = "provider"
    description: str = ""
    attribution: str = ""

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """(reachable, human-readable status). Never raises."""

    @abstractmethod
    def fetch(self, extent: Extent, epsg: int, size: int = 1024) -> RasterLayer:
        ...


# --- web services ---------------------------------------------------------

class ArcGISImageryProvider(ImageryProvider):
    """ArcGIS ImageServer or MapServer image export.

    `imageSR`/`bboxSR` make the server do the reprojection, so a request in
    UTM 15N comes back in UTM 15N even though the service publishes in Web
    Mercator.
    """

    def __init__(self, name: str, url: str, *, kind: str = "image",
                 description: str = "", attribution: str = "",
                 layers: str | None = None):
        self.name = name
        self.url = url.rstrip("/")
        self.kind = kind                     # "image" (ImageServer) | "map"
        self.description = description
        self.attribution = attribution
        self.layers = layers

    @property
    def _endpoint(self) -> str:
        return f"{self.url}/{'exportImage' if self.kind == 'image' else 'export'}"

    def available(self) -> tuple[bool, str]:
        import requests
        try:
            r = requests.get(self.url, params={"f": "pjson"}, timeout=TIMEOUT,
                             headers={"User-Agent": USER_AGENT})
            if not r.ok:
                return False, f"HTTP {r.status_code}"
            payload = r.json()
            if "error" in payload:
                return False, str(payload["error"].get("message", "error"))
            return True, "ok"
        except Exception as exc:                              # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"[:120]

    def fetch(self, extent: Extent, epsg: int, size: int = 1024) -> RasterLayer:
        import requests
        from PIL import Image

        params = {
            "bbox": f"{extent.xmin},{extent.ymin},{extent.xmax},{extent.ymax}",
            "bboxSR": epsg,
            "imageSR": epsg,
            "size": f"{size},{size}",
            "format": "png",
            "f": "image",
        }
        if self.kind == "map":
            params["transparent"] = "false"
            if self.layers:
                params["layers"] = self.layers

        r = requests.get(self._endpoint, params=params, timeout=TIMEOUT,
                         headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        if not r.headers.get("Content-Type", "").startswith("image"):
            raise RuntimeError(f"{self.name} returned {r.text[:200]}")

        img = np.asarray(Image.open(io.BytesIO(r.content)).convert("RGB"))
        if np.unique(img).size < MIN_DISTINCT_VALUES:
            raise NoCoverageError(
                f"{self.name} returned a blank tile for this extent. The "
                "service covers the state on paper, but this area may not be "
                "flown yet, or the request is below the mosaic's minimum "
                "scale. Try a different source or a wider extent.")
        return RasterLayer(image=img, extent=extent, epsg=epsg,
                           source=self.name, attribution=self.attribution)


class LocalRasterProvider(ImageryProvider):
    """A GeoTIFF or world-filed image on disk, warped to the target CRS."""

    name = "local file"

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.description = str(self.path)
        self.attribution = ""

    def available(self) -> tuple[bool, str]:
        if not self.path.exists():
            return False, "file not found"
        try:
            import rasterio
            with rasterio.open(self.path) as src:
                if src.crs is None:
                    return False, "no CRS: image is not georeferenced"
                return True, f"{src.width}x{src.height}, {src.crs}"
        except Exception as exc:                              # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"[:120]

    def fetch(self, extent: Extent, epsg: int, size: int = 1024) -> RasterLayer:
        import rasterio
        from rasterio.warp import Resampling, calculate_default_transform, reproject

        with rasterio.open(self.path) as src:
            if src.crs is None:
                raise ValueError(
                    f"{self.path.name} has no CRS. Georeference it first, or "
                    "supply a world file alongside it.")

            dst_transform = rasterio.transform.from_bounds(
                extent.xmin, extent.ymin, extent.xmax, extent.ymax, size, size)
            bands = min(src.count, 3)
            out = np.zeros((bands, size, size), dtype=np.uint8)
            for b in range(bands):
                reproject(
                    source=rasterio.band(src, b + 1),
                    destination=out[b],
                    src_transform=src.transform, src_crs=src.crs,
                    dst_transform=dst_transform, dst_crs=f"EPSG:{epsg}",
                    resampling=Resampling.bilinear)

            image = np.transpose(out, (1, 2, 0))
            if bands == 1:
                image = np.repeat(image, 3, axis=2)
            return RasterLayer(image=image, extent=extent, epsg=epsg,
                               source=self.path.name)


# --- presets --------------------------------------------------------------

ISU = "https://ortho.gis.iastate.edu/arcgis/rest/services/ortho"
ISU_CREDIT = "Iowa Geographic Map Server, Iowa State University GIS Facility"


def default_providers() -> dict[str, ImageryProvider]:
    """Photo imagery, best first.

    The Iowa Geographic Map Server carries far better imagery for this site
    than the national mosaics, and it is live - the 404s from the endpoint list
    published on its own tools page are because that list is stale and still
    advertises `/arcgisserver/services/.../WMSServer`. The working root is
    `/arcgis/rest/services/ortho`.

    Measured native ground sample distance over this lot:

        ortho_2016_2018      ~22 cm  and LEAF-OFF
        naip_2023            30 cm   leaf-on
        naip_2021 / 2025     60 cm   leaf-on
        USGS NAIPPlus        60 cm   leaf-on, and blank below ~30 m extents
        ortho_2023_2025      1 in on paper, but blank here - not flown yet

    Leaf-off matters more than resolution for this project. The west and
    northwest canopy is what drives ~48% float, and in a spring flight you can
    see the ground under those trees.

    Nothing here needs an API key. Google, Bing and Mapbox are deliberately
    absent: their terms restrict caching and derivative use, which is exactly
    what a survey tool does with imagery.
    """
    return {
        p.name: p for p in [
            ArcGISImageryProvider(
                "Iowa ortho 2016-2018 (leaf-off, ~22 cm)",
                f"{ISU}/ortho_2016_2018_nc/ImageServer",
                description="Sharpest imagery that actually covers this lot, "
                            "and flown leaf-off so ground under canopy is "
                            "visible",
                attribution=ISU_CREDIT),
            ArcGISImageryProvider(
                "Iowa NAIP 2023 (30 cm)",
                f"{ISU}/naip_2023_nc/ImageServer",
                description="Most recent good-resolution cover, leaf-on",
                attribution=ISU_CREDIT),
            ArcGISImageryProvider(
                "Iowa NAIP 2025 (60 cm)",
                f"{ISU}/naip_2025_nc/ImageServer",
                description="Newest imagery, but coarser",
                attribution=ISU_CREDIT),
            ArcGISImageryProvider(
                "Iowa ortho 2023-2025 (1 in, patchy)",
                f"{ISU}/ortho_2023_2025_nc/ImageServer",
                description="Very high resolution where flown; returned blank "
                            "for this lot when last checked",
                attribution=ISU_CREDIT),
            ArcGISImageryProvider(
                "USGS NAIP (nationwide fallback)",
                "https://imagery.nationalmap.gov/arcgis/rest/services/"
                "USGSNAIPPlus/ImageServer",
                description="Works anywhere in the US; use for other properties",
                attribution="USDA NAIP via USGS The National Map"),
            ArcGISImageryProvider(
                "Iowa ortho 1990 (historical)",
                f"{ISU}/ortho_1990/ImageServer",
                description="For seeing how the lot has changed",
                attribution=ISU_CREDIT),
        ]
    }


def terrain_providers() -> dict[str, ImageryProvider]:
    """LiDAR-derived rasters - not photographs.

    The 2020 LiDAR DEM is an INDEPENDENT elevation source for this lot. It is
    1 m and nowhere near RTK accuracy, so it will not improve the surface, but
    differencing against it is the external check the project has never had:
    it catches a whole-surface datum blunder that crossover statistics, being
    internal to the data, cannot see.

    The hillshade and intensity rasters are also the best way to spot break
    lines - swales, the driveway crown, the curb flowline - before going out
    to shoot them.
    """
    return {
        p.name: p for p in [
            ArcGISImageryProvider(
                "Iowa LiDAR 2020 hillshade (1 m)",
                f"{ISU}/lidar_2020_hillshade/ImageServer",
                description="Shaded relief; good for finding break lines",
                attribution=ISU_CREDIT),
            ArcGISImageryProvider(
                "Iowa LiDAR 2020 DEM (1 m)",
                f"{ISU}/lidar_2020_dem/ImageServer",
                description="Bare-earth elevation, independent of our GNSS",
                attribution=ISU_CREDIT),
            ArcGISImageryProvider(
                "Iowa LiDAR 2020 intensity (1 m)",
                f"{ISU}/lidar_2020_intensity/ImageServer",
                description="Return intensity; pavement and turf differ",
                attribution=ISU_CREDIT),
            ArcGISImageryProvider(
                "Iowa LiDAR 2020 slope (1 m)",
                f"{ISU}/lidar_2020_slope/ImageServer",
                description="Slope raster",
                attribution=ISU_CREDIT),
        ]
    }


# --- caching --------------------------------------------------------------

def cache_key(provider: str, extent: Extent, epsg: int, size: int) -> str:
    raw = (f"{provider}|{extent.xmin:.2f},{extent.ymin:.2f},"
           f"{extent.xmax:.2f},{extent.ymax:.2f}|{epsg}|{size}")
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def fetch_cached(provider: ImageryProvider, extent: Extent, epsg: int,
                 size: int = 1024, cache_dir: str | Path | None = None
                 ) -> RasterLayer:
    """Fetch, reusing a previously saved image for the same request.

    Imagery for a fixed lot never changes between runs, and refetching it on
    every launch is slow and rude to a free public service.
    """
    if cache_dir is None:
        return provider.fetch(extent, epsg, size)

    from PIL import Image

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = cache_key(provider.name, extent, epsg, size)
    png = cache_dir / f"imagery_{key}.png"

    if png.exists():
        img = np.asarray(Image.open(png).convert("RGB"))
        return RasterLayer(image=img, extent=extent, epsg=epsg,
                           source=f"{provider.name} (cached)",
                           attribution=provider.attribution)

    layer = provider.fetch(extent, epsg, size)
    Image.fromarray(layer.image).save(png)
    return layer
