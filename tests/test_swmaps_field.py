"""SW Maps field projects: what goes out to the phone, and what comes back.

The format was worked out from projects SW Maps wrote, and a project written
here was imported, recorded into and exported again on a phone before this
was built. These tests pin what that round trip showed: the plan layer comes
back untouched and is dropped; readings arrive as new points with their
station picked from a list; a tapped point has fix 0 and an elevation of 0;
a blank field has no row; a stray tap leaves an empty record.
"""

import sqlite3
import time
import uuid
import zipfile

import numpy as np
import pandas as pd
import pytest
from pyproj import Transformer

from gpsrtk.app import AppState
from gpsrtk.filters import KindSelect
from gpsrtk.io import read_any
from gpsrtk.io import swmaps_field as F
from gpsrtk.model.pointset import KIND, ROD_IN, SETUP, STATION, Z
from gpsrtk.plan import TERRAIN_KINDS, Plan, PlannedSetup
from gpsrtk.site import ControlMark, example_site

E0, N0 = 449712.0, 4604565.0            # local (12, 15) on the example site
TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:32615", always_xy=True)
TO_LL = Transformer.from_crs("EPSG:32615", "EPSG:4326", always_xy=True)


@pytest.fixture
def site():
    s = example_site()
    s.control = [ControlMark("BM1"), ControlMark("BM2")]
    return s


@pytest.fixture
def plan():
    """Two spots (one read), a tie transect, and a house of four corners, two
    of them located and read."""
    p = Plan(name="test plan")
    p.add_point(E0, N0)                                     # 1: outstanding
    p.add_point(E0 + 4, N0).rod_in = 52.0                   # 2: done
    p.add_line("tie-EW-1", [(E0, N0 - 6), (E0 + 10, N0 - 6)], kind="transect",
               purpose="tie transect")                      # 3, 4: guides
    house = p.add_outline("house", [(E0 + 6, N0 + 2), (E0 + 14, N0 + 2),
                                    (E0 + 14, N0 + 9), (E0 + 6, N0 + 9)])
    for number in house.numbers[:2]:                        # 5, 6: located, read
        corner = p.by_number(number)
        corner.observed_e, corner.observed_n = corner.planned_e + 0.3, corner.planned_n
        corner.method, corner.fix, corner.rod_in = "rtk", 4, 60.0
    p.by_number(house.numbers[2]).rod_in = 61.0             # 7: read, not located
    return p


def _open(path):
    """The project database inside a written .swmz, and its member name."""
    with zipfile.ZipFile(path) as z:
        (member,) = z.namelist()
        data = z.read(member)
    db = path.with_suffix(".swm2")
    db.write_bytes(data)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    return con, member


def _rows(con, sql, *args):
    return [dict(r) for r in con.execute(sql, args)]


# --- what goes out ---------------------------------------------------------------------

def test_the_project_is_laid_out_as_sw_maps_writes_one(tmp_path, plan, site):
    result = F.write_field_project(plan, site, tmp_path / "out.swmz",
                                   marks=site.mark_names, name="yard 2026-09-25")
    con, member = _open(result.path)
    assert member == "Projects/yard 2026-09-25.swm2"
    assert con.execute("pragma user_version").fetchone()[0] == 117
    assert [r["sql"] for r in _rows(con, "select sql from sqlite_master "
                                         "where type='table' order by rowid")] == list(F.SCHEMA)
    layers = {r["name"]: r for r in _rows(con, "select * from feature_layers")}
    assert list(layers) == ["plan", "shots", "control checks"]
    assert [layers[n]["drawn"] for n in layers] == [0, 1, 0], \
        "shots may be tapped under canopy; a check shot is only a GNSS height"
    fields = _rows(con, "select * from attribute_fields order by layer_id, seq")
    by_id = {f["uuid"]: f for f in fields}
    for name in ("plan", "shots"):
        assert by_id[layers[name]["label_field_id"]]["field_name"] == "station"
    assert by_id[layers["control checks"]["label_field_id"]]["field_name"] == "mark"
    shots = [(f["field_name"], f["data_type"]) for f in fields
             if f["layer_id"] == layers["shots"]["uuid"]]
    assert shots == [("station", "OPTIONS"), ("rod (in)", "TEXT"),
                     ("setup", "OPTIONS"), ("type", "OPTIONS")]
    info = dict(con.execute("select attr, value from project_info").fetchall())
    assert info["template_name"] == "yard 2026-09-25" and info["__chInterval__"] == "50"
    attrs = dict(con.execute("select attr, value from project_attributes").fetchall())
    assert attrs["plan"].startswith("test plan, exported ")


