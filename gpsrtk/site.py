"""Site: the spatial reference for one property.

Every constant that the archive scripts hardcoded at module top lives here
instead, so that pointing the tool at a second property is a config change
rather than an edit to five files:

    process_full.py     ORIGIN_E / ORIGIN_N / DATUM_ELEV_FT / GRID_M
    to_revit.py         ORIGIN_E / ORIGIN_N
    heightmap.py        CELL / DMAX / SIZE / MEDSZ / SIG
    iso.py              CELL / 1.6 / N / VE
    process_survey.py   DATUM_ID / DATUM_ELEV_FT / GRID_N

The local origin exists because Revit degrades past roughly 33 km from its
internal origin and raw UTM northings are ~4676 km out. It must never change
for a given site once anything has been exported, or surfaces will not align.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict
from pathlib import Path


# Origins are snapped to this grid. 25 m is fine enough to stay well inside
# Revit's comfortable range and coarse enough to be a memorable round number,
# and snapping means the origin of an existing survey is reproduced exactly
# from any point in it rather than depending on which point you started from.
ORIGIN_SNAP_M = 25.0

# A site is data, not code: it lives in the project file. This path is the
# convenience default for a fresh window, and is deliberately kept out of
# version control - a site file pins a property to the metre, and that is not
# something to publish along with the tool.
LOCAL_SITE = "site.local.json"


@dataclass
class SurfaceDefaults:
    """Gridding parameters. Defaults are the values established in CLAUDE.md."""

    # Points are ~0.3 m apart along a mowing pass and ~1 m between passes.
    # Raw triangulation of that produces sliver triangles that hillshade into
    # false corduroy, so samples are binned before interpolation.
    bin_cell_m: float = 0.5

    # Anything further than this from a measured sample is not terrain we
    # surveyed - the house footprint, tree wells, outside the mowed hull.
    # It gets masked rather than presented as ground.
    mask_distance_m: float = 1.6

    median_size: int = 3
    gaussian_sigma: float = 2.0
    raster_size: int = 1024

    # Percentile clip used to drop gross elevation outliers before gridding.
    despike_percentiles: tuple[float, float] = (0.2, 99.8)


@dataclass
class VerticalDatum:
    """How elevations are referenced.

    The receiver reports ellipsoidal height; SW Maps' `Ortho Height` column is
    zero because no geoid model is loaded. `geoid_separation_m` converts to
    NAVD88 and is a single constant for a site this size - GEOID18 varies far
    below a millimetre across a residential lot - so one fetched value covers
    the whole property.

    The benchmark is whichever shot the whole survey hangs from. Holding it at
    an arbitrary 100.000 ft produces a self-consistent surface that sits
    nowhere in particular; holding a shot taken on a KNOWN building feature at
    that feature's real elevation puts the whole survey in the model's frame
    instead. Both go through the same field, so tying the datum is a matter of
    changing which point is held and at what value - not of adding another
    correction on top.

    `tied_to_model` records which of those two situations you are in, because
    "100.00 ft" and "0.25 ft" look equally plausible in an export and only one
    of them means anything outside this project.
    """

    name: str = "local arbitrary"
    benchmark_point_id: int = 1
    benchmark_elev_ft: float = 100.0
    benchmark_note: str = ""
    geoid_separation_m: float | None = None
    tied_to_model: bool = False
    model_frame: str = "Revit project"

    def describe(self) -> str:
        if self.tied_to_model:
            head = (f"tied to {self.model_frame}: point "
                    f"{self.benchmark_point_id} held at "
                    f"{self.benchmark_elev_ft:.3f} ft")
        else:
            head = (f"LOCAL ARBITRARY: point {self.benchmark_point_id} held at "
                    f"{self.benchmark_elev_ft:.3f} ft, which is a chosen "
                    f"number, not a real elevation")
        return head + (f" - {self.benchmark_note}" if self.benchmark_note else "")


@dataclass
class Site:
    name: str
    epsg: int
    origin_e: float
    origin_n: float
    vertical: VerticalDatum = field(default_factory=VerticalDatum)
    surface: SurfaceDefaults = field(default_factory=SurfaceDefaults)
    notes: str = ""

    # --- construction ----------------------------------------------------

    @staticmethod
    def utm_epsg(lat: float, lon: float) -> int:
        """EPSG code of the UTM zone containing this coordinate."""
        zone = int(math.floor((lon + 180.0) / 6.0)) + 1
        return (32600 if lat >= 0 else 32700) + zone

    @staticmethod
    def snap_origin(easting: float, northing: float,
                    step: float = ORIGIN_SNAP_M) -> tuple[float, float]:
        """Round a coordinate down onto the origin grid."""
        return (math.floor(easting / step) * step,
                math.floor(northing / step) * step)

    @classmethod
    def from_seed(cls, name: str, lat: float, lon: float, **kw) -> "Site":
        """Build a site from one representative coordinate on the property.

        Picks the UTM zone and a local origin automatically, so a new property
        needs no hand-chosen constants.
        """
        from pyproj import Transformer

        epsg = cls.utm_epsg(lat, lon)
        tx = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
        e, n = tx.transform(lon, lat)
        oe, on = cls.snap_origin(e, n)
        return cls(name=name, epsg=epsg, origin_e=oe, origin_n=on, **kw)

    # --- coordinate helpers ----------------------------------------------

    def to_local(self, easting, northing):
        """Projected coordinates -> metres from the local origin."""
        return easting - self.origin_e, northing - self.origin_n

    def to_projected(self, local_e, local_n):
        """Metres from the local origin -> projected coordinates."""
        return local_e + self.origin_e, local_n + self.origin_n

    def factors(self, lat: float, lon: float):
        """Grid convergence (degrees) and point scale factor at a coordinate.

        Convergence is the angle between grid north and true north. In the
        middle of a UTM zone it is small but not negligible: about -0.43 deg
        at the site this was built for, which puts a bearing drawn directly in
        UTM roughly 0.75 ft off over 100 ft. That is the same order as a
        building-diagonal disagreement, so it is worth ruling in or out before
        plat bearings are trusted.

        Scale factor converts between grid and ground distance. Plat distances
        are ground; UTM is grid.
        """
        from pyproj import Proj

        # pyproj already converts PROJ's radian convergence to degrees; do not
        # convert again. UTM is conformal, so meridional and parallel scale are
        # equal and either is the point scale factor.
        f = Proj(f"EPSG:{self.epsg}").get_factors(lon, lat)
        return f.meridian_convergence, f.meridional_scale

    # --- persistence -----------------------------------------------------

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Site":
        d = dict(d)
        d["vertical"] = VerticalDatum(**d.get("vertical", {}))
        sd = dict(d.get("surface", {}))
        if "despike_percentiles" in sd:
            sd["despike_percentiles"] = tuple(sd["despike_percentiles"])
        d["surface"] = SurfaceDefaults(**sd)
        return cls(**d)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), "utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Site":
        return cls.from_dict(json.loads(Path(path).read_text("utf-8")))


# Iowa State Capitol. A public building, in the UTM zone and the state whose
# imagery services this ships with, so the example is one you can actually
# fetch a basemap for - and nobody's home.
EXAMPLE_SEED = (41.591087, -93.603278)


def example_site() -> Site:
    """A worked example, and the fallback when no site has been configured.

    Deliberately not anybody's property. A real site is created from its own
    seed coordinate with `Site.from_seed`, stored in the project file, and -
    for the site you want a fresh window to open on - in `site.local.json`,
    which is not version controlled.
    """
    return Site.from_seed(
        "example site", *EXAMPLE_SEED,
        vertical=VerticalDatum(
            name="local arbitrary",
            benchmark_elev_ft=100.0,
            benchmark_note="held at 100.000 ft; not tied to a model datum",
        ),
        notes="Example site. Replace it with your own: create one from a "
              "seed coordinate, or drop a site.local.json beside the project.",
    )


def default_site(root: str | Path | None = None) -> Site:
    """The site a fresh window opens on.

    Reads `site.local.json` if there is one, so that a working install opens
    on the property being surveyed without that property's coordinates being
    committed anywhere. Falls back to the example.

    A malformed local file is reported rather than swallowed: silently opening
    on the example site while the user believes they are on their own would
    put every subsequent coordinate in the wrong frame.
    """
    path = Path(root or ".") / LOCAL_SITE
    if not path.exists():
        return example_site()
    try:
        return Site.load(path)
    except Exception as exc:                                  # noqa: BLE001
        raise ValueError(f"could not read {path}: {exc}") from exc
