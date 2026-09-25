"""Reader for SW Maps `.swmz` project archives.

This is SW Maps' own save format rather than its CSV export, and it carries
more than the export does:

    Projects/<name>.swm2     SQLite - the live project database
    RawData/<stamp>.log      the receiver's raw stream, if raw logging was on

What the CSV export throws away and this keeps:

  * a `pos_data` JSON blob per point, holding age of differential, the
    reference station id, and the baseline length to it. Those say why a fix
    was good or bad, which the accuracy columns only hint at.
  * the real logging rate. The export appears to decimate; the September 2026
    project holds 35,317 points at 10 Hz where its CSV sibling of a comparable
    outing held 8,194 at 2.25 Hz.

What it does NOT carry is projected coordinates: the database stores lat/lon
only, so this reader projects. Checked against the CSV export of the same
project, which carries both, pyproj reproduces SW Maps' own X/Y to 0.5 mm -
which is the rounding of the exported three-decimal metres, so the two paths
agree exactly.

Times are stored as epoch milliseconds with no zone. They are converted using
the machine's local zone - with the daylight-saving rule for each timestamp's
own date - because that is what the device was displaying when
the survey was recorded and what the CSV export writes out. A project surveyed
in one zone and processed in another would therefore land on the wrong local
clock; only session dating depends on it, and crossover separations, which are
differences, do not care.

The raw log is located and reported but not parsed. It is the input for a
future PPK path and there is no point decoding it until there is one.
"""

from __future__ import annotations

import datetime as _dt
import json
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path

import pandas as pd

from ..model.pointset import (
    AGE_DIFF, ANT_HT, BASELINE, BEARING, E, FIX, HACC, HDOP, KIND, LAT, LON,
    N, PDOP, REF_STATION, ROD_IN, SATS_USED, SATS_VIEW, SESSION, SETUP, SOURCE,
    SPEED, STATION, TIME, TRACK, TZ, VACC, VDOP, Z, PointSet,
)
from .base import (SurveyExport, SurveyReader, normalise_kind, numeric,
                   register_reader, rod_readings)
from .swmaps import assign_sessions

SUFFIXES = (".swmz", ".swm2")
TRACK_LAYER = "track_points"

# pos_data key -> canonical column. The DOP and satellite counts line up with
# what the CSV export already provides, so a project assembled from both kinds
# of file has one set of columns rather than two.
POS_DATA = {
    "PDOP": PDOP,
    "HDOP": HDOP,
    "VDOP": VDOP,
    "SatellitesInView": SATS_VIEW,
    "SatellitesInUse": SATS_USED,
    "AgeOfDifferential": AGE_DIFF,
    "ReferenceStationID": REF_STATION,
    "BaselineLength": BASELINE,
}

NUMERIC_COLS = (LAT, LON, Z, FIX, HACC, VACC, SPEED, BEARING, ANT_HT,
                PDOP, HDOP, VDOP, SATS_VIEW, SATS_USED, AGE_DIFF, BASELINE,
                "ortho_h_m")

# Custom attribute names SW Maps projects have used for laser work, mapped to
# the same canonical columns the CSV reader produces from the spot layer.
ATTRIBUTE_ALIASES = {
    "height number": ROD_IN,
    "base position": SETUP,
    "type": KIND,
    "station": STATION,
    "notes": "notes",
}


class SWMapsProjectReader(SurveyReader):
    """Reads a `.swmz` archive, or a `.swm2` database on its own."""

    name = "SW Maps project (.swmz)"

    def can_read(self, path: Path) -> bool:
        if path.suffix.lower() not in SUFFIXES:
            return False
        if path.suffix.lower() == ".swm2":
            return True
        try:
            with zipfile.ZipFile(path) as z:
                return any(n.lower().endswith(".swm2") for n in z.namelist())
        except zipfile.BadZipFile:
            return False

    def read(self, path: Path) -> SurveyExport:
        path = Path(path)
        exp = SurveyExport(name=path.stem, source_path=path)

        with _database(path) as (db, raw_logs):
            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            try:
                points = _read_points(con)
                labels = _layer_labels(con)
                attrs = _attributes(con)
            finally:
                con.close()

        if points.empty:
            raise ValueError(
                f"{path.name} contains no points. SW Maps writes the project "
                "database on export, so an archive made before anything was "
                "recorded will look like this.")

        points = _finish(points, labels, attrs, stem=path.stem,
                         source=path.name)

        for label, frame in points.groupby("_layer", sort=False):
            frame = frame.drop(columns=["_layer"]).reset_index(drop=True)
            exp.layers[str(label)] = PointSet(
                df=frame, layer=str(label),
                history=(f"read {path.name} ({label})",))

        # Recorded, not parsed. Raw observations are the input to a PPK path
        # that does not exist yet, and a reader that silently ignored 47 MB of
        # carrier phase would be hiding the most useful thing in the file.
        if raw_logs:
            exp.tables["raw_logs"] = pd.DataFrame(
                {"member": raw_logs,
                 "note": ["raw receiver stream; not parsed"] * len(raw_logs)})
        return exp


