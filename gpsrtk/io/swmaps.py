"""Reader for SW Maps exports.

A SW Maps export is a zip containing:

    <project>_TRACK_POINTS.csv     continuous logging, one row per fix
    <project>_TRACKS.csv           track line geometry (not points)
    <project>_FEATURE_POINTS.csv   all feature-layer points, no custom columns
    <project>_PHOTOS.csv           photo locations
    <project>_<layer>.csv          one file per user-defined layer, WITH that
                                   layer's custom attribute columns appended

That last pattern is why layer detection is by exclusion rather than by a fixed
list: "spots" is a user-chosen layer name on this project, not part of the
format. A different project will produce different filenames.

Two column quirks this handles:

  * spot layers use `Latitude`/`Longitude`; track points use `Lat`/`Lon`.
  * `<project>_<layer>.csv` carries custom attributes that the equivalent rows
    in `_FEATURE_POINTS.csv` do not, so the layer files are authoritative.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pandas as pd

from ..model.pointset import (
    PointSet, E, N, Z, LAT, LON, TIME, TZ, FIX, HACC, VACC, SPEED, BEARING,
    PDOP, HDOP, VDOP, SATS_VIEW, SATS_USED, ANT_HT, ROD_IN, SETUP, KIND,
    STATION, TRACK, SOURCE, SESSION,
)
from .base import (SurveyExport, SurveyReader, register_reader, normalise_kind,
                   numeric, parse_time, rod_readings)

# export column -> canonical name
ALIASES = {
    "X": E,
    "Y": N,
    "Elevation": Z,
    "Lat": LAT, "Latitude": LAT,
    "Lon": LON, "Longitude": LON,
    "Fix ID": FIX,
    "Horizontal Accuracy": HACC,
    "Vertical Accuracy": VACC,
    "Speed": SPEED,
    "Bearing": BEARING,
    "PDOP": PDOP, "HDOP": HDOP, "VDOP": VDOP,
    "Satellites in View": SATS_VIEW,
    "Satellites in Use": SATS_USED,
    "Instrument Ht": ANT_HT,
    "Ortho Height": "ortho_h_m",
    "Track Name": TRACK,
    "Layer": TRACK,
    "Feature Name": "feature_name",
    "Time": TIME,
    "ID": "point_id",
    "Remarks": "remarks",
    # spot-layer custom attributes
    "height number": ROD_IN,
    "base position": SETUP,
    "type": KIND,
    "station": STATION,
    "notes": "notes",
}

NUMERIC_COLS = (E, N, Z, LAT, LON, FIX, HACC, VACC, SPEED, BEARING,
                PDOP, HDOP, VDOP, SATS_VIEW, SATS_USED, ANT_HT,
                ROD_IN, SETUP, "ortho_h_m", "point_id")

# Fixed-purpose files. Everything else in the zip is a user-defined layer.
TRACK_POINTS = "TRACK_POINTS"
RESERVED = {TRACK_POINTS, "TRACKS", "FEATURE_POINTS", "PHOTOS"}

# `Geometry` duplicates X/Y/Elevation as WKT; nothing downstream reads it.
DROP = ("Geometry",)


def assign_sessions(df: pd.DataFrame, prefix: str) -> pd.Series:
    """Label each row with the acquisition session it belongs to.

    A session is one continuous outing, which is the granularity at which the
    vertical offset is constant: the antenna was mounted once, the base was
    selected once, the rod was measured once. The acquisition date is the
    practical proxy. The export name is kept as a prefix so two properties, or
    two exports made on the same day, do not collide.

    Getting this granularity right matters: labelling a whole export as one
    session would hide a genuine offset between visits, and labelling every
    track separately would invent offsets that do not exist.
    """
    if TIME in df.columns and df[TIME].notna().any():
        dates = df[TIME].dt.strftime("%Y-%m-%d")
        return (prefix + "/" + dates).fillna(prefix)
    return pd.Series([prefix] * len(df), index=df.index)


def normalise(raw: pd.DataFrame, source: str, session: str) -> pd.DataFrame:
    df = raw.copy()
    df.columns = [str(c).strip() for c in df.columns]
    df = df.drop(columns=[c for c in DROP if c in df.columns])
    df = df.rename(columns={k: v for k, v in ALIASES.items() if k in df.columns})

    if TIME in df.columns:
        df[TIME], df[TZ] = parse_time(df[TIME])

    if ROD_IN in df.columns:
        df[ROD_IN] = rod_readings(df[ROD_IN])
    numeric(df, NUMERIC_COLS)
    if "point_id" in df.columns:
        df["point_id"] = df["point_id"].astype("Int64")
    if KIND in df.columns:
        df[KIND] = normalise_kind(df[KIND])

    df[SOURCE] = source
    df[SESSION] = assign_sessions(df, session)
    return df


class SWMapsReader(SurveyReader):
    name = "SW Maps"

    def can_read(self, path: Path) -> bool:
        if path.is_dir():
            return any(path.glob("*_TRACK_POINTS.csv")) or any(path.glob("*.csv"))
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as z:
                return any(n.lower().endswith(".csv") for n in z.namelist())
        return path.suffix.lower() == ".csv"

    def read(self, path: Path) -> SurveyExport:
        path = Path(path)
        stem = path.stem
        exp = SurveyExport(name=stem, source_path=path)

        frames = list(self._frames(path))
        prefix = self._project_prefix([name for name, _ in frames], stem)
        for filename, frame in frames:
            label = self._layer_label(filename, prefix)
            frame = normalise(frame, source=filename, session=stem)

            # A layer is point data only if it actually carries coordinates.
            # `_TRACKS.csv` holds line geometry and has no X/Y.
            if {E, N, Z}.issubset(frame.columns):
                exp.layers[label] = PointSet(df=frame, layer=label,
                                             history=(f"read {filename}",))
            else:
                exp.tables[label] = frame
        return exp

    @staticmethod
    def _project_prefix(filenames: list[str], stem: str) -> str:
        """The project name SW Maps put in front of every file in the export.

        Read from the fixed-purpose files rather than taken from the zip's
        own name, which is whatever the file was saved or downloaded as: a
        browser's "Project 1 (1).zip" still holds "Project 1_TRACK_POINTS.csv".
        Labelling from the zip name turned a renamed export's layers into
        "Project 1_TRACK_POINTS", which never merged with `track_points`.
        """
        found: dict[str, int] = {}
        for name in filenames:
            base = Path(name).stem
            for reserved in RESERVED:
                tail = "_" + reserved
                if base.upper().endswith(tail) and len(base) > len(tail):
                    prefix = base[:-len(tail)]
                    found[prefix] = found.get(prefix, 0) + 1
        return max(found, key=found.get) if found else stem

    @staticmethod
    def _layer_label(filename: str, prefix: str) -> str:
        base = Path(filename).stem
        if base.startswith(prefix + "_"):
            base = base[len(prefix) + 1:]
        return base.lower() if base.upper() in RESERVED else base

    @staticmethod
    def _frames(path: Path):
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as z:
                for n in sorted(z.namelist()):
                    if n.lower().endswith(".csv"):
                        with z.open(n) as fh:
                            yield Path(n).name, pd.read_csv(fh, encoding="utf-8")
        elif path.is_dir():
            for p in sorted(path.glob("*.csv")):
                yield p.name, pd.read_csv(p, encoding="utf-8")
        else:
            yield path.name, pd.read_csv(path, encoding="utf-8")


register_reader(SWMapsReader())
