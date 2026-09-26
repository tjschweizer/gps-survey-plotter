"""Reading `.yardsession` files from the Yard Survey Android app.

Built from `synthetic.write_yardsession`, which lays out the same lot as the
SW Maps synthetic export, so positions and times can be checked against it.
"""

import json
import zipfile

import numpy as np
import pandas as pd
import pytest

from gpsrtk.io import read_any
from gpsrtk.io.swmaps import SWMapsReader
from gpsrtk.io.swmaps_project import SWMapsProjectReader
from gpsrtk.io.yardsession import YardSessionReader
from gpsrtk.model import pointset as P

from synthetic import track_points, write_yardsession


@pytest.fixture
def session(tmp_path):
    return write_yardsession(tmp_path / "20260827T180000Z.yardsession")


def test_dispatches_and_reads_all_layers(session):
    exp = read_any(session)
    assert set(exp.layers) == {"track_points", "shots", "control checks"}
    assert len(exp["track_points"]) == len(track_points())
    assert any("MSM_NEAR" in note for note in exp.notes)


def test_other_readers_leave_it_alone(session):
    assert YardSessionReader().can_read(session)
    assert not SWMapsReader().can_read(session)
    assert not SWMapsProjectReader().can_read(session)


def test_projects_to_the_same_utm_as_sw_maps(session):
    tp = read_any(session)["track_points"].df
    ref = track_points()
    assert np.abs(tp[P.E].to_numpy() - ref["X"].to_numpy()).max() < 0.001
    assert np.abs(tp[P.N].to_numpy() - ref["Y"].to_numpy()).max() < 0.001


def test_times_are_local_in_the_manifest_zone(session):
    tp = read_any(session)["track_points"].df
    assert tp[P.TIME].iloc[0] == pd.Timestamp("2026-08-27 13:00:00")
    assert set(tp[P.TZ]) == {"CDT"}


def test_winter_session_gets_standard_time(tmp_path):
    path = write_yardsession(tmp_path / "w.yardsession", day="2026-01-15 13:00:00")
    tp = read_any(path)["track_points"].df
    assert tp[P.TIME].iloc[0] == pd.Timestamp("2026-01-15 13:00:00")
    assert set(tp[P.TZ]) == {"CST"}


def test_carries_canonical_columns(session):
    d = read_any(session)["track_points"].df
    for c in (P.Z, P.FIX, P.HACC, P.VACC, P.SPEED, P.PDOP, P.AGE_DIFF,
              P.REF_STATION, P.BASELINE, P.SESSION, P.SOURCE, "mono_ns"):
        assert c in d.columns, c
    assert d[P.REF_STATION].iloc[0] == "0101"   # a label, not the number 101
    assert set(d[P.FIX]) <= {4, 5}


def test_shots_are_read_as_a_field_project(session):
    shots = read_any(session)["shots"].df
    assert shots[P.STATION].iloc[0] == "P1"
    assert shots[P.ROD_IN].notna().all()
    assert set(shots[P.SETUP]) == {"A", "B"}
    assert set(shots[P.KIND]) == {"lawn"}          # normalised


def test_control_checks_are_check_shots(session):
    checks = read_any(session)["control checks"].df
    assert checks[P.STATION].iloc[0] == "01"
    assert set(checks[P.KIND]) == {"control check"}


def test_raw_streams_are_listed_not_parsed(session):
    raw = read_any(session).tables["raw_logs"]
    assert set(raw["member"]) == {"raw/gnss.bin", "raw/corrections.rtcm3"}


def test_empty_layers_are_left_out(tmp_path):
    path = write_yardsession(tmp_path / "s.yardsession", shots=False)
    assert "shots" not in read_any(path).layers


def test_refuses_a_newer_format(tmp_path):
    path = write_yardsession(tmp_path / "n.yardsession", version=2)
    with pytest.raises(ValueError, match="version 2"):
        read_any(path)


def test_refuses_something_else_with_the_extension(tmp_path):
    path = tmp_path / "x.yardsession"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("manifest.json", json.dumps({"format": "other", "version": 1}))
    with pytest.raises(ValueError, match="not a Yard Survey session"):
        read_any(path)
