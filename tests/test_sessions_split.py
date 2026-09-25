"""Sessions split where the data proves a mount change, and nowhere else.

The date used to stand in for a session. The mount changes within a day and
logging runs past midnight, so the date was wrong both ways round: two mounts
in one session, and one outing cut in two at midnight. These build one
export from synthetic blocks and check what the reader calls them.
"""

import zipfile

import pandas as pd
import pytest

from synthetic import track_points

from gpsrtk import merge as M
from gpsrtk.io import read_any
from gpsrtk.model import pointset as P
from gpsrtk.sessions_split import OUTING_GAP_S, split_sessions

DAY = "2026-08-27"


def _after(frame, minutes):
    """When the next block may start: this many minutes after the last row."""
    last = pd.to_datetime(frame["Time"].iloc[-1].rsplit(" ", 1)[0],
                          format="%m/%d/%Y %H:%M:%S.%f")
    return str(last + pd.Timedelta(minutes=minutes))


def _export(tmp_path, *frames, name="Day"):
    path = tmp_path / f"{name}.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{name}_TRACK_POINTS.csv",
                   pd.concat(frames, ignore_index=True).to_csv(index=False))
    return path


def _sessions(path):
    return read_any(path)["track_points"].df[P.SESSION]


def test_a_walked_block_a_metre_higher_is_its_own_session(tmp_path):
    """Mower, a four-minute pause, then the same ground walked with the
    antenna a metre higher: two mounts, so two sessions."""
    mower = track_points(day=f"{DAY} 13:00:00")
    walked = track_points(day=_after(mower, 4), dz=1.0, seed=1).iloc[4000:]
    sessions = _sessions(_export(tmp_path, mower, walked))

    assert sessions.nunique() == 2
    first, second = sessions.iloc[0], sessions.iloc[-1]
    assert first == f"Day/{DAY}"
    start = pd.to_datetime(walked["Time"].iloc[0][:-4], format="%m/%d/%Y %H:%M:%S.%f")
    assert second == f"Day/{DAY} {start:%H:%M}"
    assert (sessions == first).sum() == len(mower)


def test_a_pause_with_no_height_change_stays_one_session(tmp_path):
    """Emptying the bag is a pause, not a new mount."""
    before = track_points(day=f"{DAY} 13:00:00")
    after = track_points(day=_after(before, 10), seed=1).iloc[4000:]
    sessions = _sessions(_export(tmp_path, before, after))
    assert set(sessions) == {f"Day/{DAY}"}


def test_logging_past_midnight_stays_one_session(tmp_path):
    """One outing that crosses midnight keeps the date it started on."""
    late = track_points(day=f"{DAY} 23:50:00")
    assert late["Time"].iloc[-1].startswith("08/28/2026")
    sessions = _sessions(_export(tmp_path, late))
    assert set(sessions) == {f"Day/{DAY}"}


def test_a_step_that_cannot_be_proved_does_not_split(tmp_path):
    """A block on ground nothing else covered cannot show a step, so it is
    not made a session of its own - that would invent an unrecoverable one."""
    mower = track_points(day=f"{DAY} 13:00:00")
    elsewhere = track_points(day=_after(mower, 5), dz=1.0, de=200.0, seed=1)
    sessions = _sessions(_export(tmp_path, mower, elsewhere))
    assert set(sessions) == {f"Day/{DAY}"}


def test_a_separate_outing_hours_later_is_always_its_own_session(tmp_path):
    """Six hours on, the antenna was re-mounted whether or not the ground
    overlaps. If it does not, saying so is the truth, not an artefact."""
    mower = track_points(day=f"{DAY} 08:00:00")
    later = track_points(day=_after(mower, OUTING_GAP_S / 60 + 1), de=200.0, seed=1)
    sessions = _sessions(_export(tmp_path, mower, later))
    assert sessions.nunique() == 2
    report = M.diagnose(read_any(_export(tmp_path, mower, later, name="Two"))
                        ["track_points"])
    assert len(report.unlinked) == 1


def test_the_next_days_outing_is_named_by_its_own_date(tmp_path):
    first = track_points(day=f"{DAY} 13:00:00")
    second = track_points(day="2026-08-28 13:00:00", seed=1)
    assert set(_sessions(_export(tmp_path, first, second))) == {
        f"Day/{DAY}", "Day/2026-08-28"}


def test_float_heights_are_not_evidence_of_a_step():
    """Float carries decimetres of bias; a step proved by it would be
    invented. Only fixed heights count."""
    df = track_points(day=f"{DAY} 13:00:00").rename(columns={
        "X": P.E, "Y": P.N, "Elevation": P.Z, "Fix ID": P.FIX,
        "Track Name": P.TRACK})
    df[P.TIME] = pd.to_datetime(df["Time"].str[:-4], format="%m/%d/%Y %H:%M:%S.%f")
    tail = df.iloc[4000:].copy()
    tail[P.TIME] += pd.Timedelta(minutes=30)
    tail[P.Z] += 1.0
    tail[P.FIX] = 5
    labels = split_sessions(pd.concat([df, tail], ignore_index=True), "Day")
    assert labels.nunique() == 1


def test_a_later_session_has_the_right_date():
    info = M.SessionInfo(name=f"Day/{DAY} 14:05", points=1, fixed=1, floated=0)
    assert info.date == DAY


@pytest.mark.parametrize("fix", [4])
def test_rows_without_a_time_keep_the_bare_prefix(fix):
    df = pd.DataFrame({P.E: [0.0, 1.0], P.N: [0.0, 1.0], P.Z: [0.0, 0.0],
                       P.FIX: fix, P.TIME: pd.to_datetime([None, f"{DAY} 12:00"])})
    assert list(split_sessions(df, "Day")) == ["Day", f"Day/{DAY}"]
