"""SW Maps field projects: the plan going out to the phone, and the rules for
reading what comes back.

Export ▸ SW Maps field project writes a `.swmz` that SW Maps imports as a
project of its own, one per outing. It holds three point layers:

    plan            every outstanding plan shot at its best-known position,
                    labelled with its station ("P12"). Reference only: find it
                    with Stake Out, which gives the distance and bearing to it.
                    Nothing is recorded into it, and it is dropped on import.
    shots           where every reading is recorded, as a NEW point: with
                    GNSS, or by tapping your position on the map under
                    canopy. The station is picked from a list (the plan's
                    shots, the control marks, turning points), so a typo can
                    no longer file a reading against the wrong point.
    control checks  the fixed-height pole on a control mark at the start and
                    end of every outing. GNSS only: a check shot means
                    something only as a GNSS height.

Readings go into new points rather than into the plan's: a pre-filled point
keeps the time it was generated and the position it was planned at, and the
time order of readings (a blank setup means "the one before") and a measured
position are the two things a reading must bring back.

The file format was worked out from projects SW Maps wrote itself, and the
table definitions below are SW Maps' own, verbatim. A project is a zip
holding `Projects/<name>.swm2`, a SQLite database with `user_version` 117.
Every attribute value is stored as text; a blank one has no row at all; a
point tapped on the map has fix quality 0 and an elevation of 0; SW Maps
names the project after the file and gives a record no name of its own.
"""

from __future__ import annotations

import math
import sqlite3
import string
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

from ..model.pointset import FIX, KIND, ROD_IN, STATION, TRACK, Z

# Layers and fields. The readers map these names; nothing else should spell
# them out.
PLAN_LAYER = "plan"
SHOTS_LAYER = "shots"
CHECKS_LAYER = "control checks"
STATION_FIELD = "station"
PURPOSE_FIELD = "purpose"
ROD_FIELD = "rod (in)"
SETUP_FIELD = "setup"
TYPE_FIELD = "type"
MARK_FIELD = "mark"

# What a record in the control checks layer is, once read.
CHECK_KIND = "control check"

TURNING_POINTS = ("TP1", "TP2", "TP3")
# Letters offered beyond the plan's own setups, for a setup nobody planned.
SPARE_SETUPS = 3

# `type`, for a shot the plan does not know. A planned shot's type is the
# plan's purpose for it, whatever is picked on the phone. Terrain first.
UNPLANNED_TYPES = ("spot", "breakline", "swale bottom", "crown", "top of slope",
                   "toe of slope", "drainage outlet", "building corner",
                   "foundation", "driveway edge", "sidewalk edge",
                   "curb flowline", "fence", "tree", "utility", "monument",
                   "other")