def test_only_outstanding_shots_go_out_to_be_found(tmp_path, plan, site):
    """A shot with no reading, or an outline corner not yet located; never
    a tie-transect guide, which is walked rather than visited."""
    assert [p.number for p in F.outstanding(plan)] == [1, 7, 8]
    result = F.write_field_project(plan, site, tmp_path / "out.swmz",
                                   marks=site.mark_names)
    con, _ = _open(result.path)
    names = [r["name"] for r in _rows(
        con, "select f.name from features f join feature_layers l "
             "on l.uuid = f.layer_id where l.name = 'plan' order by f.rowid")]
    assert names == ["P1", "P7", "P8"] == [f"P{n}" for n in result.outstanding]


def test_plan_points_are_tapped_points_at_their_best_known_position(tmp_path, site):
    plan = Plan()
    plan.add_point(E0, N0, purpose="swale bottom")
    moved = plan.add_outline("shed", [(E0 + 2, N0), (E0 + 5, N0), (E0 + 5, N0 + 3)],
                             kind="shed")
    corner = plan.by_number(moved.numbers[0])            # located, not yet read
    corner.observed_e, corner.observed_n, corner.method = E0 + 2.5, N0 - 0.5, "rtk"
    result = F.write_field_project(plan, site, tmp_path / "out.swmz")
    con, _ = _open(result.path)
    points = _rows(con, "select p.*, f.name from points p join features f "
                        "on f.uuid = p.fid order by p.rowid")
    values = {r["value"] for r in _rows(con, "select * from attribute_values")}
    assert [p["name"] for p in points] == ["P1", "P2", "P3", "P4"]
    for p in points:
        assert {k: p[k] for k in F.TAPPED} == F.TAPPED
        assert p["time"] == p["start_time"] > 0
    at = [TO_UTM.transform(p["lon"], p["lat"]) for p in points]
    assert at[0] == pytest.approx((E0, N0), abs=1e-3)
    assert at[1] == pytest.approx((E0 + 2.5, N0 - 0.5), abs=1e-3), "where it was measured"
    assert at[2] == pytest.approx((E0 + 5, N0), abs=1e-3), "where it was clicked"
    assert {"swale bottom", "building corner"} <= values


def test_the_pick_lists(tmp_path, plan, site):
    """Every plan shot, so a doubtful reading can be taken again, then the
    marks, then turning points. The plan's setups, any setup a reading was
    typed against, and three spare letters."""
    plan.setups.append(PlannedSetup("A"))
    plan.by_number(2).setup = "0"
    result = F.write_field_project(plan, site, tmp_path / "out.swmz",
                                   marks=site.mark_names)
    assert result.stations == ["P1", "P2", "P5", "P6", "P7", "P8",
                               "BM1", "BM2", "TP1", "TP2", "TP3"]
    assert result.setups == ["A", "0", "B", "C", "D"]
    con, _ = _open(result.path)
    choices = dict(con.execute("select field_name, field_choices from attribute_fields "
                               "where data_type = 'OPTIONS'").fetchall())
    assert choices["station"] == "||".join(result.stations)
    assert choices["setup"] == "A||0||B||C||D"
    assert choices["mark"] == "BM1||BM2"
    assert choices["type"].split("||")[:2] == ["spot", "breakline"]


def test_without_control_marks_there_is_no_checks_layer(tmp_path, plan, site):
    result = F.write_field_project(plan, site, tmp_path / "out.swmz")
    con, _ = _open(result.path)
    assert [r["name"] for r in _rows(con, "select name from feature_layers")] == ["plan", "shots"]
    assert "BM1" not in result.stations


# --- what comes back -------------------------------------------------------------------

