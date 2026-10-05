"""Fuse a recorded session: the Cube's IMU with every GNSS epoch, fixed and float.

    python -m gpsrtk.fusion.fuse <session folder> <out.csv>

Per Cube connection (`raw/cube.ulg`, `cube-2.ulg`, ...): the gyro and
accelerometer FIFOs integrated into exact 200 Hz increments a chunk at a
time (a long log never sits in memory whole), the step-1 checks run on the
longest connection to measure the rig (timing, mount yaw, lever arm) and
compare it with what the session's note says, then the INS filter and
smoother over the connection with every fixed and float epoch. Epochs the
Cube didn't cover keep their GNSS position as it was.

The output has one row per GNSS epoch: time, fix quality, the antenna's
position (lat, lon, ellipsoidal height) and its sd, and where it came from
(`fused`, or `gnss` outside the Cube's connections). It locates the lot:
write it somewhere git ignores.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from . import checks as C
from . import ins
from .clock import CubeClock, PhoneUtc
from .rawsession import Imu, gnss_epochs, read_events, timesync_by_part
from .ulog import ULog, fifo_integrals

FIFO = ("sensor_gyro_fifo", "sensor_accel_fifo")
GRID_HZ = 200
FILTER_HZ = 25


@dataclass
class Rig:
    """The IMU against the vehicle, as measured or as noted.

    `mount_yaw_deg`: the IMU's x axis right of the vehicle's forward.
    `antenna` and `pivot`: from the IMU, in the vehicle's axes (forward,
    right, down), metres. The pivot is the point that doesn't slip sideways
    (a fixed axle).
    """
    mount_yaw_deg: float
    antenna: tuple[float, float, float]
    pivot: tuple[float, float, float]
    lag_s: float

    def to_imu(self, v) -> np.ndarray:
        mu = np.radians(self.mount_yaw_deg)
        c, s = np.cos(mu), np.sin(mu)
        x, y, z = v
        return np.array([x * c + y * s, -x * s + y * c, z])


@dataclass
class Part:
    name: str
    edges: np.ndarray        # 200 Hz step edges, seconds from the first epoch, Cube timestamps (no lag)
    dtheta: np.ndarray
    dvel: np.ndarray
    clipped: int


@dataclass
class Fused:
    epochs: pd.DataFrame
    rig: Rig
    report: list[str] = field(default_factory=list)


def cube_parts(folder: Path, utc: PhoneUtc, t0: float, log=print) -> list[Part]:
    parts = []
    for part, s in sorted(timesync_by_part(read_events(folder)).items()):
        path = folder / part
        if not path.exists():
            continue
        clock = CubeClock(s[:, 0], s[:, 1], s[:, 2])
        ul = ULog.read(path, topics=FIFO)
        to_s = lambda us, clock=clock: utc.to_utc_s(clock.to_phone_ns(us)) - t0
        r = ul.topic(FIFO[0])
        if r is None or len(r) < 100:
            continue
        lo, hi = to_s(np.array([float(r["timestamp_sample"][0]), float(r["timestamp_sample"][-1])]))
        edges = np.arange(np.ceil(lo * GRID_HZ) / GRID_HZ + 0.1, hi - 0.1, 1 / GRID_HZ)
        dth, cg = fifo_integrals(ul, FIFO[0], to_s, edges)
        ul.drop(FIFO[0])
        dv, ca = fifo_integrals(ul, FIFO[1], to_s, edges)
        del ul
        ok = np.isfinite(dth).all(axis=1) & np.isfinite(dv).all(axis=1)
        # A hole in the IMU data ends a run: each run of at least 20 s is fused on its own.
        runs = np.flatnonzero(np.diff(np.concatenate([[0], ok.astype(int), [0]])))
        for i, (a, b) in enumerate(zip(runs[::2], runs[1::2])):
            if (b - a) < 20 * GRID_HZ:
                continue
            name = part if len(runs) == 2 else f"{part}#{i + 1}"
            parts.append(Part(name, edges[a:b + 1], dth[a:b], dv[a:b], cg + ca))
            log(f"  {name}: {(edges[b] - edges[a]):.0f} s of IMU from {edges[a]:.0f} s, {cg + ca} clipped samples")
    return parts


def epoch_arrays(e: pd.DataFrame, t0: float) -> dict:
    """The GNSS side of checks.Prepared."""
    t = e["utc"].to_numpy() - t0
    east, north, h = e["e"].to_numpy(), e["n"].to_numpy(), e["h"].to_numpy()
    ve = np.gradient(C.lowpass(east - east.mean(), 2.0, C.GNSS_HZ), t)
    vn = np.gradient(C.lowpass(north - north.mean(), 2.0, C.GNSS_HZ), t)
    return dict(t=t, e=east, n=north, h=h, ve=ve, vn=vn, speed=np.hypot(ve, vn), course=np.arctan2(ve, vn),
                doppler_speed=e["speed"].fillna(0.0).to_numpy(), doppler_course=np.radians(e["course"].to_numpy()))


def prepared(part: Part, ep: dict, t0: float, mount_yaw_deg: float) -> C.Prepared:
    """A Cube connection as the checks see it: rates on the 200 Hz grid, turned
    by the noted mount yaw so x is the vehicle's forward (the checks assume it)."""
    grid = (part.edges[:-1] + part.edges[1:]) / 2
    mu = np.radians(mount_yaw_deg)
    to_vehicle = np.array([[np.cos(mu), -np.sin(mu), 0], [np.sin(mu), np.cos(mu), 0], [0, 0, 1.0]])
    gyro, accel = part.dtheta * GRID_HZ @ to_vehicle.T, part.dvel * GRID_HZ @ to_vehicle.T
    stops = [s for s in C.find_stops(ep["t"], ep["doppler_speed"]) if s[0] >= grid[0] and s[1] <= grid[-1]]
    still = C.in_spans(grid, stops) if stops else np.ones(len(grid), bool)
    g = accel[still].mean(axis=0)
    down = -g / np.linalg.norm(g)
    w = gyro @ down
    w = w - (w[still].mean() if stops else 0.0)
    imu = Imu("cube", grid[[0, -1]], gyro[[0, -1]], grid[[0, -1]], accel[[0, -1]])
    dev = C.Device("cube", imu, gyro, accel, down, w, np.empty(0))
    return C.Prepared(t0, grid, ep, stops, np.empty(0), {"cube": dev})