# SW Maps' own table definitions, in the order it creates them.
USER_VERSION = 117
SCHEMA = (
    "CREATE TABLE android_metadata (locale TEXT)",
    "CREATE TABLE project_info(attr TEXT UNIQUE,value TEXT)",
    "CREATE TABLE project_attributes(attr TEXT UNIQUE NOT NULL,value TEXT,"
    "data_type TEXT,field_choices TEXT,required_field INTEGER,field_length INTEGER)",
    "CREATE TABLE feature_layers(uuid TEXT UNIQUE NOT NULL,name TEXT UNIQUE NOT NULL,"
    "group_name TEXT,geom_type TEXT,point_symbol TEXT,color INTEGER,fill_color INTEGER,"
    "line_width INTEGER,label_field_id TEXT,active INTEGER,drawn INTEGER,png_symbol BLOB,"
    "z_index INTEGER)",
    "CREATE TABLE attribute_fields(uuid TEXT UNIQUE NOT NULL,layer_id TEXT,"
    "field_name TEXT,data_type TEXT,field_choices TEXT,seq INTEGER)",
    "CREATE TABLE attribute_values(item_id TEXT,field_id TEXT,data_type TEXT,value TEXT)",
    "CREATE TABLE features(uuid TEXT UNIQUE NOT NULL,layer_id TEXT,name TEXT,remarks TEXT)",
    "CREATE TABLE points(uuid TEXT UNIQUE NOT NULL,fid TEXT,seq NUMBER,lat NUMBER,"
    "lon NUMBER,elv  NUMBER,ortho_ht  NUMBER,time NUMBER,start_time NUMBER,"
    "instrument_ht NUMBER,fix_quality NUMBER,speed NUMBER,snap_id TEXT,bearing NUMBER,"
    "accuracy_h NUMBER,accuracy_v NUMBER,pos_data TEXT,additional_data TEXT)",
    "CREATE TABLE photos(uuid TEXT UNIQUE NOT NULL,remarks TEXT,photo_path TEXT)",
    "CREATE TABLE external_layers(uuid TEXT UNIQUE NOT NULL,name TEXT,source_type TEXT,"
    "full_path TEXT,z_index NUMBER,active NUMBER,wms_layer_name TEXT,gpkg_layers TEXT,"
    "cache NUMBER,transparency REAL)",
    "CREATE TABLE layer_style(layer_id TEXT,table_name TEXT,field TEXT,value TEXT,"
    "point_shape TEXT,color INTEGER,fill_color INTEGER,line_width INTEGER,label_field TEXT)",
    "CREATE TABLE tracks(uuid TEXT UNIQUE NOT NULL,name TEXT UNIQUE NOT NULL,"
    "color TEXT,description TEXT)",
)

# A point placed by tapping the map, as SW Maps writes one: every column but
# the ids, the position and the time.
TAPPED = {"seq": 0, "elv": 0, "ortho_ht": 0, "instrument_ht": 0, "fix_quality": 0,
          "speed": 0, "snap_id": "", "bearing": 0, "accuracy_h": 0, "accuracy_v": 0,
          "pos_data": "", "additional_data": ""}

# The plan view's pens: guide purple for the plan, terrain red for shots,
# control green for the checks. SW Maps fills at the outline colour, 0x50 alpha.
PLAN_PEN, SHOT_PEN, CHECK_PEN = 0x783CA0, 0xC0392B, 0x2B7A3D


@dataclass
class FieldProject:
    """What an export wrote."""

    path: Path
    name: str
    outstanding: list[int] = field(default_factory=list)
    stations: list[str] = field(default_factory=list)
    setups: list[str] = field(default_factory=list)
    marks: list[str] = field(default_factory=list)


def _argb(rgb: int, alpha: int = 0xFF) -> int:
    """An Android colour int: ARGB, stored signed."""
    v = (alpha << 24) | rgb
    return v - (1 << 32) if v >= 1 << 31 else v


def _id() -> str:
    return str(uuid.uuid4())


# --- what goes out -----------------------------------------------------------------

def outstanding(plan) -> list:
    """Plan shots still to visit, in number order.

    A shot with no rod reading yet, or an outline corner that has not been
    located - its reading may be done, but its position is the point of it.
    Tie-transect guides are walked, not visited, and never go.
    """
    from ..plan import is_guide

    corners = {n for ln in plan.lines if ln.is_outline for n in ln.numbers}
    out = []
    for p in sorted(plan.points, key=lambda q: q.number):
        if is_guide(p.purpose):
            continue
        try:
            located = plan.resolve(p.number)[2] != "planned"
        except (KeyError, ValueError):
            located = False
        if not p.has_reading or (p.number in corners and not located):
            out.append(p)
    return out


def station_choices(plan, marks=()) -> list[str]:
    """Every plan shot - so a doubtful reading can be taken again - then the
    control marks, then the turning points."""
    from ..plan import is_guide

    shots = [f"P{p.number}" for p in sorted(plan.points, key=lambda q: q.number)
             if not is_guide(p.purpose)]
    return shots + [str(m) for m in marks] + list(TURNING_POINTS)


