"""The fusion package's readers and step-1 checks, on synthetic data.

The checks are tested on a simulated cart whose geometry and timing are
known: a non-slipping axle, the antenna ahead of it, an IMU behind the
antenna with a mount yaw, and IMU timestamps late by a fixed amount.
"""

import struct

import numpy as np
import pandas as pd
import pytest

from gpsrtk.fusion import checks as C
from gpsrtk.fusion.clock import CubeClock, PhoneUtc, lower_envelope
from gpsrtk.fusion.rawsession import Imu
from gpsrtk.fusion.ulog import MAGIC, ULog, fifo


# --- ULog ------------------------------------------------------------------------

def msg(kind, body):
    return struct.pack("<HB", len(body), ord(kind)) + body


def ulog(*messages):
    return MAGIC + bytes([1]) + struct.pack("<Q", 0) + b"".join(messages)


def test_ulog_nested_arrays_chars_and_dropped_padding():
    vec = msg("F", b"vec:float[3] v;uint8_t flag;uint8_t[3] _padding0;")
    top = msg("F", b"pos:uint64_t timestamp;vec a;vec[2] b;char[4] name;int16_t k;uint8_t[2] _padding0;")
    rec = lambda ts, f: (struct.pack("<Q", ts) + b"".join(struct.pack("<3fB3x", f + i, f + i + 1, f + i + 2, 1)
                                                          for i in (0, 10, 20)) + b"ab\0\0" + struct.pack("<h", -3) + b"\0\0")
    log = ULog.parse(ulog(vec, top, msg("A", struct.pack("<BH", 0, 7) + b"pos"),
                          msg("P", bytes([12]) + b"float MPC_XY" + struct.pack("<f", 2.5)),
                          msg("D", struct.pack("<H", 7) + rec(100, 1.0)),
                          msg("D", struct.pack("<H", 7) + rec(200, 3.0)[:-2]),
                          msg("O", struct.pack("<H", 250))))
    r = log.topic("pos")
    assert r["timestamp"].tolist() == [100, 200]
    assert r["a"]["v"][:, 0].tolist() == [1.0, 3.0]
    assert r["b"][:, 1]["v"][:, 2].tolist() == [23.0, 25.0]
    assert r["name"].tolist() == [b"ab", b"ab"] and r["k"].tolist() == [-3, -3]
    assert log.params == {"MPC_XY": 2.5}
    assert log.dropouts == [(200, 250)]


def fifo_log(period_us, n_records, per_record=20, jitter_us=150, lose=(), seed=1, value=None):
    """A gyro FIFO stream from a sensor whose true sample period is `period_us`
    (nominal 125), read late by up to `jitter_us`, with some records lost."""
    rng = np.random.default_rng(seed)
    fmt = msg("F", b"sensor_gyro_fifo:uint64_t timestamp;uint64_t timestamp_sample;uint32_t device_id;"
                   b"float dt;float scale;int16_t[32] x;int16_t[32] y;int16_t[32] z;uint8_t samples;uint8_t[3] _padding0;")
    out = [fmt, msg("A", struct.pack("<BH", 0, 1) + b"sensor_gyro_fifo")]
    truth = []
    for k in range(n_records):
        newest = 1_000_000 + (k + 1) * per_record * period_us
        idx = k * per_record + np.arange(per_record)
        if k in lose:
            continue
        truth.append(newest)
        read = int(newest + rng.uniform(0, jitter_us))
        vals = (idx % 1000 if value is None else np.full(len(idx), value)).astype(np.int16)
        body = (struct.pack("<QQIff", read + 50, read, 1, 125.0, 0.001) + struct.pack("<32h", *np.resize(vals, 32))
                + bytes(64) + bytes(64) + bytes([per_record]))
        out.append(msg("D", struct.pack("<H", 1) + body))
    return ULog.parse(ulog(*out)), np.array(truth, float)


def test_fifo_rebuilds_the_sensor_clock():
    # 0.7% slow against nominal 125 us, jittered reads, two records lost
    log, truth = fifo_log(125.9, 4000, lose=(1000, 2500))
    t, xyz = fifo(log, "sensor_gyro_fifo")
    assert len(t) == 20 * (4000 - 2)
    assert np.all(np.diff(t) > 0)
    newest = t[19::20]
    # within the jitter's floor plus a little, and never after the read
    err = newest - truth
    assert np.abs(err - np.median(err)).max() < 30
    assert abs(np.median(err)) < 30