def measure_rig(p: C.Prepared, noted: Rig, log=print) -> None:
    """Timing, mount yaw and lever arm from the data, reported against the note.

    Reported, not used: these checks assume a vehicle that stays nearly
    level, and a push mower pitches whenever its front is lifted to turn
    (on 2026-10-03 the acceleration fit was 25x worse than on the cart). The
    filter itself carries full attitude, so it runs on the note's geometry.
    """
    rev = C.reversing(p)
    at = C.along_track_lag(p, "cube", rev)
    ev = [x["lag"] for x in C.event_lags(p, "cube") if x["ok"]]
    lag = float(np.median(ev + [at["lag"]])) if ev else at["lag"]
    log(f"  timing: along-track {at['lag'] * 1e3:+.0f} ms (corr {at['peak']:.2f}), starts/stops "
        + (", ".join(f"{x * 1e3:+.0f}" for x in ev) or "none") + f" ms -> {lag * 1e3:+.0f} ms")
    cf = C.course_fit(p, "cube", rev, lag)
    slope, info = C.local_slope(p)
    note = (-noted.antenna[0], -noted.antenna[1])
    lf = C.lever_fit(p, "cube", cf, slope, note)
    log(f"  course model: {cf.rms_deg:.2f} deg rms; ground {info['grade_pct']:.1f}% mean grade")
    log(f"  mount yaw beyond the note's: {lf.mount_yaw_deg:+.1f} deg; IMU from antenna "
        f"{lf.dx:+.3f}/{lf.dy:+.3f} m fwd/right +/- {lf.sd[0]:.3f} (noted {note[0]:+.2f}/{note[1]:+.2f}); "
        f"residual {lf.rms:.3f} m/s^2 (noted lever arm {lf.rms_note:.3f}, none {lf.rms_zero:.3f})")


def episodes_of(fix: np.ndarray) -> np.ndarray:
    """Float runs numbered from 0; -1 elsewhere."""
    f = fix == 5
    start = f & ~np.concatenate([[False], f[:-1]])
    n = np.cumsum(start) - 1
    return np.where(f, n, -1)