def setup_choices(plan) -> list[str]:
    """The plan's laser setups, any other setup a reading was typed against,
    and a few spare letters for one nobody planned."""
    named = [s.name for s in plan.setups]
    typed = sorted({p.setup for p in plan.points if p.setup} - set(named))
    known = named + typed
    spare = [c for c in string.ascii_uppercase if c not in known][:SPARE_SETUPS]
    return known + spare


def default_name(site, made: date | None = None) -> str:
    return f"{site.name} {(made or date.today()):%Y-%m-%d}"


def write_field_project(plan, site, path: str | Path, *, marks=(),
                        name: str | None = None,
                        made: date | None = None) -> FieldProject:
    """Write a SW Maps field project for the next outing.

    `marks` are the site's control marks. Without any there is nothing to
    check against, so the control checks layer is left out.
    """
    from pyproj import Transformer

    made = made or date.today()
    name = name or default_name(site, made)
    path = Path(path)
    marks = [str(m) for m in marks]
    todo = outstanding(plan)
    result = FieldProject(path=path, name=name,
                          outstanding=[p.number for p in todo],
                          stations=station_choices(plan, marks),
                          setups=setup_choices(plan), marks=marks)

    to_ll = Transformer.from_crs(f"EPSG:{site.epsg}", "EPSG:4326", always_xy=True)
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / f"{name}.swm2"
        con = sqlite3.connect(db)
        try:
            _fill(con, plan, result, todo, to_ll, made)
            con.commit()
        finally:
            con.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(db, f"Projects/{name}.swm2")
    return result


def _fill(con, plan, result: FieldProject, todo, to_ll, made: date) -> None:
    for sql in SCHEMA:
        con.execute(sql)
    con.execute(f"PRAGMA user_version = {USER_VERSION}")
    con.execute("INSERT INTO android_metadata VALUES ('en_US')")
    con.executemany("INSERT INTO project_info VALUES (?, ?)", [
        ("__chInterval__", "50"), ("__uuid__", _id()),
        ("template_name", result.name), ("template_author", "Yard Survey")])
    con.executemany("INSERT INTO project_attributes VALUES (?, ?, ?, ?, ?, ?)", [
        ("plan", f"{plan.name}, exported {made:%Y-%m-%d}", "TEXT", "", 0, 0),
        ("observer", "", "TEXT", "", 0, 0)])

    layers, fields = [], []

    def layer(name, symbol, pen, drawn, z_index, label_field):
        uid = _id()
        layers.append((uid, name, "", "POINT", symbol, _argb(pen), _argb(pen, 0x50),
                       4, label_field, 1, drawn, None, z_index))
        return uid

    def add_field(layer_id, name, kind, choices=(), seq=0):
        uid = _id()
        fields.append((uid, layer_id, name, kind, "||".join(choices), seq))
        return uid

    plan_station, shot_station, mark = _id(), _id(), _id()
    plan_layer = layer(PLAN_LAYER, "PLUS", PLAN_PEN, 0, 9001, plan_station)
    fields.append((plan_station, plan_layer, STATION_FIELD, "TEXT", "", 0))
    plan_purpose = add_field(plan_layer, PURPOSE_FIELD, "TEXT", seq=1)

    shots = layer(SHOTS_LAYER, "CIRCLE_OUTLINE", SHOT_PEN, 1, 9002, shot_station)
    fields.append((shot_station, shots, STATION_FIELD, "OPTIONS",
                   "||".join(result.stations), 0))
    add_field(shots, ROD_FIELD, "TEXT", seq=1)
    add_field(shots, SETUP_FIELD, "OPTIONS", result.setups, seq=2)
    add_field(shots, TYPE_FIELD, "OPTIONS", UNPLANNED_TYPES, seq=3)

    if result.marks:
        checks = layer(CHECKS_LAYER, "TRIANGLE_OUTLINE", CHECK_PEN, 0, 9003, mark)
        fields.append((mark, checks, MARK_FIELD, "OPTIONS", "||".join(result.marks), 0))

    con.executemany("INSERT INTO feature_layers VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", layers)
    con.executemany("INSERT INTO attribute_fields VALUES (?, ?, ?, ?, ?, ?)", fields)

    # The plan's points, as if tapped on the map at their best-known positions.
    now = int(time.time() * 1000)
    columns = ["uuid", "fid", "lat", "lon", "time", "start_time", *TAPPED]
    insert = (f"INSERT INTO points ({', '.join(columns)}) "
              f"VALUES ({', '.join('?' * len(columns))})")
    for i, p in enumerate(todo):
        fid, station = _id(), f"P{p.number}"
        lon, lat = to_ll.transform(*plan.drawn_position(p.number))
        con.execute("INSERT INTO features VALUES (?, ?, ?, ?)",
                    (fid, plan_layer, station, ""))
        con.execute(insert, [_id(), fid, lat, lon, now + i, now + i, *TAPPED.values()])
        con.executemany("INSERT INTO attribute_values VALUES (?, ?, ?, ?)", [
            (fid, plan_station, "TEXT", station),
            (fid, plan_purpose, "TEXT", p.purpose)])


