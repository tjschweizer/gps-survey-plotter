"""A synthetic SW Maps export, so tests of the application do not need real data.

The reference export in `archive/` pins measured facts about real ground and
stays where it is: the established findings are about that lawn and nothing
else. But most of what the application does - loading, merging, filtering,
solving, planning, drawing - is behaviour that any plausible export exercises
equally well, and gating all of it on a private file meant most of the suite
skipped everywhere except one machine.

So this writes a small lot in the same CSV layout SW Maps exports, with the
properties the pipeline actually depends on:

  * mower passes ~0.3 m apart along a pass and 1 m between passes, crossed by a
    second set of passes later in the session, so there are crossovers well
    over 60 s apart;
  * RTK fixed nearly everywhere, float under a patch of "canopy" in the
    north-west, with float's larger noise and bias;
  * a speed that drops at the turns, so the speed filter has something to do;
  * a laser spot layer of lawn shots from two instrument setups that share two
    points, with point 1 as the benchmark.

It sits around the example site's origin, so `example_site()` is the right
site for it. Nothing here describes a real property.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

# Example site origin (Iowa State Capitol seed, snapped to the 25 m grid).
ORIGIN_E, ORIGIN_N = 449700.0, 4604550.0
LOT_W, LOT_H = 40.0, 30.0            # metres east and north of the origin
ANTENNA_M = 1.0                      # mower-mounted antenna above ground
ROD_ANTENNA_M = 2.0                  # planted rod antenna above ground
EPSG = 32615


def ground(le, ln):
    """Ellipsoidal ground height at local coordinates. About 1.2 m of relief."""
    le, ln = np.asarray(le, dtype=float), np.asarray(ln, dtype=float)
    return (261.0 - 0.02 * le + 0.015 * ln
            + 0.25 * np.exp(-((le - 20.0) ** 2 + (ln - 15.0) ** 2) / 50.0))


def _stamp(t: pd.Series) -> list[str]:
    return [s.strftime("%m/%d/%Y %H:%M:%S.%f")[:-3] + " CDT" for s in t]


def _latlon(e, n):
    from pyproj import Transformer

    tx = Transformer.from_crs(f"EPSG:{EPSG}", "EPSG:4326", always_xy=True)
    lon, lat = tx.transform(e, n)
    return lat, lon


def track_points(*, day: str = "2026-08-27 13:00:00", dz: float = 0.0,
                 de: float = 0.0, seed: int = 0) -> pd.DataFrame:
    """Mower passes east-west, then north-south across them."""
    rng = np.random.default_rng(seed)
    runs = []
    for i, ln in enumerate(np.arange(0.5, LOT_H, 1.0)):          # east-west
        le = np.arange(0.0, LOT_W, 0.3)
        runs.append((le if i % 2 == 0 else le[::-1], np.full(le.size, ln),
                     90.0 if i % 2 == 0 else 270.0))
    for i, le in enumerate(np.arange(0.5, LOT_W, 1.0)):          # north-south
        ln = np.arange(0.0, LOT_H, 0.3)
        runs.append((np.full(ln.size, le), ln if i % 2 == 0 else ln[::-1],
                     0.0 if i % 2 == 0 else 180.0))

    le = np.concatenate([r[0] for r in runs])
    ln = np.concatenate([r[1] for r in runs])
    bearing = np.concatenate([np.full(r[0].size, r[2]) for r in runs])
    track = np.concatenate([np.full(r[0].size, k) for k, r in enumerate(runs)])

    # Slow near the ends of each pass, where the mower turns.
    pos = np.concatenate([np.arange(r[0].size) for r in runs])
    length = np.concatenate([np.full(r[0].size, r[0].size) for r in runs])
    edge = np.minimum(pos, length - 1 - pos)
    speed = np.where(edge < 6, 0.45, 1.05) + rng.normal(0, 0.03, le.size)

    canopy = (le < 10.0) & (ln > 22.0)
    fix = np.where(canopy & (rng.random(le.size) < 0.6), 5, 4)
    noise = np.where(fix == 4, rng.normal(0, 0.015, le.size),
                     rng.normal(0.12, 0.25, le.size))

    t = pd.Timestamp(day) + pd.to_timedelta(np.arange(le.size) * 0.3, "s")
    e = ORIGIN_E + le + de
    n = ORIGIN_N + ln
    lat, lon = _latlon(e, n)
    z = ground(le, ln) + ANTENNA_M + dz + noise

    return pd.DataFrame({
        "ID": np.arange(1, le.size + 1),
        "Track Name": [f"Track {k + 1}" for k in track],
        "Time": _stamp(t),
        "X": e.round(3), "Y": n.round(3), "Elevation": z.round(3),
        "Lat": lat.round(9), "Lon": lon.round(9),
        "Ortho Height": 0.0,
        "Fix ID": fix,
        "Horizontal Accuracy": np.where(fix == 4, 0.014, 0.30),
        "Vertical Accuracy": np.where(fix == 4, 0.021, 0.45),
        "Speed": speed.round(3), "Bearing": bearing,
        "PDOP": 1.2, "Satellites in Use": 28, "Instrument Ht": 0.0,
    })


# Lawn spots: (id, local e, local n, setup). Setups 0 and 1 share points 7, 8,
# so the level network has redundancy between them.
SPOTS = [
    (1, 5.0, 5.0, 0), (2, 15.0, 5.0, 0), (3, 25.0, 5.0, 0), (4, 35.0, 5.0, 0),
    (5, 5.0, 15.0, 0), (6, 15.0, 15.0, 0), (7, 25.0, 15.0, 0),
    (8, 35.0, 15.0, 0),
    (7, 25.0, 15.0, 1), (8, 35.0, 15.0, 1), (9, 5.0, 25.0, 1),
    (10, 15.0, 25.0, 1), (11, 25.0, 25.0, 1), (12, 35.0, 25.0, 1),
]
HI = {0: 263.10, 1: 262.85}          # laser plane heights, ellipsoidal metres


def spot_rows(*, day: str = "2026-08-27 12:00:00", de: float = 0.0,
              dz: float = 0.0) -> pd.DataFrame:
    ids = np.array([s[0] for s in SPOTS])
    le = np.array([s[1] for s in SPOTS])
    ln = np.array([s[2] for s in SPOTS])
    setup = np.array([s[3] for s in SPOTS])
    g = ground(le, ln)
    rod_in = (np.array([HI[s] for s in setup]) - g) / 0.0254
    e, n = ORIGIN_E + le + de, ORIGIN_N + ln
    lat, lon = _latlon(e, n)
    t = pd.Timestamp(day) + pd.to_timedelta(np.arange(len(ids)) * 45, "s")
    return pd.DataFrame({
        "ID": ids,
        "Feature Name": [f"spot {i}" for i in ids],
        "Time": _stamp(t),
        "X": e.round(3), "Y": n.round(3),
        "Elevation": (g + ROD_ANTENNA_M + dz).round(3),
        "Latitude": lat.round(9), "Longitude": lon.round(9),
        "Fix ID": 4,
        "height number": rod_in.round(2),
        "type": "lawn",
        "base position": setup,
    })


def write_export(path: str | Path, *, day: str = "2026-08-27 13:00:00",
                 dz: float = 0.0, de: float = 0.0, seed: int = 0,
                 spots: bool = True) -> Path:
    """Write a SW Maps CSV export zip and return its path.

    `dz` is a constant vertical bias for the whole outing - a different
    antenna mount - and `de` moves it east, which with a large value puts it
    on ground no other outing covers.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stem = path.stem
    date = pd.Timestamp(day)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{stem}_TRACK_POINTS.csv",
                   track_points(day=day, dz=dz, de=de, seed=seed)
                   .to_csv(index=False))
        if spots:
            sp = spot_rows(day=str(date - pd.Timedelta(hours=1)), de=de, dz=dz)
            z.writestr(f"{stem}_spots.csv", sp.to_csv(index=False))
            # FEATURE_POINTS repeats every spot without the custom columns.
            feat = sp.drop(columns=["height number", "type", "base position"])
            feat.insert(2, "Layer", "spots")
            z.writestr(f"{stem}_FEATURE_POINTS.csv", feat.to_csv(index=False))
    return path
