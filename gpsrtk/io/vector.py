"""Vector reference layers: parcels, lots, rights of way.

Parcel polygons are cartographic, not survey grade - typically a foot to three
feet of positional uncertainty. They are extremely useful as a reference for
where the boundary roughly is, and they must never be mistaken for the boundary
itself. That distinction is carried in the data (`survey_grade` is False) and
should be carried in how they are drawn.

An authoritative boundary comes from a recorded plat traversed from a monument
that was actually shot with RTK. That is a different, later feature; this module
deliberately does not pretend to provide it.

Story County publishes in EPSG:3417 (Iowa North, US survey feet) but honours
`outSR`, so features come back already in the site CRS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from ..surface import Extent

USER_AGENT = "yardsurvey/0.1 (+terrain survey tooling)"
TIMEOUT = 45.0


@dataclass
class VectorLayer:
    """Polygons or polylines in a known CRS."""

    rings: list[np.ndarray]              # each (n, 2) in projected coordinates
    attributes: list[dict] = field(default_factory=list)
    epsg: int = 0
    source: str = ""
    survey_grade: bool = False
    fetched: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __len__(self) -> int:
        return len(self.rings)

    def describe(self) -> str:
        grade = "survey grade" if self.survey_grade else "REFERENCE ONLY"
        return f"{self.source}: {len(self.rings)} shapes, EPSG:{self.epsg} ({grade})"


class ArcGISFeatureProvider:
    """Queries an ArcGIS feature layer for shapes intersecting an extent."""

    def __init__(self, name: str, url: str, *, description: str = "",
                 survey_grade: bool = False, out_fields: str = "*"):
        self.name = name
        self.url = url.rstrip("/")
        self.description = description
        self.survey_grade = survey_grade
        self.out_fields = out_fields

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

    def fetch(self, extent: Extent, epsg: int) -> VectorLayer:
        import requests

        params = {
            "geometry": (f"{extent.xmin},{extent.ymin},"
                         f"{extent.xmax},{extent.ymax}"),
            "geometryType": "esriGeometryEnvelope",
            "inSR": epsg,
            "outSR": epsg,
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": self.out_fields,
            "returnGeometry": "true",
            "f": "json",
        }
        r = requests.get(f"{self.url}/query", params=params, timeout=TIMEOUT,
                         headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        payload = r.json()
        if "error" in payload:
            raise RuntimeError(f"{self.name}: {payload['error']}")

        rings: list[np.ndarray] = []
        attrs: list[dict] = []
        for feature in payload.get("features", []):
            geom = feature.get("geometry") or {}
            # Polygons carry "rings"; polylines carry "paths".
            for part in geom.get("rings", []) or geom.get("paths", []):
                rings.append(np.asarray(part, dtype=float)[:, :2])
                attrs.append(feature.get("attributes", {}))

        return VectorLayer(rings=rings, attributes=attrs, epsg=epsg,
                           source=self.name, survey_grade=self.survey_grade)


def default_vector_providers() -> dict[str, ArcGISFeatureProvider]:
    """Reference linework for the county this was built for.

    None of these are survey grade. They are here to orient the map and to make
    it obvious where a boundary approximately runs, not to define one.
    """
    base = "https://apps.storycounty.com/arcgis/rest/services"
    return {
        p.name: p for p in [
            ArcGISFeatureProvider(
                "Story County parcels", f"{base}/parcels/MapServer/0",
                description="Assessor parcel polygons (cartographic, +/- 1-3 ft)"),
            ArcGISFeatureProvider(
                "Story County lots", f"{base}/Lots/MapServer/0",
                description="Platted lot lines (cartographic)"),
            ArcGISFeatureProvider(
                "Story County right of way", f"{base}/RightOfWay/MapServer/0",
                description="Road right of way (cartographic)"),
        ]
    }
