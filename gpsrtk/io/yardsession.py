"""Reader for `.yardsession` files from the Yard Survey Android app.

The app (repository `android-gps-survey`) replaces SW Maps. It owns the
receiver's USB link, logs every byte from the receiver and the NTRIP caster,
and exports a session as a zip:

    manifest.json          format, version, session, IANA time zone, mount
    track_points.csv       one row per receiver epoch (10 Hz), canonical names
    shots.csv              rod readings: station, rod_text (inches as typed),
                           setup, kind, remarks
    control_checks.csv     the fixed-height pole on a mark: mark, remarks
    events.jsonl.gz        the app's event log
    session.json, raw/...  the session as recorded: gnss.bin (NMEA and the
                           receiver's own RTCM3 MSM7), corrections.rtcm3, and a
                           timing index for each

The CSVs already use this package's column names, so reading is mostly
projection, time and the field-project layer rules. Positions are WGS84
lat/lon and are projected as `.swmz` points are.

Times are UTC epoch milliseconds from the receiver itself, not the phone's
clock. They are converted to naive local time in the zone the manifest names,
with each timestamp's own daylight-saving rule, which is what the other
readers produce.

The raw streams are listed in `tables["raw_logs"]` and not parsed: they are
the input to the PPK path, when there is one.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pandas as pd

from ..model.pointset import (
    E, KIND, LAT, LON, N, REF_STATION, ROD_IN, SESSION, SOURCE, STATION,
    TIME, TRACK, TZ, Z, PointSet,
)
from .base import (SurveyExport, SurveyReader, normalise_kind, numeric,
                   register_reader, rod_readings)
from .swmaps import assign_sessions
from .swmaps_field import CHECKS_LAYER, SHOTS_LAYER, apply_conventions
from .swmaps_project import NUMERIC_COLS, _project

SUFFIX = ".yardsession"
FORMAT = "yardsession"
SUPPORTED_VERSION = 1
TRACK_LAYER = "track_points"

# Member -> layer label. The labels are the field project's, so
# `apply_conventions` treats them exactly as it treats a `.swmz`.
LAYERS = {
    "track_points.csv": TRACK_LAYER,
    "shots.csv": SHOTS_LAYER,
    "control_checks.csv": CHECKS_LAYER,
}

# Columns the app names differently from this package.
RENAMES = {"mark": STATION}

EXTRA_NUMERIC = ("mono_ns", "avg_n", "avg_sd_m", "avg_sd_h_m")


class YardSessionReader(SurveyReader):
    name = "Yard Survey session (.yardsession)"

    def can_read(self, path: Path) -> bool:
        if path.suffix.lower() != SUFFIX:
            return False
        try:
            with zipfile.ZipFile(path) as z:
                return "manifest.json" in z.namelist()
        except zipfile.BadZipFile:
            return False

    def read(self, path: Path) -> SurveyExport:
        path = Path(path)
        exp = SurveyExport(name=path.stem, source_path=path)
        with zipfile.ZipFile(path) as z:
            manifest = json.loads(z.read("manifest.json"))
            _check(manifest, path)
            tz = manifest.get("tz") or "UTC"
            names = z.namelist()
            for member, label in LAYERS.items():
                if member not in names:
                    continue
                with z.open(member) as f:
                    # Station and setup names are labels: "01" must stay "01".
                    df = pd.read_csv(f, dtype={"station": str, "mark": str,
                                               "setup": str, REF_STATION: str,
                                               "rod_text": str, TRACK: str})
                if df.empty:
                    continue
                df = _finish(df, tz, stem=path.stem, source=path.name)
                exp.layers[label] = PointSet(
                    df=df, layer=label, history=(f"read {path.name} ({label})",))
            raw = [n for n in names if n.startswith("raw/") and not n.endswith("/")]

        if not exp.layers:
            raise ValueError(f"{path.name} holds no points: the receiver had no "
                             "position for the whole session.")
        exp.notes += apply_conventions(exp.layers)
        if manifest.get("mount"):
            exp.notes.append(f"Corrections: NTRIP mount point {manifest['mount']}.")
        if raw:
            exp.tables["raw_logs"] = pd.DataFrame(
                {"member": raw, "note": ["raw stream; not parsed"] * len(raw)})
        return exp


def _check(manifest: dict, path: Path) -> None:
    if manifest.get("format") != FORMAT:
        raise ValueError(f"{path.name} is not a Yard Survey session "
                         f"(format {manifest.get('format')!r})")
    version = manifest.get("version")
    if not isinstance(version, int) or version > SUPPORTED_VERSION:
        raise ValueError(
            f"{path.name} is format version {version}; this reader knows "
            f"version {SUPPORTED_VERSION}. Update gps-survey-plotter.")


def _finish(df: pd.DataFrame, tz: str, *, stem: str, source: str) -> pd.DataFrame:
    df = df.rename(columns=RENAMES)
    df[TIME], df[TZ] = _local_times(df["time_utc_ms"], tz)
    numeric(df, NUMERIC_COLS + EXTRA_NUMERIC)
    if "rod_text" in df.columns:
        df[ROD_IN] = rod_readings(df["rod_text"])
    if KIND in df.columns:
        df[KIND] = normalise_kind(df[KIND])
    if Z not in df.columns:
        df[Z] = float("nan")
    # A shot saved without a position (a turning point under canopy) is still
    # a level-network reading; a layer of nothing else has no position at all.
    if pd.to_numeric(df[LAT], errors="coerce").notna().any():
        df[E], df[N] = _project(df[LAT], df[LON])
    else:
        df[E] = df[N] = float("nan")
    df[SOURCE] = source
    if TRACK not in df.columns or df[TRACK].isna().all():
        df[TRACK] = stem
    df[SESSION] = assign_sessions(df, stem)
    return df


def _local_times(ms: pd.Series, tz: str) -> tuple[pd.Series, pd.Series]:
    """UTC epoch ms to naive local time in `tz`, and the zone abbreviation."""
    utc = pd.to_datetime(pd.to_numeric(ms, errors="coerce"), unit="ms", utc=True)
    local = utc.dt.tz_convert(tz)
    return local.dt.tz_localize(None), local.dt.strftime("%Z")


register_reader(YardSessionReader())
