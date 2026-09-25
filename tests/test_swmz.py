"""Reading SW Maps `.swmz` project archives.

The tests build a database rather than leaning on the real 25 MB archive, so
they run without it and so the awkward cases - a feature layer, custom
attributes, a missing table - can actually be constructed. The one test that
does use the real file is skipped when it is not present, and exists because
a synthetic database cannot prove the reader understands SW Maps' own schema.
"""

import sqlite3
import zipfile
from pathlib import Path

import pytest

from gpsrtk.io import read_any
from gpsrtk.io.swmaps_project import SWMapsProjectReader
from gpsrtk.model import pointset as P

REAL = Path(__file__).resolve().parents[1] / "archive" / "Project_1_-_2nd.swmz"

# 2026-09-23 16:52:05.500 CDT, the first epoch of the real September outing.
T0 = 1790200325500

SCHEMA = """
CREATE TABLE points(uuid TEXT UNIQUE NOT NULL, fid TEXT, seq NUMBER,
  lat NUMBER, lon NUMBER, elv NUMBER, ortho_ht NUMBER, time NUMBER,
  start_time NUMBER, instrument_ht NUMBER, fix_quality NUMBER, speed NUMBER,
  snap_id TEXT, bearing NUMBER, accuracy_h NUMBER, accuracy_v NUMBER,
  pos_data TEXT, additional_data TEXT);
CREATE TABLE tracks(uuid TEXT UNIQUE NOT NULL, name TEXT UNIQUE NOT NULL,
  color TEXT, description TEXT);
CREATE TABLE features(uuid TEXT UNIQUE NOT NULL, layer_id TEXT, name TEXT,
  remarks TEXT);
CREATE TABLE feature_layers(uuid TEXT UNIQUE NOT NULL, name TEXT UNIQUE,
  group_name TEXT, geom_type TEXT);
CREATE TABLE attribute_fields(uuid TEXT UNIQUE NOT NULL, layer_id TEXT,
  field_name TEXT, data_type TEXT, field_choices TEXT, seq INTEGER);
CREATE TABLE attribute_values(item_id TEXT, field_id TEXT, data_type TEXT,
  value TEXT);
"""

POS_DATA = ('{"PDOP":"0.770","HDOP":"0.420","VDOP":"0.640",'
            '"SatellitesInView":26,"SatellitesInUse":25,'
            '"AgeOfDifferential":0.6,"ReferenceStationID":"0101",'
            '"BaselineLength":"7378.017"}')


