"""Revit points-file export.

Revit's Toposolid / Toposurface importer (Create from Import -> Specify Points
File) wants a headerless comma-delimited file:

    X, Y, Z [, description]

with all three in the SAME unit, chosen at import time.

Two problems this solves, both inherited from to_revit.py:

1. UTM coordinates sit ~4676 km from the origin, and Revit degrades badly past
   roughly 33 km. Points are shifted to the site's local origin, and that
   origin is written to a sidecar so later sessions land in the same place.

2. Elevations are carried in feet on the local datum while X/Y are metres.
   Everything is converted to one unit.

A third problem the archive script did not face yet: Revit's importer becomes
unusable well before the point counts a few mowing sessions produce, so this
module decimates and says so rather than writing a file that will not open.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from ..model.pointset import PointSet, E, N, Z, KIND, elevation_column
from ..site import Site
from ..units import M_PER_FT, m_to_ft

# Revit's points-file importer slows to a crawl well before this; past roughly
# 30k it is effectively unusable. Warn rather than fail, since the threshold is
# soft and machine-dependent.
POINT_WARNING_THRESHOLD = 30_000


def write_points(ps: PointSet, site: Site, path: str | Path, *,
                 units: str = "feet",
                 z_column: str | None = None,
                 describe: bool = False) -> dict:
    """Write a Revit points file plus its origin sidecar.

    Elevation comes from the derived elevation column when a vertical model has
    been applied, and from raw ellipsoidal height otherwise. Both are carried
    in metres, so the unit conversion is the same either way; what changes is
    the datum, which the sidecar records.
    """
    path = Path(path)
    if units not in ("feet", "meters"):
        raise ValueError(f"units must be 'feet' or 'meters', got {units!r}")

    z_column = z_column or elevation_column(ps)
    d = ps.df
    local_e, local_n = site.to_local(d[E].to_numpy(), d[N].to_numpy())
    z = d[z_column].to_numpy()

    if units == "feet":
        x, y, z_out = m_to_ft(local_e), m_to_ft(local_n), m_to_ft(z)
    else:
        x, y, z_out = local_e, local_n, z

    out = pd.DataFrame({"x": x.round(4), "y": y.round(4), "z": z_out.round(4)})
    if describe and KIND in d.columns:
        out["desc"] = d[KIND].astype(str).to_numpy()

    out.to_csv(path, index=False, header=False)
    sidecar = path.with_name(path.stem + "_ORIGIN.txt")
    sidecar.write_text(_sidecar_text(site, units, len(out), z_column),
                       encoding="utf-8")

    return {
        "path": path,
        "sidecar": sidecar,
        "z_column": z_column,
        "n_points": len(out),
        "over_threshold": len(out) > POINT_WARNING_THRESHOLD,
        "bounds": {"x": (float(out.x.min()), float(out.x.max())),
                   "y": (float(out.y.min()), float(out.y.max())),
                   "z": (float(out.z.min()), float(out.z.max()))},
    }


def _sidecar_text(site: Site, units: str, n: int, z_column: str) -> str:
    v = site.vertical
    if z_column == Z:
        datum = ("RAW ELLIPSOIDAL HEIGHT - no vertical model applied.\n"
                 "                        These are antenna heights above the\n"
                 "                        ellipsoid, not ground elevations.")
    elif v.tied_to_model:
        datum = f"tied to {v.model_frame}"
    else:
        datum = ("LOCAL ARBITRARY - self-consistent, but the benchmark value\n"
                 "                        is a chosen number, not a real\n"
                 "                        elevation. Set the datum tie before\n"
                 "                        placing this in the model.")
    return (
        "Revit points file origin\n"
        "========================\n"
        f"site                  : {site.name}\n"
        f"CRS                   : EPSG:{site.epsg}\n"
        f"local origin easting  : {site.origin_e:.3f} m\n"
        f"local origin northing : {site.origin_n:.3f} m\n"
        f"units in points file  : {units}\n"
        f"elevation source      : {z_column}\n"
        f"elevation datum       : {datum}\n"
        f"benchmark point       : {v.benchmark_point_id}\n"
        f"benchmark             : {v.benchmark_elev_ft:.3f} ft"
        f"{' - ' + v.benchmark_note if v.benchmark_note else ''}\n"
        f"points                : {n:,}\n"
        "\n"
        "Set the Revit Survey Point to this UTM coordinate so shared\n"
        "coordinates stay correct. Reuse these exact values for every\n"
        "future import or surfaces will not align.\n"
    )
