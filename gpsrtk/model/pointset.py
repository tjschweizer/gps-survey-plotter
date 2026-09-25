"""PointSet: the canonical in-memory representation of survey points.

Every reader normalises into this schema so that filters, surfacing, and the
views never have to know which app produced the data. SW Maps is the current
source but is explicitly not assumed.

Normalising matters more than it looks. Within a single SW Maps export the
column names are already inconsistent: `*_spots.csv` uses `Latitude`/`Longitude`
while `*_TRACK_POINTS.csv` uses `Lat`/`Lon` for the same quantity, and the spots
file carries three trailing custom-attribute columns that the other files lack.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd


# --- canonical column names ----------------------------------------------
# Filters and surfacing refer to these symbolically, never to raw export names.

E = "e"                 # projected easting, metres
N = "n"                 # projected northing, metres
Z = "z_ellip_m"         # ellipsoidal height, metres (Ortho Height is always 0:
                        # no geoid model is loaded on the receiver)
ELEV = "elev_m"         # derived ground elevation on the working vertical
                        # datum, metres. Distinct from Z on purpose: Z is what
                        # the receiver reported, ELEV is what we concluded.
LAT = "lat"
LON = "lon"
TIME = "time"           # naive local datetime
TZ = "tz"               # timezone abbreviation as exported, e.g. "CDT"
FIX = "fix"             # 4 = RTK fixed, 5 = float
HACC = "hacc"           # horizontal accuracy, metres (receiver's own estimate)
VACC = "vacc"           # vertical accuracy, metres
SPEED = "speed"         # metres/second
BEARING = "bearing"     # degrees
PDOP, HDOP, VDOP = "pdop", "hdop", "vdop"
SATS_VIEW, SATS_USED = "sats_view", "sats_used"
ANT_HT = "antenna_ht_m"  # SW Maps `Instrument Ht`; 0.0 in every export so far,
                         # meaning Z is antenna height, not ground

# Correction-link quality. Present in the `.swmz` project database, absent from
# the CSV export. These say WHY a fix was good or bad, where hacc/vacc only say
# that the receiver was pleased with itself - and CLAUDE.md is explicit that
# the receiver's own estimate is not to be trusted over measured QC.
AGE_DIFF = "age_diff_s"      # seconds since the correction used was received
REF_STATION = "ref_station"  # network reference station the fix was made against
BASELINE = "baseline_m"      # distance to it

# spot-layer custom attributes
ROD_IN = "rod_in"       # laser rod reading, inches
SETUP = "setup"         # instrument setup id the rod reading belongs to
KIND = "kind"           # user-assigned classification: lawn, bldg, ...
STATION = "station"     # the physical point a rod was read on: "P12" for a
                        # planned shot, a mark name such as "BM1", any name
                        # for a turning point. See `vertical.stations`.

# provenance
TRACK = "track"         # track or layer name within the export
SOURCE = "source"       # file the point came from
SESSION = "session"     # acquisition session; carries the solvable offset

FIX_RTK = 4
FIX_FLOAT = 5

REQUIRED = (E, N, Z)

# Every PointSet ever made gets the next number. Unlike `id()`, which Python
# hands out again once an object has been collected, a token is never reused,
# so a cache keyed on it cannot mistake a new data set for an old one.
_TOKENS = itertools.count(1)


@dataclass
class PointSet:
    """Points plus provenance. Immutable by convention: filters return new ones.

    `df` is a pandas DataFrame using the canonical names above. Columns absent
    from a given source are simply missing rather than filled, so a filter can
    tell "not recorded" from "recorded as zero" - which matters for
    `antenna_ht_m`, where 0.0 is a real and misleading value.
    """

    df: pd.DataFrame
    layer: str = ""                       # 'track_points', 'spots', ...
    history: tuple[str, ...] = field(default_factory=tuple)
    token: int = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        missing = [c for c in REQUIRED if c not in self.df.columns]
        if missing:
            raise ValueError(
                f"PointSet missing required column(s): {', '.join(missing)}")
        self.token = next(_TOKENS)

    def __len__(self) -> int:
        return len(self.df)

    def has(self, *cols: str) -> bool:
        return all(c in self.df.columns for c in cols)

    def select(self, mask, label: str = "") -> "PointSet":
        """Return a new PointSet keeping rows where mask is True."""
        mask = np.asarray(mask)
        out = self.df.loc[mask].reset_index(drop=True)
        step = label or f"select({int(mask.sum())}/{len(mask)})"
        return replace(self, df=out, history=self.history + (step,))

    def with_frame(self, df: pd.DataFrame, label: str) -> "PointSet":
        """Return a new PointSet wrapping a transformed frame."""
        return replace(self, df=df.reset_index(drop=True),
                       history=self.history + (label,))

    # --- convenience accessors ------------------------------------------

    @property
    def xyz(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        d = self.df
        return d[E].to_numpy(), d[N].to_numpy(), d[Z].to_numpy()

    @property
    def fixed(self) -> "PointSet":
        """RTK-fixed points only - the default basis for any surface."""
        return self.select(self.df[FIX] == FIX_RTK, "fix == 4")

    def describe(self) -> str:
        d = self.df
        parts = [f"{len(d):,} points"]
        if FIX in d:
            vc = d[FIX].value_counts()
            parts.append(f"fixed {int(vc.get(FIX_RTK, 0)):,} / "
                         f"float {int(vc.get(FIX_FLOAT, 0)):,}")
        # Worth saying out loud once a layer spans more than one outing: until
        # the session offsets are solved, those outings are on different
        # datums and the surface steps between them.
        if SESSION in d and len(d):
            n_sessions = d[SESSION].astype(str).nunique()
            if n_sessions > 1:
                parts.append(f"{n_sessions} sessions")
        if len(d):
            column = elevation_column(self)
            values = d[column].dropna()
            if len(values):
                parts.append(f"z {values.min():.3f}-{values.max():.3f} m")
            else:
                # Plan shots carry a rod reading and no GNSS height at all;
                # printing "nan-nan" reads as a fault rather than a fact.
                parts.append("no height yet")
        return "  |  ".join(parts)


def elevation_column(ps: PointSet) -> str:
    """Which column carries the best available height.

    Prefer the derived elevation once a vertical model has been applied, and
    fall back to raw ellipsoidal height otherwise. Everything downstream asks
    for this rather than hardcoding a column, so applying a datum automatically
    propagates to surfacing, export, and the views.
    """
    return ELEV if ELEV in ps.df.columns else Z


def concat(sets: list[PointSet], layer: str = "merged") -> PointSet:
    """Combine point sets, keeping the union of columns."""
    if not sets:
        raise ValueError("nothing to concatenate")
    df = pd.concat([s.df for s in sets], ignore_index=True, sort=False)
    return PointSet(df=df, layer=layer,
                    history=(f"concat({len(sets)} sets)",))