def _db(path, *, tracks=True, features=False, attributes=False,
        n=5, pos_data=POS_DATA):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    rows = []
    if tracks:
        con.execute("INSERT INTO tracks VALUES ('trk','Track 1','-65536','')")
        for i in range(n):
            rows.append((f"p{i}", "trk", i, 41.59109 + i * 1e-5,
                         -93.60328 + i * 1e-5, 262.9 + i * 0.01, 0,
                         T0 + i * 100, T0, 0, 4, 0.8, "", 158.2, 0.008,
                         0.017, pos_data, ""))
    if features:
        con.execute("INSERT INTO feature_layers VALUES "
                    "('lyr','Spot Heights','','POINT')")
        con.execute("INSERT INTO features VALUES ('f1','lyr','shot 1','')")
        rows.append(("pf1", "f1", 0, 41.59117, -93.60341, 263.4, 0,
                     T0 + 10_000, T0, 0, 4, 0.0, "", 0.0, 0.007, 0.015,
                     pos_data, ""))
    if attributes:
        con.execute("INSERT INTO attribute_fields VALUES "
                    "('fld','lyr','height number','number','',0)")
        con.execute("INSERT INTO attribute_values VALUES "
                    "('f1','fld','number','41.5')")
    con.executemany(
        "INSERT INTO points VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return path


@pytest.fixture
def swm2(tmp_path):
    return _db(tmp_path / "proj.swm2")


@pytest.fixture
def swmz(tmp_path):
    _db(tmp_path / "inner.swm2", features=True, attributes=True)
    archive = tmp_path / "Outing 3.swmz"
    with zipfile.ZipFile(archive, "w") as z:
        z.write(tmp_path / "inner.swm2", "Projects/Outing 3.swm2")
        z.writestr("RawData/2026_09_23_16_47_10.log", b"$GNGGA,,,,\r\n")
    return archive


# --- dispatch -------------------------------------------------------------

def test_the_registry_picks_this_reader_for_swmz(swmz):
    exp = read_any(swmz)
    assert exp.name == "Outing 3"


def test_a_bare_database_is_readable_too(swm2):
    """Useful when the archive has already been unpacked."""
    assert read_any(swm2).layers


def test_a_csv_zip_is_left_to_the_other_reader(tmp_path):
    z = tmp_path / "x.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("a_TRACK_POINTS.csv", "X,Y,Elevation\n1,2,3\n")
    assert not SWMapsProjectReader().can_read(z)


def test_a_zip_with_no_database_is_not_claimed(tmp_path):
    z = tmp_path / "empty.swmz"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("readme.txt", "nothing here")
    assert not SWMapsProjectReader().can_read(z)


def test_something_that_is_not_a_zip_is_refused(tmp_path):
    bad = tmp_path / "broken.swmz"
    bad.write_bytes(b"not a zip at all")
    assert not SWMapsProjectReader().can_read(bad)


# --- contents -------------------------------------------------------------

def test_track_points_land_in_the_same_layer_as_the_csv_reader(swm2):
    """Merging two outings depends on the layer names agreeing."""
    exp = read_any(swm2)
    assert "track_points" in exp.layers
    assert exp["track_points"].df[P.TRACK].unique().tolist() == ["Track 1"]


def test_coordinates_are_projected_from_lat_lon(swm2):
    """The database stores lat/lon only; E/N has to be derived."""
    from pyproj import Transformer

    d = read_any(swm2)["track_points"].df
    tx = Transformer.from_crs("EPSG:4326", "EPSG:32615", always_xy=True)
    e, n = tx.transform(d[P.LON].iloc[0], d[P.LAT].iloc[0])
    assert d[P.E].iloc[0] == pytest.approx(e, abs=1e-6)
    assert d[P.N].iloc[0] == pytest.approx(n, abs=1e-6)


def test_the_correction_link_columns_survive(swm2):
    """These are the whole reason to prefer this format over the CSV export."""
    d = read_any(swm2)["track_points"].df
    assert d[P.AGE_DIFF].iloc[0] == pytest.approx(0.6)
    assert d[P.BASELINE].iloc[0] == pytest.approx(7378.017)
    assert d[P.REF_STATION].iloc[0] == "0101"
    assert d[P.PDOP].iloc[0] == pytest.approx(0.77)
    assert d[P.SATS_USED].iloc[0] == 25
    assert "pos_data" not in d.columns, "the raw blob should not leak through"


def test_a_project_without_pos_data_still_reads(tmp_path):
    d = read_any(_db(tmp_path / "bare.swm2", pos_data=""))["track_points"].df
    assert len(d) == 5
    assert P.AGE_DIFF not in d.columns


def test_times_are_naive_local_like_the_csv_reader(swm2):
    """Two readers whose timestamps sat hours apart would break every
    crossover the moment their data was merged."""
    d = read_any(swm2)["track_points"].df
    t = d[P.TIME].iloc[0]
    assert t.tzinfo is None
    assert t.year == 2026 and t.month == 9 and t.day == 23
    assert d[P.TZ].iloc[0].isupper() and " " not in d[P.TZ].iloc[0]


def test_each_time_uses_the_offset_for_its_own_date():
    """Winter and summer epochs each take their own zone offset, not the one
    in force on the day the file is read. Checked against the platform's own
    conversion, so it means something on any machine whose zone has DST."""
    import datetime as dt

    import pandas as pd

    from gpsrtk.io.swmaps_project import _local_times

    jan = int(dt.datetime(2026, 1, 15, 18, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
    jul = int(dt.datetime(2026, 7, 15, 18, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
    times, _ = _local_times(pd.Series([jan, jul]))
    assert times.iloc[0] == pd.Timestamp(dt.datetime.fromtimestamp(jan / 1000))
    assert times.iloc[1] == pd.Timestamp(dt.datetime.fromtimestamp(jul / 1000))


@pytest.fixture
def central_time(monkeypatch):
    import time

    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is not available on this platform")
    monkeypatch.setenv("TZ", "America/Chicago")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_winter_times_are_standard_time(central_time):
    """18:00 UTC in January is 12:00 CST; it used to come out as 13:00 "CDT"
    when read in summer."""
    import datetime as dt

    import pandas as pd

    from gpsrtk.io.swmaps_project import _local_times

    jan = int(dt.datetime(2026, 1, 15, 18, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
    jul = int(dt.datetime(2026, 7, 15, 18, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
    times, zones = _local_times(pd.Series([jan, jul]))
    assert times.iloc[0] == pd.Timestamp("2026-01-15 12:00")
    assert zones.iloc[0] == "CST"
    assert times.iloc[1] == pd.Timestamp("2026-07-15 13:00")
    assert zones.iloc[1] == "CDT"


def test_sessions_are_labelled_by_file_and_date(swm2):
    session = read_any(swm2)["track_points"].df[P.SESSION].iloc[0]
    assert session == "proj/2026-09-23"


def test_feature_layers_become_their_own_layer(swmz):
    exp = read_any(swmz)
    assert set(exp.layers) == {"track_points", "spot_heights"}
    assert len(exp["spot_heights"]) == 1


def test_custom_attributes_reach_their_canonical_columns(swmz):
    """A rod reading has to arrive as `rod_in` or the level network will not
    see it, whichever reader brought it in."""
    d = read_any(swmz)["spot_heights"].df
    assert d[P.ROD_IN].iloc[0] == pytest.approx(41.5)


def test_the_type_attribute_is_normalised(tmp_path):
    """"Lawn " typed on the phone is the same kind as "lawn"."""
    db = _db(tmp_path / "typed.swm2", features=True, attributes=True)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO attribute_fields VALUES "
                "('typ','lyr','type','text','',1)")
    con.execute("INSERT INTO attribute_values VALUES "
                "('f1','typ','text',' Lawn ')")
    con.commit()
    con.close()
    d = read_any(db)["spot_heights"].df
    assert d[P.KIND].iloc[0] == "lawn"


def test_a_rod_reading_in_feet_and_inches_is_read(tmp_path):
    db = _db(tmp_path / "rod.swm2", features=True, attributes=True)
    con = sqlite3.connect(db)
    con.execute("UPDATE attribute_values SET value='5-3-1/4' WHERE item_id='f1'")
    con.commit()
    con.close()
    d = read_any(db)["spot_heights"].df
    assert d[P.ROD_IN].iloc[0] == pytest.approx(63.25)


def test_a_shot_named_after_its_plan_number_is_that_station(tmp_path):
    """Recording a planned shot as "P12" is how it is matched to the plan."""
    from gpsrtk.vertical import stations

    db = _db(tmp_path / "named.swm2", features=True, attributes=True)
    con = sqlite3.connect(db)
    con.execute("UPDATE features SET name='p12' WHERE uuid='f1'")
    con.commit()
    con.close()
    exp = read_any(db)
    assert list(stations(exp["spot_heights"])) == ["P12"]
    assert exp["track_points"].df["feature_name"].isna().all()


def test_the_station_attribute_is_read(tmp_path):
    from gpsrtk.vertical import stations

    db = _db(tmp_path / "station.swm2", features=True, attributes=True)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO attribute_fields VALUES "
                "('stn','lyr','station','text','',2)")
    con.execute("INSERT INTO attribute_values VALUES ('f1','stn','text','BM1')")
    con.commit()
    con.close()
    ps = read_any(db)["spot_heights"]
    assert ps.df[P.STATION].iloc[0] == "BM1"
    assert list(stations(ps)) == ["BM1"]


def test_the_raw_log_is_reported_but_not_parsed(swmz):
    exp = read_any(swmz)
    assert "raw_logs" in exp.tables
    assert exp.tables["raw_logs"]["member"].iloc[0].startswith("RawData/")
    assert "not parsed" in exp.tables["raw_logs"]["note"].iloc[0]


def test_an_empty_project_says_so(tmp_path):
    empty = _db(tmp_path / "none.swm2", tracks=False)
    with pytest.raises(ValueError, match="no points"):
        read_any(empty)


def test_a_database_that_is_not_a_project_is_refused(tmp_path):
    path = tmp_path / "other.swm2"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE unrelated(x)")
    con.commit()
    con.close()
    with pytest.raises(ValueError, match="not a SW Maps project"):
        read_any(path)


# --- against the real file ------------------------------------------------

@pytest.mark.skipif(not REAL.exists(), reason="real archive not present")
def test_the_real_archive_reads():
    exp = read_any(REAL)
    ps = exp["track_points"]
    assert len(ps) == 35_317
    d = ps.df
    assert (d[P.FIX] == P.FIX_RTK).sum() == 30_337
    assert d[P.SESSION].nunique() == 1
    # Two tracks, one of them a three-point false start.
    assert d[P.TRACK].value_counts().to_dict() == {"Track 2": 35_314,
                                                   "Track 1": 3}
    assert d[P.BASELINE].median() == pytest.approx(7381.772, abs=0.01)


@pytest.mark.skipif(not REAL.exists(), reason="real archive not present")
def test_the_real_archive_agrees_with_the_csv_export_on_projection():
    """The CSV export carries SW Maps' own X/Y; this reader computes its own.

    Checked on the export that has both, they agree to the rounding of the
    exported three-decimal metres - so a project assembled from one file of
    each kind is in one coordinate frame, not two that nearly match.
    """
    import numpy as np
    from pyproj import Transformer

    csv = read_any(Path(REAL).parent / "Project 1.zip")["track_points"].df
    tx = Transformer.from_crs("EPSG:4326", "EPSG:32615", always_xy=True)
    e, n = tx.transform(csv[P.LON].to_numpy(), csv[P.LAT].to_numpy())
    worst = float(np.max(np.hypot(e - csv[P.E], n - csv[P.N])))
    assert worst < 0.002, f"{worst * 1000:.2f} mm apart"