def _record(con, layer, values, *, e, n, when, fix=4, z=262.0, remarks=""):
    """Append a record as SW Maps does: a feature, its point, a row per value
    that was filled in. Tapped (fix 0) points get SW Maps' 0 m elevation."""
    layers = {r["name"]: r["uuid"] for r in _rows(con, "select uuid, name from feature_layers")}
    fields = {(r["layer_id"], r["field_name"]): (r["uuid"], r["data_type"])
              for r in _rows(con, "select * from attribute_fields")}
    fid = str(uuid.uuid4())
    con.execute("insert into features values (?, ?, ?, ?)", (fid, layers[layer], "", remarks))
    lon, lat = TO_LL.transform(e, n)
    ms = int(pd.Timestamp(when).tz_localize("America/Chicago").timestamp() * 1000)
    con.execute("insert into points (uuid, fid, seq, lat, lon, elv, ortho_ht, time, "
                "start_time, instrument_ht, fix_quality, speed, snap_id, bearing, "
                "accuracy_h, accuracy_v, pos_data, additional_data) values "
                "(?, ?, 0, ?, ?, ?, 0, ?, ?, 0, ?, 0, '', 0, ?, ?, '', '')",
                (str(uuid.uuid4()), fid, lat, lon, 0 if fix == 0 else z, ms, ms, fix,
                 0 if fix == 0 else 0.014, 0 if fix == 0 else 0.02))
    for name, value in values.items():
        field_id, kind = fields[(layers[layer], name)]
        con.execute("insert into attribute_values values (?, ?, ?, ?)",
                    (fid, field_id, kind, str(value)))
    return fid


def _track(con, *, day, n=40):
    """A short mower track in the same project, as a real outing has."""
    tid = str(uuid.uuid4())
    con.execute("insert into tracks values (?, ?, ?, ?)", (tid, "Track 1", "", ""))
    start = pd.Timestamp(day).tz_localize("America/Chicago")
    for i in range(n):
        lon, lat = TO_LL.transform(449700.5 + 0.3 * i, 4604550.5)
        ms = int((start + pd.Timedelta(seconds=0.3 * i)).timestamp() * 1000)
        con.execute("insert into points (uuid, fid, seq, lat, lon, elv, ortho_ht, time, "
                    "start_time, instrument_ht, fix_quality, speed, snap_id, bearing, "
                    "accuracy_h, accuracy_v, pos_data, additional_data) values "
                    "(?, ?, ?, ?, ?, 262.0, 0, ?, ?, 0, 4, 1.0, '', 90, 0.014, 0.02, '', '')",
                    (str(uuid.uuid4()), tid, i, lat, lon, ms, ms))