# --- clocks ----------------------------------------------------------------------

def test_lower_envelope_follows_the_quickest():
    rng = np.random.default_rng(2)
    x = (10 ** 15 + np.arange(3000) * 100_000_000).astype(np.int64)
    true = 1_000_000 + 3e-6 * (x - x[0])
    late = np.where(np.arange(3000) % 97 == 0, 0, 35_000_000 + rng.integers(0, 3_000_000, 3000))
    line = lower_envelope(x, (true + late).astype(np.int64))
    assert line.b * 1e6 == pytest.approx(3.0, abs=0.05)
    assert abs(line(x[1500]) - true[1500]) < 50_000


def test_cube_clock_and_phone_utc():
    sent = (10 ** 15 + np.arange(600) * 1_000_000_000).astype(np.int64)
    rtt = np.where(np.arange(600) % 7 == 0, 300_000, 400_000 + (np.arange(600) * 7919) % 2_600_000)
    mid = sent + rtt // 2 + np.where(rtt < 400_000, 0, rtt // 4)
    cube = (mid + 5e9 - 2e-6 * (mid - sent[0])).astype(np.int64)
    k = CubeClock(sent, sent + rtt, cube)
    assert k.drift_ppm == pytest.approx(-2.0, abs=0.2)
    phone = 10 ** 15 + 300e9
    assert k.to_phone_ns((phone + 5e9 - 2e-6 * 300e9) / 1e3) == pytest.approx(phone, abs=500_000)
    mono = (10 ** 15 + np.arange(1000) * 100_000_000).astype(np.int64)
    utc = (1_790_000_000_000 + np.arange(1000) * 100) * 1_000_000
    u = PhoneUtc(mono + np.where(np.arange(1000) % 50 == 0, 0, 30_000_000), utc)
    assert u.to_utc_s(mono[10]) == pytest.approx(utc[10] / 1e9, abs=1e-4)


# --- the checks on a simulated cart -------------------------------------------------

TAU = 0.040          # IMU timestamps late by this, s
L_AHEAD = 0.25       # antenna ahead of the non-slipping axle, m
LEVER = (-0.30, 0.05)  # IMU minus antenna, cart axes (forward, right), m
MOUNT_YAW = np.radians(2.0)


def simulated_cart(duration=200.0):
    """A cart driven through stops, varying speed and alternating turns."""
    fs = 1000.0
    t = np.arange(0, duration, 1 / fs)
    ramp = lambda x: np.clip(x, 0, 1) ** 2 * (3 - 2 * np.clip(x, 0, 1))
    go = ramp((t - 10) / 2) * (1 - ramp((t - (duration - 12)) / 2))
    u = go * (1.3 + 0.6 * np.sin(2 * np.pi * t / 37))
    w = go * 0.5 * np.sin(2 * np.pi * t / 11) * (0.6 + 0.4 * np.sin(2 * np.pi * t / 29))
    psi = C.integrate(w, fs)
    fwd = np.column_stack([np.sin(psi), np.cos(psi)])
    right = np.column_stack([np.cos(psi), -np.sin(psi)])
    axle = np.column_stack([C.integrate(u * fwd[:, 0], fs), C.integrate(u * fwd[:, 1], fs)])
    ant = axle + L_AHEAD * fwd
    imu_pt = ant + LEVER[0] * fwd + LEVER[1] * right
    acc = np.gradient(np.gradient(imu_pt, 1 / fs, axis=0), 1 / fs, axis=0)
    ax, ay = np.sum(acc * fwd, 1), np.sum(acc * right, 1)
    cm, sm = np.cos(MOUNT_YAW), np.sin(MOUNT_YAW)
    f_imu = np.column_stack([cm * ax + sm * ay, -sm * ax + cm * ay, np.full(len(t), -9.80)])
    gyro = np.column_stack([np.zeros(len(t)), np.zeros(len(t)), w])
    return t, ant, f_imu, gyro, u


