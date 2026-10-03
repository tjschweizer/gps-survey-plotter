"""A recorded session's raw streams, on UTC seconds.

`load(folder)` reads a session folder as the app records it:

- GNSS epochs from `raw/gnss.bin` (GGA, RMC, GST), projected to UTM 15N;
  speed and course are the receiver's Doppler values from RMC.
- The phone's IMU from `raw/imu.bin`: uncalibrated gyro and accelerometer
  with Android's bias estimates, on the phone's axes.
- The Cube's gyro and accelerometer FIFOs from each `raw/cube*.ulg`.

The IMU axes are converted to FRD (x forward, y right, z down) as mounted
on the rig: PX4's board frame already is; the phone lies screen up with its
top forward, so FRD = (y, x, -z) of Android's axes. Another phone mounting
changes `PHONE_TO_FRD`.
"""

from __future__ import annotations

import gzip
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from pyproj import Transformer

from .clock import CubeClock, PhoneUtc
from .ulog import ULog, fifo

KNOT = 1852.0 / 3600.0
PHONE_TO_FRD = np.array([[0.0, 1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, -1.0]])

# Android sensor types in imu.bin
GYRO_UNCAL, ACCEL_UNCAL = 16, 35

NMEA = re.compile(rb"\$G[A-Z](GGA|RMC|GST),([^\r\n$*]*)\*([0-9A-Fa-f]{2})")


@dataclass
class Imu:
    """One device's gyro and accelerometer, FRD axes, times in UTC seconds."""
    name: str
    t_gyro: np.ndarray
    gyro: np.ndarray          # rad/s
    t_accel: np.ndarray
    accel: np.ndarray         # m/s^2, specific force
    extra: dict = field(default_factory=dict)


@dataclass
class Session:
    folder: Path
    epochs: pd.DataFrame
    phone_utc: PhoneUtc
    cube_clocks: dict[str, CubeClock]
    imus: dict[str, Imu]
    events: list[dict]
    ulogs: dict[str, ULog]


