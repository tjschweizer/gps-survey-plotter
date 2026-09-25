"""Control marks and start/end check shots.

A permanent mark shot with the fixed-height pole at the start and end of
every outing ties that outing to every other outing that shot it - whether or
not their ground overlaps - and shows when a mount has shifted. Read with the
rod from every laser setup, the mark closes the setup and can hold the datum.
"""

import json
import zipfile

import numpy as np
import pandas as pd
import pytest

from synthetic import ORIGIN_E, ORIGIN_N, _latlon, _stamp, track_points, write_export

from gpsrtk.app import AppState
from gpsrtk.app.sessions import sessions_payload
from gpsrtk.project import Project
from gpsrtk.site import ControlMark, Site, example_site

MARK_GROUND = 260.8              # ellipsoidal height of the mark's head
POLE_M = 2.0                     # the fixed-height pole


def _checks(times, *, pole_dz=0.0, rods=None, setup=0):
    """Check shots on BM1, 5 m south-west of the lot, at the given times."""
    e, n = np.full(len(times), ORIGIN_E - 5.0), np.full(len(times), ORIGIN_N - 5.0)
    lat, lon = _latlon(e, n)
    frame = pd.DataFrame({
        "ID": np.arange(901, 901 + len(times)),
        "Feature Name": "BM1",
        "Time": _stamp(pd.Series(pd.to_datetime(times))),
        "X": e, "Y": n, "Elevation": MARK_GROUND + POLE_M + pole_dz,
        "Latitude": lat, "Longitude": lon, "Fix ID": 4,
    })
    if rods is not None:
        frame["height number"] = rods
        frame["base position"] = setup
        frame["type"] = "check"
    return frame


def _outing(path, *, day, dz=0.0, de=0.0, seed=0, pole_dz=None, rods=None):
    """Tracks, plus BM1 shot 10 minutes before the first pass and after the last."""
    tracks = track_points(day=day, dz=dz, de=de, seed=seed)
    start = pd.Timestamp(day)
    end = start + pd.Timedelta(seconds=0.3 * len(tracks))
    checks = _checks([start - pd.Timedelta(minutes=10), end + pd.Timedelta(minutes=10)],
                     pole_dz=dz if pole_dz is None else pole_dz, rods=rods)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{path.stem}_TRACK_POINTS.csv", tracks.to_csv(index=False))
        z.writestr(f"{path.stem}_marks.csv", checks.to_csv(index=False))
    return path


@pytest.fixture
def site():
    s = example_site()
    s.control = [ControlMark("BM1", note="mag nail")]
    return s


@pytest.fixture
def fresh(site, tmp_path):
    return AppState(site=site, cache_dir=tmp_path / "cache")


def test_an_outing_on_other_ground_is_tied_by_its_check_shots(fresh, tmp_path):
    """The synthetic "Elsewhere" outing shares no ground with the first. With
    check shots on the same mark at both, it is reconcilable, and its offset
    - a GNSS bias of 4 cm, carried by pole and mower alike - is recovered."""
    first = _outing(tmp_path / "First.zip", day="2026-08-27 13:00:00")
    elsewhere = _outing(tmp_path / "Elsewhere.zip", day="2026-10-02 10:00:00",
                        de=200.0, dz=0.04, seed=2)
    fresh.load(first)
    report = fresh.add_export(elsewhere)
    assert report.reconcilable, report.describe()
    assert report.check_shots == 4
    assert "check shots on control marks" in report.describe()

    model = fresh.solve_vertical("ellipsoidal")
    assert not model.sessions.unresolved
    offsets = model.sessions.offsets
    assert offsets["Elsewhere/2026-10-02"] - offsets["First/2026-08-27"] == \
        pytest.approx(0.04, abs=0.01)
    assert not report.thin, "a mark tie is direct, not thin overlap"


def test_without_check_shots_it_cannot_be_reconciled(site, tmp_path, synthetic_zip,
                                                    synthetic_elsewhere):
    st = AppState(site=site, cache_dir=tmp_path / "cache")
    st.load(synthetic_zip)
    assert not st.add_export(synthetic_elsewhere).reconcilable


def test_a_shifted_mount_is_flagged(fresh, tmp_path):
    """The second outing's mower sat 5 cm higher, but the pole did not move:
    the overlap says +5 cm, the check shots say 0, and the checks say so."""
    fresh.load(_outing(tmp_path / "First.zip", day="2026-08-27 13:00:00"))
    fresh.add_export(_outing(tmp_path / "Second.zip", day="2026-09-27 13:00:00",
                             dz=0.05, seed=1, pole_dz=0.0))
    model = fresh.solve_vertical("ellipsoidal")
    second = model.sessions.checks["Second/2026-09-27"]
    assert second["start"][1] == pytest.approx(-0.05, abs=0.01)
    assert second["flagged"]
    assert not model.sessions.checks["First/2026-08-27"]["flagged"]

    rows = {s["name"]: s for s in sessions_payload(fresh)["list"]}
    assert rows["Second/2026-09-27"]["checks_flagged"]
    assert "checks: BM1 start -5." in rows["Second/2026-09-27"]["checks"]
    assert "OVER 3 cm" in rows["Second/2026-09-27"]["checks"]
    assert rows["First/2026-08-27"]["checks"].startswith("checks: BM1 start +0.0 cm")