def _returned(tmp_path, plan, site, fill, name="field back"):
    """Write a field project, record into it, and zip it up as SW Maps exports."""
    result = F.write_field_project(plan, site, tmp_path / "out.swmz",
                                   marks=site.mark_names, name=name)
    con, member = _open(result.path)
    fill(con)
    con.commit()
    con.close()
    back = tmp_path / f"{name}.swmz"
    with zipfile.ZipFile(back, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(result.path.with_suffix(".swm2"), member)
    return back


def test_a_returned_project_reads_by_its_layers(tmp_path, plan, site):
    def fill(con):
        day = "2026-09-26 10:00:00"
        _record(con, "shots", {"station": "P1", "rod (in)": "63 3/8", "setup": "A"},
                e=E0 + 0.02, n=N0, when=day, remarks="under the maple")
        _record(con, "shots", {"station": "P8", "rod (in)": "5' 1\""},
                e=E0 + 14, n=N0 + 9, when="2026-09-26 10:02:00", fix=0)
        _record(con, "shots", {}, e=E0 + 1, n=N0 + 1, when="2026-09-26 10:03:00", fix=0)
        _record(con, "shots", {}, e=E0 + 1, n=N0 + 1, when="2026-09-26 10:03:01", fix=0)
        _record(con, "control checks", {"mark": "BM1"}, e=E0 - 17, n=N0 - 20,
                when="2026-09-26 09:55:00", z=262.8)

    exp = read_any(_returned(tmp_path, plan, site, fill))
    assert set(exp.layers) == {"shots", "control_checks"}, "the plan layer is dropped"
    shots = exp.layers["shots"].df
    assert list(shots[STATION]) == ["P1", "P8"]
    assert list(shots[ROD_IN]) == [63.375, 61.0]
    assert list(shots[SETUP].fillna("")) == ["A", ""], "a letter is a setup, not NaN"
    assert shots["remarks"].iloc[0] == "under the maple"
    assert shots[Z].iloc[0] == 262.0 and np.isnan(shots[Z].iloc[1]), \
        "a tapped point has no height, not 0 m"
    assert exp.notes == ["2 empty records in shots (no station, rod reading, type or "
                         "remarks) were left out - a stray tap on the map records one."]
    checks = exp.layers["control_checks"].df
    assert list(checks[STATION]) == ["BM1"] and list(checks[KIND]) == [F.CHECK_KIND]


def test_the_outing_fills_the_plan_and_ties_to_its_marks(tmp_path, plan, site, synthetic_zip):
    """Loaded over the survey: readings fill the plan's empty cells, a planned
    shot is what the plan says it is, an unplanned terrain shot is terrain,
    and the pole shots are the outing's check shots."""
    def fill(con):
        day = "2026-09-26 10:00:00"
        _track(con, day=day)
        _record(con, "control checks", {"mark": "BM1"}, e=E0 - 17, n=N0 - 20,
                when="2026-09-26 09:55:00", z=262.8)
        _record(con, "shots", {"station": "P1", "rod (in)": "63 3/8", "setup": "A",
                               "type": "fence"},
                e=E0 + 0.02, n=N0, when="2026-09-26 10:05:00", remarks="soft ground")
        _record(con, "shots", {"station": "P8", "rod (in)": "61 1/2"},
                e=E0 + 14, n=N0 + 9, when="2026-09-26 10:06:00", fix=0)
        _record(con, "shots", {"rod (in)": "58", "type": "swale bottom"},
                e=E0 + 20, n=N0 + 5, when="2026-09-26 10:07:00")
        _record(con, "shots", {"station": "BM1", "rod (in)": "50"},
                e=E0 - 17, n=N0 - 20, when="2026-09-26 10:08:00", z=263.3)
        _record(con, "control checks", {"mark": "BM1"}, e=E0 - 17, n=N0 - 20,
                when="2026-09-26 10:10:00", z=262.8)

    state = AppState(site=site, cache_dir=tmp_path / "cache")
    state.plan = plan
    state.load(synthetic_zip)
    report = state.add_export(_returned(tmp_path, plan, site, fill))

    p1, p8 = plan.by_number(1), plan.by_number(8)
    assert (p1.rod_in, p1.setup, p1.observed_note) == (63.375, "A", "soft ground")
    assert p1.method == "rtk", "a fixed shot gives its position"
    assert (p8.rod_in, p8.method) == (61.5, "planned"), "a tapped one does not"
    assert "P1: rod 63.375 in, setup A" in "\n".join(report.notes)

    spots = state.spots.df
    field = spots[spots["source"] == "field back.swmz"]
    kinds = dict(zip(field[STATION].astype(str), field[KIND]))
    assert kinds["P1"] == "lawn", "P1 is a planned spot, whatever type was picked"
    assert kinds["P8"] == "building corner"
    terrain = KindSelect(names=list(TERRAIN_KINDS)).apply(state.spots).df
    assert 58.0 in set(terrain[ROD_IN]), "an unplanned swale shot is terrain"

    checks = state.check_shots()
    assert len(checks) == 2 and set(checks["mark"]) == {"BM1"}
    assert checks["z"].to_numpy() == pytest.approx([262.8, 262.8]), \
        "the rod reading on BM1 is not a check shot"
    assert checks["session"].str.startswith("field back/").all()


# --- through the API --------------------------------------------------------------------

def test_the_export_downloads_a_project_named_for_the_site_and_day(tmp_path, site):
    from fastapi.testclient import TestClient

    from gpsrtk.web import create_app

    state = AppState(site=site, cache_dir=tmp_path / "cache")
    state.plan.add_point(E0, N0)
    client = TestClient(create_app(state, vector_providers={}),
                        base_url="http://127.0.0.1", headers={"X-Yard-Survey": "1"})
    reply = client.post("/api/export/swmaps", json={}).json()
    download = reply["download"]
    assert download["filename"] == f"example site {time.strftime('%Y-%m-%d')}.swmz"
    assert "1 outstanding shot," in reply["notice"]["text"]
    assert "BM1, BM2, with the fixed-height pole" in reply["notice"]["text"]
    data = client.get(download["url"]).content
    path = tmp_path / "got.swmz"
    path.write_bytes(data)
    con, member = _open(path)
    assert member == f"Projects/example site {time.strftime('%Y-%m-%d')}.swm2"
    assert con.execute("select count(*) from features").fetchone()[0] == 1