# --- what comes back ----------------------------------------------------------------

def _key(label: str) -> str:
    """A layer label as both readers can produce it: "control checks" from a
    CSV file name, "control_checks" from a project database."""
    return str(label).strip().lower().replace(" ", "_")


def _blank(s: pd.Series) -> pd.Series:
    return s.isna() | s.astype(str).str.strip().eq("")


def apply_conventions(layers: dict) -> list[str]:
    """Read a field project's layers by what they are. Edits `layers` in
    place and returns notes for the user.

    * The `plan` layer is a copy of the plan, not field data: it goes - and
      so do its rows in a CSV export's FEATURE_POINTS, which repeats every
      feature layer and names each row's layer.
    * A point tapped on the map (fix quality 0) has no height. SW Maps
      writes 0 m, which would pass for a GNSS height a few hundred metres
      under the lawn.
    * A `shots` record with nothing in it - no station, no rod reading, no
      type, no remarks - is a stray tap, and is left out, and counted.
    * A `control checks` record is a check shot, and nothing else is.
    """
    notes = []
    for label in list(layers):
        key = _key(label)
        if key == _key(PLAN_LAYER):
            del layers[label]
            continue
        ps = layers[label]
        d = ps.df
        if key == "feature_points" and TRACK in d.columns:
            planned = d[TRACK].map(lambda v: isinstance(v, str)
                                   and _key(v) == _key(PLAN_LAYER))
            if planned.any():
                ps = ps.select(~planned.to_numpy(), "not the plan")
                d = ps.df
        if key != "track_points" and FIX in d.columns and Z in d.columns:
            tapped = pd.to_numeric(d[FIX], errors="coerce").eq(0).to_numpy()
            if tapped.any():
                d = d.copy()
                d.loc[tapped, Z] = math.nan
                ps = ps.with_frame(d, "tapped points have no height")
        if key == _key(SHOTS_LAYER):
            d = ps.df
            empty = pd.Series(True, index=d.index)
            for column in (STATION, ROD_IN, KIND, "remarks", "notes"):
                if column in d.columns:
                    empty &= _blank(d[column])
            if empty.any():
                ps = ps.select(~empty.to_numpy(), "not empty")
                count = int(empty.sum())
                notes.append(
                    f"{count} empty record{'s' if count != 1 else ''} in "
                    f"{label} (no station, rod reading, type or remarks) "
                    f"{'were' if count != 1 else 'was'} left out - a stray tap "
                    "on the map records one.")
        if key == _key(CHECKS_LAYER):
            d = ps.df.copy()
            d[KIND] = CHECK_KIND
            ps = ps.with_frame(d, "control checks")
        layers[label] = ps
    return notes