def prepared_cart():
    t, ant, f_imu, gyro, _ = simulated_cart()
    rng = np.random.default_rng(3)
    te = np.arange(0, t[-1], 0.1)
    e = np.interp(te, t, ant[:, 0]) + rng.normal(0, 0.003, len(te))
    n = np.interp(te, t, ant[:, 1]) + rng.normal(0, 0.003, len(te))
    ve = np.gradient(C.lowpass(e - e.mean(), 2.0, 10.0), te)
    vn = np.gradient(C.lowpass(n - n.mean(), 2.0, 10.0), te)
    speed = np.hypot(ve, vn)
    true_speed = np.interp(te, t, np.hypot(*np.gradient(ant, 1 / 1000, axis=0).T))
    ep = dict(t=te, e=e, n=n, h=np.zeros_like(te), ve=ve, vn=vn, speed=speed,
              course=np.arctan2(ve, vn), doppler_speed=true_speed, doppler_course=np.arctan2(ve, vn))
    grid = np.arange(1.0, t[-1] - 1, 1 / C.FS)
    # IMU stamps late by TAU: the sample stamped g describes the instant g - TAU.
    gyro_g = np.column_stack([np.interp(grid - TAU, t, gyro[:, k]) for k in range(3)])
    acc_g = np.column_stack([np.interp(grid - TAU, t, f_imu[:, k]) for k in range(3)])
    imu = Imu("imu", grid, gyro_g, grid, acc_g)
    stops = C.find_stops(te, true_speed)
    dev = C.Device("imu", imu, gyro_g, acc_g, np.array([0, 0, 1.0]), gyro_g[:, 2], np.empty(0))
    return C.Prepared(0.0, grid, ep, stops, np.empty(0), {"imu": dev})


@pytest.fixture(scope="module")
def cart():
    return prepared_cart()


def test_stops_found(cart):
    assert len(cart.stops) == 2


def test_along_track_timing(cart):
    rev = np.zeros(len(cart.ep["t"]), bool)
    assert C.along_track_lag(cart, "imu", rev)["lag"] == pytest.approx(TAU, abs=0.015)


def test_course_model_gives_the_pivot_distance_at_known_timing(cart):
    rev = np.zeros(len(cart.ep["t"]), bool)
    fit = C.course_fit(cart, "imu", rev, TAU)
    assert fit.L == pytest.approx(L_AHEAD, abs=0.02)
    assert fit.rms_deg < 1.0
    free = C.course_fit(cart, "imu", rev)
    assert free.lag == pytest.approx(TAU, abs=0.03)


def test_lever_arm_and_mount_yaw(cart):
    rev = np.zeros(len(cart.ep["t"]), bool)
    cf = C.course_fit(cart, "imu", rev, TAU)
    lf = C.lever_fit(cart, "imu", cf, np.zeros((len(cart.ep["t"]), 2)))
    assert lf.dx == pytest.approx(LEVER[0], abs=0.02)
    assert lf.dy == pytest.approx(LEVER[1], abs=0.02)
    assert lf.mount_yaw_deg == pytest.approx(2.0, abs=0.3)
    assert lf.rms < lf.rms_zero


# --- the INS filter on the simulated cart ---------------------------------------------

from gpsrtk.fusion import ins  # noqa: E402