def fuse_part(part: Part, p: C.Prepared, rig: Rig, ned: np.ndarray, fix: np.ndarray, lat0: float,
              cfg_over: dict | None = None, keep_attitude: bool = False):
    """Filter and smoother over one Cube connection; antenna NED and sd at the epochs it covers."""
    k = GRID_HZ // FILTER_HZ
    m = (len(part.dtheta) // k) * k
    dth = part.dtheta[:m].reshape(-1, k, 3).sum(axis=1)
    dv = part.dvel[:m].reshape(-1, k, 3).sum(axis=1)
    t = part.edges[: m + 1: k] - rig.lag_s               # true time: IMU late by lag
    steps = ins.steps_from_increments(t, dth, dv, FILTER_HZ)

    la, lp = rig.to_imu(rig.antenna), rig.to_imu(rig.pivot)
    cfg = ins.Config(lever_antenna=la, lever_pivot=lp, mount_yaw=np.radians(rig.mount_yaw_deg),
                     nhc_sd=(0.10, 0.05))
    for key, v in (cfg_over or {}).items():
        setattr(cfg, key, v)

    te = p.ep["t"]
    inside = (te >= t[0] + 0.5) & (te <= t[-1] - 0.5)
    # Heading at the start, from the gyro's heading fitted to the course.
    rev = C.reversing(p)
    cf = C.course_fit(p, "cube", rev, rig.lag_s)
    psi = C.integrate(C.lowpass(p.devices["cube"].w, 5, C.FS), C.FS)
    heading = float(np.interp(t[0] + rig.lag_s, p.grid, psi) + cf.b + cf.d * t[0]) + cfg.mount_yaw
    f_mean = (dv[:FILTER_HZ * 2] / np.diff(t)[:FILTER_HZ * 2, None]).mean(axis=0)
    Cbn = ins.initial_attitude(f_mean, heading)
    w_ie = ins.OMEGA_E * np.array([np.cos(lat0), 0.0, -np.sin(lat0)])
    bg = np.zeros(3)
    j0 = int(np.argmax(inside))
    ant0 = ned[j0]
    x0 = ins.Nominal(ant0 - Cbn @ la, np.zeros(3), Cbn, bg, np.zeros(3), np.zeros(3), np.zeros(3), np.zeros(3))
    first_sd = 0.02 if fix[j0] == 4 else 0.5
    # The Cube may sit several degrees off the deck (8 on 2026-10-03): room for the mount to settle.
    sd0 = np.concatenate([[first_sd] * 3, [0.5] * 3, np.radians([2.0, 2.0, 5.0]), np.radians([0.3] * 3),
                          [0.1] * 3, np.radians([10.0, 10.0, 5.0]), [1e-4] * 3, list(cfg.float_wander_sd)])
    gnss = ins.Gnss(te, ned, fix == 5, episodes_of(fix), np.isin(fix, (4, 5)))
    # No innovation gate: rejecting epochs the filter disagrees with let it
    # drift until it rejected everything (2026-10-03: 80 cm off in 30 s).
    # Fixed epochs are trustworthy, and float error has states of its own.
    res = ins.run(steps, gnss, cfg, lat0, x0, np.diag(sd0 ** 2), smooth=True, smooth_sd=True,
                  keep_attitude=keep_attitude)
    ant = np.column_stack([np.interp(te[inside], res.t, res.antenna_smooth[:, i]) for i in range(3)])
    sd = np.column_stack([np.interp(te[inside], res.t, res.sd_smooth[:, i]) for i in range(3)])
    return inside, ant, sd, res


def fuse(folder: str | Path, noted: Rig, log=print) -> Fused:
    folder = Path(folder)
    e = gnss_epochs(folder / "raw")
    t0 = float(e["utc"].iloc[0])
    utc = PhoneUtc(e["mono_ns"].to_numpy(np.int64), np.round(e["utc"].to_numpy() * 1e3).astype(np.int64) * 1_000_000)
    ep = epoch_arrays(e, t0)
    fix = e["fix"].to_numpy()
    ned, ref = ins.local_ned(e["lat"].to_numpy(), e["lon"].to_numpy(), e["h"].to_numpy())
    report = []
    say = lambda s: (report.append(s), log(s))
    say(f"{len(e)} epochs over {ep['t'][-1]:.0f} s: {np.mean(fix == 4) * 100:.0f}% fixed, {np.mean(fix == 5) * 100:.0f}% float")
    parts = cube_parts(folder, utc, t0, say)
    preps = [prepared(pt, ep, t0, noted.mount_yaw_deg) for pt in parts]
    longest = int(np.argmax([pt.edges[-1] - pt.edges[0] for pt in parts]))
    say(f"rig checks on {parts[longest].name} (reported only):")
    measure_rig(preps[longest], noted, say)
    rig = noted
    say(f"rig used: mount yaw {rig.mount_yaw_deg:+.0f} deg, antenna {rig.antenna} m and pivot {rig.pivot} m "
        f"from the IMU (forward, right, down), timing {rig.lag_s * 1e3:+.0f} ms")

    out = ned.copy()
    sd = np.tile([0.01, 0.01, 0.02], (len(e), 1)).astype(float)
    sd[fix == 5] = [0.3, 0.3, 0.4]
    sd[~np.isin(fix, (4, 5))] = [3.0, 3.0, 5.0]
    source = np.array(["gnss"] * len(e), dtype=object)
    for pt, p in zip(parts, preps):
        inside, ant, s, res = fuse_part(pt, p, rig, ned, fix, ref[0])
        out[inside], sd[inside], source[inside] = ant, s, "fused"
        x = res.nominal_last
        say(f"  {pt.name}: fused {inside.sum()} epochs; "
            f"mount misalignment {np.degrees(x.mount).round(2)} deg; gyro bias {np.degrees(x.bg * 3600).round(0)} deg/h")
    lat, lon, h = ins.ned_to_geodetic(out, ref)
    df = pd.DataFrame({"utc": e["utc"], "t": ep["t"], "fix": fix, "source": source, "lat": lat, "lon": lon, "h": h,
                       "sd_n": sd[:, 0], "sd_e": sd[:, 1], "sd_h": sd[:, 2], "speed": ep["speed"],
                       "gnss_lat": e["lat"], "gnss_lon": e["lon"], "gnss_h": e["h"]})
    fl = (fix == 5) & (source == "fused")
    say(f"float epochs fused: {fl.sum()}; with smoothed height sd under 3 cm: {(fl & (sd[:, 2] < 0.03)).sum()}, "
        f"under 5 cm: {(fl & (sd[:, 2] < 0.05)).sum()}")
    return Fused(df, rig, report)


def crossover_qc(df: pd.DataFrame, radius: float = 0.30, min_s: float = 60.0) -> list[str]:
    """Measured height agreement where the track crosses itself (pairs within
    `radius`, more than `min_s` apart), raw GNSS against fused, fixed and float.

    Fixed against fixed is the yardstick (5.77 cm rms in CLAUDE.md for the
    mower); float against fixed says what the fusion makes of float heights.
    """
    from pyproj import Transformer
    from scipy.spatial import cKDTree

    to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32615", always_xy=True)
    out = []
    for label, lat, lon, h in (("raw GNSS", "gnss_lat", "gnss_lon", "gnss_h"), ("fused", "lat", "lon", "h")):
        e, n = to_utm.transform(df[lon].to_numpy(), df[lat].to_numpy())
        pairs = cKDTree(np.column_stack([e, n])).query_pairs(radius, output_type="ndarray")
        t = df["t"].to_numpy()
        pairs = pairs[np.abs(t[pairs[:, 0]] - t[pairs[:, 1]]) > min_s]
        fix = df["fix"].to_numpy()
        z = df[h].to_numpy()
        for what, a, b in (("fixed-fixed", 4, 4), ("float-fixed", 5, 4), ("float-float", 5, 5)):
            sel = ((fix[pairs[:, 0]] == a) & (fix[pairs[:, 1]] == b)) | ((fix[pairs[:, 0]] == b) & (fix[pairs[:, 1]] == a))
            d = z[pairs[sel, 0]] - z[pairs[sel, 1]]
            if len(d):
                out.append(f"  {label:8s} {what}: {np.sqrt(np.mean(d ** 2)) * 100:5.1f} cm rms, median |d| "
                           f"{np.median(np.abs(d)) * 100:4.1f} cm ({len(d):,} pairs)")
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("session")
    ap.add_argument("out")
    ap.add_argument("--mount-yaw", type=float, default=-90.0, help="IMU x right of forward, deg (note: -90)")
    ap.add_argument("--antenna", type=float, nargs=3, default=(0.25, 0.0, -0.10),
                    help="antenna from the IMU, forward right down, m")
    ap.add_argument("--pivot", type=float, nargs=3, default=(0.0, 0.0, 0.20),
                    help="the non-slipping axle from the IMU, forward right down, m")
    ap.add_argument("--lag", type=float, default=-50.0,
                    help="IMU timing against GNSS, ms (negative: IMU early; the cart run gave -35 to -60)")
    a = ap.parse_args(argv)
    noted = Rig(a.mount_yaw, tuple(a.antenna), tuple(a.pivot), lag_s=a.lag / 1000)
    f = fuse(a.session, noted, log=lambda s: print(s, flush=True))
    f.epochs.to_csv(a.out, index=False)
    print(f"wrote {len(f.epochs)} epochs to {a.out}")
    print("crossovers (height differences, pairs within 30 cm and more than 60 s apart):")
    for line in crossover_qc(f.epochs):
        print(line)


if __name__ == "__main__":
    main(sys.argv[1:])