def test_a_session_with_no_check_shots_says_so(fresh, synthetic_zip):
    fresh.load(synthetic_zip)
    (row,) = sessions_payload(fresh)["list"]
    assert row["checks"] == "no check shots" and not row["checks_flagged"]


def test_a_check_shot_far_from_any_outing_belongs_to_none(fresh, tmp_path):
    tracks = track_points(day="2026-08-27 13:00:00")
    path = tmp_path / "Late.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Late_TRACK_POINTS.csv", tracks.to_csv(index=False))
        z.writestr("Late_marks.csv", _checks(["2026-08-27 18:00:00"]).to_csv(index=False))
    fresh.load(path)
    assert fresh.check_shots().empty


def test_a_rod_reading_on_a_mark_can_hold_the_datum(fresh, tmp_path):
    """Read the rod on BM1 at the open and the close of the setup: the mark
    is a station, the setup closes on it, and the datum hangs from it."""
    path = tmp_path / "Rod.zip"
    write_export(path)
    with zipfile.ZipFile(path, "a") as z:
        z.writestr("Rod_marks.csv", _checks(["2026-08-27 12:30:00", "2026-08-27 12:40:00"],
                                            rods=[50.0, 50.0]).to_csv(index=False))
    fresh.load(path)
    assert "BM1" in fresh.spot_ids()
    fresh.set_datum_tie(point="bm1", elev_ft=100.0)
    model = fresh.solve_vertical("local")
    assert model.level.elevations["BM1"] == pytest.approx(100.0 * 0.3048, abs=1e-6)
    setup0 = next(c for c in model.level.setups if c.setup == "0")
    assert setup0.repeats == [("BM1", 0.0)]


# --- the site and the project file ---------------------------------------------

def test_marks_round_trip_in_the_site(site, tmp_path):
    site.control.append(ControlMark("bm2", elev_ft=101.25))
    path = tmp_path / "site.json"
    site.save(path)
    back = Site.load(path)
    assert [(m.name, m.elev_ft, m.note) for m in back.control] == [
        ("BM1", None, "mag nail"), ("BM2", 101.25, "")]


def test_a_project_without_control_marks_still_loads(tmp_path):
    """Version 2 files have no `control` key in their site."""
    old = Project(site=example_site()).to_dict()
    old["version"] = 2
    old["site"].pop("control")
    path = tmp_path / "v2.yardproj"
    path.write_text(json.dumps(old), encoding="utf-8")
    assert Project.load(path).site.control == []


def test_projects_are_format_3(site, tmp_path):
    saved = Project(site=site).save(tmp_path / "v3.yardproj")
    d = json.loads(saved.read_text("utf-8"))
    assert d["version"] == 3
    assert d["site"]["control"] == [{"name": "BM1", "elev_ft": None, "note": "mag nail"}]


def test_setting_marks_refuses_names_that_are_already_stations(fresh):
    with pytest.raises(ValueError, match="P<number> is a planned shot"):
        fresh.set_control_marks([{"name": "P12"}])
    with pytest.raises(ValueError, match="listed twice"):
        fresh.set_control_marks([{"name": "BM1"}, {"name": "bm1"}])
    fresh.set_control_marks([{"name": " bm3 ", "elev_ft": 99.5}])
    assert fresh.site.mark_names == ["BM3"]


def test_the_control_marks_api(tmp_path, synthetic_zip):
    from fastapi.testclient import TestClient

    from gpsrtk.web import create_app

    st = AppState(site=example_site(), cache_dir=tmp_path / "cache")
    st.load(synthetic_zip)
    client = TestClient(create_app(st), base_url="http://127.0.0.1",
                        headers={"X-Yard-Survey": "1"})
    assert client.get("/api/control").json()["marks"] == []
    r = client.post("/api/control", json={"marks": [{"name": "BM1", "elev_ft": None,
                                                      "note": "nail"}]})
    assert r.status_code == 200
    assert client.get("/api/control").json()["marks"][0]["name"] == "BM1"
    r = client.post("/api/control", json={"marks": [{"name": "12"}]})
    assert r.status_code == 400 and r.json()["error"]["title"] == "Control marks"


def test_the_field_sheet_carries_the_routine(tmp_path):
    from gpsrtk.io.fieldsheet import write_field_sheet
    from gpsrtk.plan import Plan

    plan = Plan()
    plan.add_point(449712.0, 4604565.0)
    text = write_field_sheet(plan, tmp_path / "s.html", marks=["BM1", "BM2"]).read_text("utf-8")
    assert "Control marks and check shots" in text and "(marks: BM1, BM2)" in text
    assert "start and end of every outing" in text