@pytest.fixture(scope="module")
def ins_cart():
    """The simulated cart as the filter sees it: IMU increments at 50 Hz, GNSS at 10 Hz."""
    t, ant, f_imu, gyro, u = simulated_cart(120.0)
    fs, rate = 1000.0, 50.0
    tb = np.arange(1.0, t[-1] - 1, 1 / rate)
    cum = lambda x: np.column_stack([np.interp(tb, t, C.integrate(x[:, k], fs)) for k in range(3)])
    # NED: the simulation's axes are (east, north); the cart starts still and level.
    still = np.interp(tb[:-1], t, (np.hypot(*np.gradient(ant, 1 / fs, axis=0).T) < 1e-3).astype(float)) > 0.5
    steps = ins.Steps(tb, np.diff(cum(gyro), axis=0), np.diff(cum(f_imu), axis=0), still)
    te = np.arange(1.0, t[-1] - 1, 0.1)
    ned = np.column_stack([np.interp(te, t, ant[:, 1]), np.interp(te, t, ant[:, 0]), np.zeros(len(te))])
    # Lever arms in the IMU's axes: the antenna from the IMU, and the axle L_AHEAD behind the antenna.
    mu = MOUNT_YAW
    to_imu = np.array([[np.cos(mu), np.sin(mu), 0], [-np.sin(mu), np.cos(mu), 0], [0, 0, 1.0]])
    la = to_imu @ -np.array([LEVER[0], LEVER[1], 0.0])
    lp = to_imu @ (-np.array([LEVER[0], LEVER[1], 0.0]) - [L_AHEAD, 0, 0])
    cfg = ins.Config(lever_antenna=la, lever_pivot=lp, mount_yaw=mu)
    psi0 = 0.0  # heading at rest: the cart starts facing north
    Cbn = ins.initial_attitude(np.array([0, 0, -9.80]), psi0 + MOUNT_YAW)
    x0 = ins.Nominal(ned[0] - Cbn @ la, np.zeros(3), Cbn, *(np.zeros(3) for _ in range(5)))
    sd0 = np.concatenate([[0.02] * 6, np.radians([0.3, 0.3, 1.0]), np.radians([0.05] * 3), [0.05] * 3,
                          np.radians([0.5] * 3), [1e-4] * 3, [0.15, 0.15, 0.29]])
    return steps, te, ned, cfg, np.diag(sd0 ** 2), np.interp(te, t, u)


def gnss_with_gap(te, ned, gap):
    m = (te >= gap[0]) & (te < gap[1])
    return ins.Gnss(te, ned, np.zeros(len(te), bool), np.where(m, 0, -1), ~m), m


def test_ins_follows_fixed_gnss(ins_cart):
    steps, te, ned, cfg, P0, _ = ins_cart
    x0 = ins.Nominal(ned[0] - ins.initial_attitude(np.array([0, 0, -9.8]), MOUNT_YAW) @ cfg.lever_antenna,
                     np.zeros(3), ins.initial_attitude(np.array([0, 0, -9.8]), MOUNT_YAW),
                     *(np.zeros(3) for _ in range(5)))
    g, _ = gnss_with_gap(te, ned, (-1, -1))
    r = ins.run(steps, g, cfg, 0.7, x0, P0, smooth=False)
    est = np.column_stack([np.interp(te, r.t, r.antenna[:, k]) for k in range(3)])
    assert np.sqrt(np.mean(np.sum((est - ned)[20:] ** 2, axis=1))) < 0.01


def test_ins_bridges_a_gap_better_than_a_straight_line(ins_cart):
    steps, te, ned, cfg, P0, axle_speed = ins_cart
    Cbn = ins.initial_attitude(np.array([0, 0, -9.8]), MOUNT_YAW)
    x0 = ins.Nominal(ned[0] - Cbn @ cfg.lever_antenna, np.zeros(3), Cbn, *(np.zeros(3) for _ in range(5)))
    g, m = gnss_with_gap(te, ned, (50.0, 70.0))
    r = ins.run(steps, g, cfg, 0.7, x0, P0, smooth=True)
    est = np.column_stack([np.interp(te[m], r.t, r.antenna_smooth[:, k]) for k in range(3)])
    smooth_err = np.sqrt(np.mean(np.sum((est - ned[m])[:, :2] ** 2, axis=1)))
    line = np.column_stack([np.interp(te[m], te[~m], ned[~m, k]) for k in range(3)])
    line_err = np.sqrt(np.mean(np.sum((line - ned[m])[:, :2] ** 2, axis=1)))
    assert smooth_err < 0.02 and smooth_err < line_err / 20
    # With a noisy accelerometer, a wheel-speed sensor holds the real-time solution
    # together through the gap.
    rng = np.random.default_rng(4)
    h = np.diff(steps.t)[:, None]
    noisy = ins.Steps(steps.t, steps.dtheta, steps.dvel + rng.normal(0, 0.3, steps.dvel.shape) * np.sqrt(h), steps.still)
    rw = ins.run(noisy, g, cfg, 0.7, x0, P0, smooth=False, wheel=(te, axle_speed))
    rf = ins.run(noisy, g, cfg, 0.7, x0, P0, smooth=False)
    err = lambda res: np.sqrt(np.mean(np.sum((np.column_stack(
        [np.interp(te[m], res.t, res.antenna[:, k]) for k in range(3)]) - ned[m])[:, :2] ** 2, axis=1)))
    assert err(rw) < err(rf)