# --- archive handling -----------------------------------------------------

class _database:
    """Yield a path to the .swm2 on disk, plus the names of any raw logs.

    SQLite needs a real file, so a database inside the archive is extracted to
    a temporary one. Only the database is extracted - the raw log is tens of
    megabytes and nothing here reads it.
    """

    def __init__(self, path: Path):
        self.path = path
        self.tmp: Path | None = None

    def __enter__(self):
        if self.path.suffix.lower() == ".swm2":
            return self.path, []

        with zipfile.ZipFile(self.path) as z:
            names = z.namelist()
            dbs = [n for n in names if n.lower().endswith(".swm2")]
            if not dbs:
                raise ValueError(f"{self.path.name} contains no .swm2 database")
            raw = [n for n in names if n.lower().startswith("rawdata/")
                   and not n.endswith("/")]

            self.tmp = Path(tempfile.mkdtemp(prefix="swmz_")) / "project.swm2"
            with z.open(dbs[0]) as src, open(self.tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
        return self.tmp, raw

    def __exit__(self, *exc):
        if self.tmp is not None:
            shutil.rmtree(self.tmp.parent, ignore_errors=True)
        return False


# --- database -------------------------------------------------------------

def _table_exists(con, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _read_points(con) -> pd.DataFrame:
    if not _table_exists(con, "points"):
        raise ValueError("no `points` table; this is not a SW Maps project")
    return pd.read_sql_query("SELECT * FROM points ORDER BY time, seq", con)


def _layer_labels(con) -> dict[str, tuple[str, str]]:
    """Map each `points.fid` to (layer label, track/feature name).

    A point's `fid` is a track when it came from continuous logging and a
    feature when it was a recorded shot. Both are resolved here so that the
    caller does not have to care which table a row came from.
    """
    out: dict[str, tuple[str, str]] = {}
    if _table_exists(con, "tracks"):
        for r in con.execute("SELECT uuid, name FROM tracks"):
            out[r["uuid"]] = (TRACK_LAYER, r["name"] or "")

    if _table_exists(con, "features") and _table_exists(con, "feature_layers"):
        layers = {r["uuid"]: (r["name"] or "layer")
                  for r in con.execute("SELECT uuid, name FROM feature_layers")}
        for r in con.execute("SELECT uuid, layer_id, name FROM features"):
            label = layers.get(r["layer_id"], "features")
            out[r["uuid"]] = (_clean_label(label), r["name"] or "")
    return out


def _attributes(con) -> pd.DataFrame:
    """Custom per-feature attributes, pivoted to one row per item.

    SW Maps keys these by the item the attribute belongs to. That is the
    feature for a recorded shot, but projects have also been seen keying them
    by the point, so both are returned and the caller joins on whichever
    matches. Returns an empty frame when the project defines no attributes,
    which is the case for a pure tracking project.
    """
    if not (_table_exists(con, "attribute_fields")
            and _table_exists(con, "attribute_values")):
        return pd.DataFrame()

    fields = pd.read_sql_query(
        "SELECT uuid, field_name FROM attribute_fields", con)
    values = pd.read_sql_query(
        "SELECT item_id, field_id, value FROM attribute_values", con)
    if fields.empty or values.empty:
        return pd.DataFrame()

    merged = values.merge(fields, left_on="field_id", right_on="uuid",
                          how="left")
    merged["field_name"] = merged["field_name"].fillna(merged["field_id"])
    wide = merged.pivot_table(index="item_id", columns="field_name",
                              values="value", aggfunc="first")
    wide.columns = [ATTRIBUTE_ALIASES.get(str(c), str(c)) for c in wide.columns]
    return wide


# --- normalisation --------------------------------------------------------

def _clean_label(name: str) -> str:
    return str(name).strip().lower().replace(" ", "_") or "layer"


def _local_times(ms: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Epoch milliseconds to naive local time, plus the zone abbreviation.

    Naive local is what the CSV reader produces, so the two paths can be
    concatenated without one set of timestamps sitting hours from the other.

    Each timestamp is converted with the local rule for its own date, not
    the offset in force today: a January outing processed in July used to
    come out an hour late and labelled "CDT". The offset is looked up once
    per whole minute, since zones change offset only on the minute, which
    keeps a 10 Hz outing to a few dozen lookups.
    """
    ms = pd.to_numeric(ms, errors="coerce")
    minute = (ms // 60_000) * 60
    offsets: dict[float, float] = {}
    names: dict[float, str] = {}
    for m in minute.dropna().unique():
        local = _dt.datetime.fromtimestamp(m, _dt.timezone.utc).astimezone()
        offsets[m] = local.utcoffset().total_seconds()
        names[m] = _abbreviate(local.tzname() or "")
    utc = pd.to_datetime(ms, unit="ms")
    shift = pd.to_timedelta(minute.map(offsets), unit="s")
    return utc + shift, minute.map(names)


def _abbreviate(zone: str) -> str:
    """"Central Daylight Time" -> "CDT".

    Windows reports zone names in full where POSIX gives the abbreviation, and
    the CSV export writes the abbreviation. Two spellings of the same zone in
    one `tz` column would look like two zones.
    """
    if not zone or " " not in zone:
        return zone or ""
    return "".join(word[0] for word in zone.split() if word).upper()


def _expand_pos_data(df: pd.DataFrame) -> pd.DataFrame:
    if "pos_data" not in df.columns:
        return df
    parsed = df["pos_data"].apply(_maybe_json)
    if not parsed.map(bool).any():
        return df.drop(columns=["pos_data"])

    extra = pd.DataFrame(list(parsed), index=df.index)
    extra = extra[[c for c in extra.columns if c in POS_DATA]]
    extra = extra.rename(columns=POS_DATA)
    # Never let the blob overwrite a real column that is already populated.
    extra = extra[[c for c in extra.columns if c not in df.columns]]
    return pd.concat([df.drop(columns=["pos_data"]), extra], axis=1)


def _maybe_json(value):
    if not value:
        return {}
    try:
        out = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def _finish(points: pd.DataFrame, labels: dict, attrs: pd.DataFrame,
            *, stem: str, source: str) -> pd.DataFrame:
    df = points.rename(columns={
        "elv": Z, "ortho_ht": "ortho_h_m", "fix_quality": FIX,
        "accuracy_h": HACC, "accuracy_v": VACC,
        "instrument_ht": ANT_HT, "lat": LAT, "lon": LON,
    })

    df["_layer"] = df["fid"].map(lambda f: labels.get(f, ("points", ""))[0])
    df[TRACK] = df["fid"].map(lambda f: labels.get(f, ("points", ""))[1])
    # A recorded shot's name is its feature name, as in the CSV export; a
    # plan shot recorded as "P12" is matched to its station by it.
    if (df["_layer"] != TRACK_LAYER).any():
        df["feature_name"] = df[TRACK].where(df["_layer"] != TRACK_LAYER)

    df[TIME], df[TZ] = _local_times(df["time"])
    df = _expand_pos_data(df)
    numeric(df, NUMERIC_COLS)

    if not attrs.empty:
        joined = df.join(attrs, on="fid")
        missing = [c for c in attrs.columns if joined[c].isna().all()]
        if missing:                       # keyed by point rather than feature
            joined = joined.drop(columns=missing).join(attrs[missing], on="uuid")
        df = joined
        if ROD_IN in df.columns:
            df[ROD_IN] = rod_readings(df[ROD_IN])
        numeric(df, (ROD_IN, SETUP))
        if KIND in df.columns:
            df[KIND] = normalise_kind(df[KIND])

    df[E], df[N] = _project(df[LAT], df[LON])
    df[SOURCE] = source
    df[SESSION] = assign_sessions(df, stem)

    drop = [c for c in ("fid", "snap_id", "additional_data", "start_time")
            if c in df.columns]
    return df.drop(columns=drop)


def _project(lat: pd.Series, lon: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Project to the UTM zone the data sits in.

    The zone comes from the data rather than from a configured site, because a
    reader has no site: it is handed a file. `AppState` checks the result
    against the project's CRS on load and says so if they disagree.
    """
    from pyproj import Transformer

    from ..site import Site

    lat = pd.to_numeric(lat, errors="coerce")
    lon = pd.to_numeric(lon, errors="coerce")
    good = lat.notna() & lon.notna()
    if not good.any():
        raise ValueError("no usable lat/lon in the project database")

    epsg = Site.utm_epsg(float(lat[good].mean()), float(lon[good].mean()))
    tx = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    e, n = tx.transform(lon.to_numpy(), lat.to_numpy())
    return pd.Series(e, index=lat.index), pd.Series(n, index=lat.index)


register_reader(SWMapsProjectReader())