def read_events(folder: Path) -> list[dict]:
    p = folder / "events.jsonl"
    opener = open if p.exists() else gzip.open
    with opener(p if p.exists() else folder / "events.jsonl.gz", "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def read_indexed(raw: Path, name: str) -> tuple[bytes, np.ndarray, np.ndarray]:
    """A stream's bytes, and for each read the end offset and arrival (mono ns)."""
    data = (raw / f"{name}.bin").read_bytes()
    idx = (raw / f"{name}.idx").read_bytes()
    rec = np.frombuffer(idx[: len(idx) // 12 * 12], dtype=[("mono", "<i8"), ("len", "<i4")])
    return data, np.cumsum(rec["len"].astype(np.int64)), rec["mono"]


def _checksum_ok(m: re.Match) -> bool:
    c = 0
    for b in m.group(0)[1:-3]:
        c ^= b
    return c == int(m.group(3), 16)


def _tod_s(t: str) -> float | None:
    try:
        return int(t[0:2]) * 3600 + int(t[2:4]) * 60 + float(t[4:])
    except ValueError:
        return None


def _f(fields: list[str], i: int) -> float:
    try:
        return float(fields[i])
    except (IndexError, ValueError):
        return np.nan


def _deg(v: str, hemi: str, digits: int) -> float:
    if len(v) < digits + 2:
        return np.nan
    d = int(v[:digits]) + float(v[digits:]) / 60
    return -d if hemi in ("S", "W") else d


def gnss_epochs(raw: Path) -> pd.DataFrame:
    """One row per epoch with a fix: UTC, arrival, position, Doppler speed and course.

    Until it has a fix the receiver's clock is a guess, so only epochs whose
    GGA has a fix count, dated by the first valid RMC.
    """
    data, ends, monos = read_indexed(raw, "gnss")
    rows: dict[str, dict] = {}
    date = None
    for m in NMEA.finditer(data):
        if not _checksum_ok(m):
            continue
        f = m.group(2).decode("ascii", "replace").split(",")
        if not f or not f[0]:
            continue
        r = rows.setdefault(f[0], {})
        kind = m.group(1)
        if kind == b"GGA":
            if len(f) < 11 or f[5] in ("", "0"):
                continue
            i = min(int(np.searchsorted(ends, m.end())), len(monos) - 1)
            r.update(mono_ns=int(monos[i]), lat=_deg(f[1], f[2], 2), lon=_deg(f[3], f[4], 3),
                     fix=int(f[5]), sats=_f(f, 6), hdop=_f(f, 7),
                     h=_f(f, 8) + (_f(f, 10) if f[10] else 0.0), age=_f(f, 12))
        elif kind == b"RMC" and len(f) > 8 and f[1] == "A":
            r.update(speed=_f(f, 6) * KNOT, course=_f(f, 7))
            if date is None and len(f[8]) == 6:
                date = datetime.strptime(f[8], "%d%m%y").replace(tzinfo=timezone.utc)
        elif kind == b"GST":
            r.update(sd_lat=_f(f, 5), sd_lon=_f(f, 6), sd_h=_f(f, 7))
    if date is None:
        return pd.DataFrame()
    day0 = date.timestamp()
    out, last, days = [], None, 0
    for key, r in rows.items():  # insertion order is stream order
        if "mono_ns" not in r:
            continue
        tod = _tod_s(key)
        if tod is None:
            continue
        if last is not None and tod < last - 43200:
            days += 1
        last = tod
        out.append(dict(utc=day0 + days * 86400 + tod, **r))
    df = pd.DataFrame(out)
    e, n = Transformer.from_crs("EPSG:4326", "EPSG:32615", always_xy=True).transform(
        df["lon"].to_numpy(), df["lat"].to_numpy())
    df["e"], df["n"] = e, n
    return df.sort_values("utc").reset_index(drop=True)


def phone_imu(raw: Path, utc: PhoneUtc) -> Imu | None:
    """imu.bin: u8 type, u8 n, i64 sensor ns, i64 arrival ns, f32 x n (LE)."""
    p = raw / "imu.bin"
    if not p.exists():
        return None
    buf = p.read_bytes()
    pos, n = 0, len(buf)
    rec: dict[int, list] = {GYRO_UNCAL: [], ACCEL_UNCAL: []}
    while pos + 18 <= n:
        kind, count = buf[pos], buf[pos + 1]
        end = pos + 18 + 4 * count
        if end > n:
            break
        if kind in rec:
            rec[kind].append(buf[pos + 2:end])
        pos = end

    def unpack(chunks: list[bytes], count: int):
        a = np.frombuffer(b"".join(chunks), dtype=[("ts", "<i8"), ("arr", "<i8"), ("v", "<f4", (count,))])
        return a["ts"], a["v"].astype(float)

    tg, g = unpack(rec[GYRO_UNCAL], 6)
    ta, a = unpack(rec[ACCEL_UNCAL], 6)
    return Imu(
        "phone",
        utc.to_utc_s(tg), g[:, :3] @ PHONE_TO_FRD.T,
        utc.to_utc_s(ta), a[:, :3] @ PHONE_TO_FRD.T,
        extra={"gyro_bias_android": g[:, 3:] @ PHONE_TO_FRD.T,
               "accel_bias_android": a[:, 3:] @ PHONE_TO_FRD.T},
    )


def timesync_by_part(events: list[dict]) -> dict[str, np.ndarray]:
    parts: dict[str, list] = {}
    current = None
    for e in events:
        if e.get("src") != "cube":
            continue
        if e["type"] == "OPEN":
            current = e.get("ulog", "raw/cube.ulg")
            parts.setdefault(current, [])
        elif e["type"] == "TIMESYNC" and current:
            parts[current].append((e["sent_ns"], e["recv_ns"], e["cube_ns"]))
    return {k: np.array(v, dtype=np.int64) for k, v in parts.items() if v}


def load(folder: str | Path) -> Session:
    folder = Path(folder)
    raw = folder / "raw"
    events = read_events(folder)
    epochs = gnss_epochs(raw)
    utc = PhoneUtc(epochs["mono_ns"].to_numpy(np.int64),
                   np.round(epochs["utc"].to_numpy() * 1e3).astype(np.int64) * 1_000_000)
    imus: dict[str, Imu] = {}
    phone = phone_imu(raw, utc)
    if phone is not None:
        imus["phone"] = phone
    clocks: dict[str, CubeClock] = {}
    ulogs: dict[str, ULog] = {}
    for part, s in timesync_by_part(events).items():
        path = folder / part
        if not path.exists():
            continue
        clock = clocks[part] = CubeClock(s[:, 0], s[:, 1], s[:, 2])
        log = ulogs[part] = ULog.read(path)
        tg, g = fifo(log, "sensor_gyro_fifo")
        ta, a = fifo(log, "sensor_accel_fifo")
        name = "cube" if part.endswith("cube.ulg") else Path(part).stem
        full = {k: float(log.topic(f"sensor_{k}_fifo")["scale"][0]) * 32767
                for k in ("gyro", "accel") if log.topic(f"sensor_{k}_fifo") is not None}
        imus[name] = Imu(name, utc.to_utc_s(clock.to_phone_ns(tg)), g,
                         utc.to_utc_s(clock.to_phone_ns(ta)), a,
                         extra={"part": part, "full_scale": full})
    return Session(folder, epochs, utc, clocks, imus, events, ulogs)