# --- the comparison plots -----------------------------------------------------------------

def test_compare_plots_render(tmp_path):
    """All four figures and the table, from results shaped like compare.run's."""
    from PIL import Image

    from gpsrtk.fusion import compare

    rng = np.random.default_rng(0)
    errs = lambda scale: (np.abs(rng.normal(0, scale, 50)), np.abs(rng.normal(0, scale, 50)))
    case = lambda: {"filter": errs(0.2), "smoother": errs(0.05), "gnss": errs(0.3), "line": errs(2.0)}
    res = {name: {(L, mode, tc): case() for L in compare.LENGTHS for mode, tc in compare.MODES}
           for name in compare.OPTIONS}
    timing = {lag: {(30, "outage", 0): case(), (30, "float", 10.0): case()} for lag in compare.LAGS}
    t = np.arange(0, 60, 0.1)
    ex = {"episode": (0.0, 60.0), "t": t, "mask": np.ones(len(t), bool),
          "gnss": rng.normal(0, 0.3, (len(t), 3)), "line": rng.normal(0, 0.1, (len(t), 3))}
    for name in compare.OPTIONS:
        for kind in ("filter", "smoother"):
            ex[f"{name} {kind}"] = rng.normal(0, 0.1, (len(t), 3))
    paths = compare.plots({"res": res, "timing": timing, "example": ex}, tmp_path)
    for p in paths[:4]:
        w, h = Image.open(p).size
        assert w > 1000 and h > 800
    rows = paths[4].read_text().splitlines()
    # Per case: filter and smoother for each option, and the two baselines once.
    cases = len(compare.LENGTHS) * len(compare.MODES)
    assert rows[0].startswith("case,length_s") and len(rows) == 1 + cases * (2 * len(compare.OPTIONS) + 2)


def test_fifo_integrals_bridge_short_holes_and_leave_long_ones_missing():
    from gpsrtk.fusion.ulog import fifo_integrals

    # 1 rad/s throughout (1000 x 0.001); records lost: one, a run of 12 (30 ms), a run of 40 (100 ms).
    lose = {500} | set(range(1500, 1512)) | set(range(2500, 2540))
    log, _ = fifo_log(125.9, 4000, jitter_us=100, lose=lose, value=1000)
    edges = np.arange(1.02, 1.0 + 4000 * 20 * 125.9e-6 - 0.02, 0.005)
    I, clipped = fifo_integrals(log, "sensor_gyro_fifo", lambda us: np.asarray(us, float) / 1e6, edges)
    ok = np.isfinite(I).all(axis=1)
    assert clipped == 0
    assert np.allclose(I[ok, 0], 0.005, rtol=2e-3)
    hole = (edges[:-1] > 1.0 + 2500 * 20 * 125.9e-6) & (edges[1:] < 1.0 + 2540 * 20 * 125.9e-6)
    assert (~ok[hole]).all(), "a 100 ms hole must read as missing"
    assert ok[~hole].mean() > 0.999, "short holes are bridged"


def test_local_ned_goes_back_exactly():
    lat = np.array([45.00, 45.0002, 44.9999])
    lon = np.array([-100.00, -100.0003, -99.9998])
    h = np.array([250.0, 251.3, 249.2])
    ned, ref = ins.local_ned(lat, lon, h)
    la, lo, hh = ins.ned_to_geodetic(ned, ref)
    assert np.allclose(la, lat, atol=1e-10) and np.allclose(lo, lon, atol=1e-10) and np.allclose(hh, h, atol=1e-4)


def test_ins_ignores_epochs_outside_the_imu(ins_cart):
    """An epoch before the IMU starts once landed on its first step."""
    steps, te, ned, cfg, P0, _ = ins_cart
    Cbn = ins.initial_attitude(np.array([0, 0, -9.8]), MOUNT_YAW)
    x0 = ins.Nominal(ned[0] - Cbn @ cfg.lever_antenna, np.zeros(3), Cbn, *(np.zeros(3) for _ in range(5)))
    g, _ = gnss_with_gap(te, ned, (-1, -1))
    early = ins.Gnss(np.r_[0.5, te], np.vstack([ned[0] + 100.0, ned]), np.r_[False, g.float_], np.r_[-1, g.episode], np.r_[True, g.use])
    a = ins.run(steps, g, cfg, 0.7, x0, P0, smooth=False)
    b = ins.run(steps, early, cfg, 0.7, x0, P0, smooth=False)
    assert np.allclose(a.antenna, b.antenna)


def test_float_runs_are_numbered_and_the_mount_turns_the_lever_arm():
    from gpsrtk.fusion.fuse import Rig, episodes_of

    assert episodes_of(np.array([4, 5, 5, 4, 5, 0, 5])).tolist() == [-1, 0, 0, -1, 1, -1, 2]
    # Mounted with its x axis to the left: the vehicle's forward is the IMU's +y.
    assert np.allclose(Rig(-90.0, (0.25, 0, -0.1), (0, 0, 0.2), 0.0).to_imu((0.25, 0.0, -0.1)), [0.0, 0.25, -0.1])


# --- maps and the animation from a fused session ------------------------------------

def fused_session(tmp_path):
    """A synthetic fused.csv: passes 0.5 m apart over a 20 x 16 m tilted plane at
    the example site, mostly fixed, some fused float, some float the IMU
    didn't cover; and the site file."""
    from pyproj import Transformer

    from gpsrtk.site import example_site
    from synthetic import ORIGIN_E, ORIGIN_N

    rows = []
    t = 0.0
    for k in range(32):
        ys = np.arange(0, 16, 0.1)
        for y in (ys if k % 2 == 0 else ys[::-1]):
            rows.append((t, 2 + k * 0.5, 2 + y))
            t += 0.1
    t, x, y = map(np.array, zip(*rows))
    fix = np.where((np.arange(len(t)) // 200) % 5 == 4, 5, 4)
    source = np.where((fix == 5) & (np.arange(len(t)) % 2 == 0), "gnss", "fused")
    e, n = ORIGIN_E + x, ORIGIN_N + y
    lon, lat = Transformer.from_crs("EPSG:32615", "EPSG:4326", always_xy=True).transform(e, n)
    h = 250.0 + 0.03 * x + 0.01 * y
    df = pd.DataFrame({"utc": 1_790_000_000 + t, "t": t, "fix": fix, "source": source,
                       "lat": lat, "lon": lon, "h": h, "sd_h": 0.02})
    csv = tmp_path / "fused.csv"
    df.to_csv(csv, index=False)
    site = tmp_path / "site.json"
    example_site().save(site)
    return csv, site, df


def test_point_set_takes_float_only_where_nothing_fixed(tmp_path):
    from gpsrtk.fusion.maps import point_set

    _, _, df = fused_session(tmp_path)
    ps, info = point_set(df)
    assert info["fixed"] == (df.fix == 4).sum()
    kept_float = ps.df[ps.df["fix"] == 5]
    # Every float point kept sits in a 0.5 m cell no fixed point reached, and none the IMU didn't cover.
    fixed = ps.df[ps.df["fix"] == 4]
    key = lambda d: set(zip(np.floor(d["e"] / 0.5).astype(int), np.floor(d["n"] / 0.5).astype(int)))
    assert not (key(kept_float) & key(fixed))
    assert len(kept_float) == info["float_added"] <= ((df.fix == 5) & (df.source == "fused")).sum()


def test_maps_and_animation_render(tmp_path):
    from PIL import Image

    from gpsrtk.fusion.animate import animate
    from gpsrtk.fusion.maps import maps

    csv, site, _ = fused_session(tmp_path)
    lines = maps(csv, site, tmp_path / "plots")
    for name in ("heightmap.png", "contours.png", "drainage.png"):
        assert Image.open(tmp_path / "plots" / name).size[0] > 600
    assert any("measured share" in line for line in lines)
    gif = animate(csv, site, tmp_path / "plots" / "mowing.gif", seconds=0.5, log=lambda s: None,
                  append=[(tmp_path / "plots" / "heightmap.png", 5), (tmp_path / "plots" / "drainage.png", 10)])
    im = Image.open(gif)
    assert im.n_frames == 5 + 2 and im.size[0] > 600
    durations = []
    for k in range(im.n_frames):
        im.seek(k)
        durations.append(im.info["duration"])
    assert durations[-3:] == [3000, 5000, 10000]
