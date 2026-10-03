"""Step 1 of the fusion plan: measure what the fusion depends on, first.

Run on a session folder:

    python -m gpsrtk.fusion.checks <session folder>

GNSS velocity here is differentiated from the RTK positions (low-passed at
2 Hz, zero phase), not the receiver's Doppler speed and course: on the
LG290P those run about 80 ms behind its own positions (`doppler_lag`).

1. Clocks.
   - Phone against Cube: yaw rates cross-correlated. Tests the TIMESYNC fit
     and the phone's sensor timestamps; GNSS plays no part.
   - IMU against GNSS, three ways. Along-track acceleration on straight
     running, where no lever arm acts. Starts and stops: the IMU integrated
     to velocity from a stop, against GNSS speed. And the course model
     below, which needs the cart's geometry to separate timing from it.
2. Rig geometry.
   - GNSS course at the antenna is the cart's heading plus
     asin(yaw rate x L / speed), L how far the antenna sits ahead of the
     point that doesn't slip sideways (a fixed axle). Timing and L trade
     against each other here (both shift course in proportion to yaw
     rate), so L is given for a range of timings.
   - The antenna's acceleration moved to each IMU through the lever arm
     (yaw acceleration and centripetal terms), with the ground's slope
     (from the track's own heights) tilting the cart: lever arm, mount yaw
     and tilt, against what the IMU measured.
   - The phone against the Cube: mount yaw window by window, and pitch and
     roll stop by stop, to see it wander.
3. Noise and biases in the stops, flagging stops where a note was typed on
   the phone.
4. Vibration: band power moving and stopped, the strongest frequencies,
   accelerometer clipping and whether it biases the measured acceleration.

Angles: headings clockwise from north; yaw rate positive turning right.
Axes: FRD (x forward, y right, z down). Times: seconds from the first epoch.
A positive timing offset means the IMU's timestamps are late against GNSS.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import numpy as np
from scipy.optimize import least_squares, minimize_scalar
from scipy.signal import butter, find_peaks, sosfiltfilt, welch
from scipy.spatial import cKDTree

from .rawsession import Imu, Session, load

FS = 200.0          # common grid for the IMUs, Hz
GNSS_HZ = 10.0
DEG = 180 / np.pi


def lowpass(x: np.ndarray, fc: float, fs: float, order: int = 4) -> np.ndarray:
    return sosfiltfilt(butter(order, fc, fs=fs, output="sos"), x, axis=0)


def bandpass(x: np.ndarray, lo: float, hi: float, fs: float, order: int = 2) -> np.ndarray:
    return sosfiltfilt(butter(order, [lo, hi], btype="band", fs=fs, output="sos"), x, axis=0)


def uniform(t: np.ndarray, x: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    """Resample onto a uniform grid at the stream's mean rate."""
    fs = (len(t) - 1) / (t[-1] - t[0])
    tu = np.arange(t[0], t[-1], 1 / fs)
    return tu, np.column_stack([np.interp(tu, t, x[:, i]) for i in range(x.shape[1])]), fs


def to_grid(t: np.ndarray, x: np.ndarray, grid: np.ndarray, fc: float) -> np.ndarray:
    tu, xu, fs = uniform(t, x)
    if fc < fs / 2:
        xu = lowpass(xu, fc, fs)
    return np.column_stack([np.interp(grid, tu, xu[:, i]) for i in range(x.shape[1])])


def wrap(a):
    return (np.asarray(a) + np.pi) % (2 * np.pi) - np.pi


def integrate(x: np.ndarray, fs: float) -> np.ndarray:
    return np.concatenate([[0.0], np.cumsum((x[1:] + x[:-1]) / 2) / fs])


@dataclass
class Device:
    name: str
    imu: Imu
    gyro: np.ndarray        # on the grid, low-passed at 50 Hz
    accel: np.ndarray
    down: np.ndarray        # unit, device axes, from the stops
    w: np.ndarray           # yaw rate about down, bias removed, rad/s
    clip_t: np.ndarray      # times of clipped accelerometer samples


@dataclass
class Prepared:
    t0: float
    grid: np.ndarray
    ep: dict                # GNSS epoch arrays, times from t0
    stops: list[tuple[float, float]]
    notes: np.ndarray       # times notes were saved on the phone
    devices: dict[str, Device]


def find_stops(t: np.ndarray, speed: np.ndarray, still: float = 0.05,
               min_s: float = 5.0, trim: float = 1.0) -> list[tuple[float, float]]:
    s = np.concatenate([[0], (speed < still).astype(int), [0]])
    edges = np.flatnonzero(np.diff(s))
    return [(t[a] + trim, t[b - 1] - trim) for a, b in zip(edges[::2], edges[1::2])
            if t[b - 1] - t[a] >= min_s]


def in_spans(t: np.ndarray, spans) -> np.ndarray:
    m = np.zeros(len(t), bool)
    for a, b in spans:
        m |= (t >= a) & (t <= b)
    return m


def prepare(sess: Session) -> Prepared:
    e = sess.epochs
    t0 = float(e["utc"].iloc[0])
    t = e["utc"].to_numpy() - t0
    east, north, h = e["e"].to_numpy(), e["n"].to_numpy(), e["h"].to_numpy()
    ve = np.gradient(lowpass(east - east.mean(), 2.0, GNSS_HZ), t)
    vn = np.gradient(lowpass(north - north.mean(), 2.0, GNSS_HZ), t)
    ep = dict(t=t, e=east, n=north, h=h, ve=ve, vn=vn, speed=np.hypot(ve, vn),
              course=np.arctan2(ve, vn),
              doppler_speed=e["speed"].fillna(0.0).to_numpy(),
              doppler_course=np.radians(e["course"].to_numpy()))
    stops = find_stops(t, ep["doppler_speed"])
    lo = max([0.0] + [min(i.t_gyro[0], i.t_accel[0]) - t0 for i in sess.imus.values()]) + 1
    hi = min([t[-1]] + [max(i.t_gyro[-1], i.t_accel[-1]) - t0 for i in sess.imus.values()]) - 1
    grid = np.arange(lo, hi, 1 / FS)
    still = in_spans(grid, stops)
    devices = {}
    for name, imu in sess.imus.items():
        gyro = to_grid(imu.t_gyro - t0, imu.gyro, grid, 50.0)
        accel = to_grid(imu.t_accel - t0, imu.accel, grid, 50.0)
        g = accel[still].mean(axis=0)
        down = -g / np.linalg.norm(g)
        w = gyro @ down
        w = w - w[still].mean()
        full = imu.extra.get("full_scale", {}).get("accel")
        clip_t = (imu.t_accel[(np.abs(imu.accel) >= 0.999 * full).any(axis=1)] - t0
                  if full else np.empty(0))
        devices[name] = Device(name, imu, gyro, accel, down, w, clip_t)
    notes = np.array([float(sess.phone_utc.to_utc_s(x["mono_ns"])) - t0
                      for x in sess.events if x.get("type") == "NOTE"])
    return Prepared(t0, grid, ep, stops, notes, devices)


# --- 1. clocks -----------------------------------------------------------------

def lag_scan(t, a, tb, b, lags, mask=None) -> tuple[np.ndarray, float, float]:
    """Correlation of a(t) with b(t + lag) for each lag; the peak, interpolated."""
    m = np.ones(len(t), bool) if mask is None else mask
    aa = a[m] - a[m].mean()
    corr = np.empty(len(lags))
    for i, lag in enumerate(lags):
        bb = np.interp(t[m] + lag, tb, b)
        bb = bb - bb.mean()
        corr[i] = np.dot(aa, bb) / np.sqrt(np.dot(aa, aa) * np.dot(bb, bb))
    k = int(np.argmax(corr))
    peak = lags[k]
    if 0 < k < len(lags) - 1:
        y0, y1, y2 = corr[k - 1:k + 2]
        if y0 - 2 * y1 + y2 < 0:
            peak = lags[k] + 0.5 * (lags[1] - lags[0]) * (y0 - y2) / (y0 - 2 * y1 + y2)
    return corr, float(peak), float(corr[k])


def doppler_lag(p: Prepared) -> float:
    """How far the receiver's Doppler speed runs behind its positions, s (positive: behind)."""
    m = p.ep["doppler_speed"] > 0.05
    _, lag, _ = lag_scan(p.ep["t"], p.ep["speed"], p.ep["t"],
                         lowpass(p.ep["doppler_speed"], 2.0, GNSS_HZ), np.arange(-0.3, 0.3, 0.002), m)
    return lag


def imu_vs_imu(p: Prepared, a: str, b: str) -> dict:
    """b's timestamps minus a's, from yaw rate (each about its own down axis)."""
    wa, wb = lowpass(p.devices[a].w, 5, FS), lowpass(p.devices[b].w, 5, FS)
    lags = np.arange(-0.1, 0.1, 0.0005)
    _, lag, peak = lag_scan(p.grid, wa, p.grid, wb, lags)
    windows = []
    for s in np.arange(p.grid[0], p.grid[-1] - 30, 60.0):
        m = (p.grid >= s) & (p.grid < s + 60)
        if np.std(wa[m]) > 0.05:
            windows.append(lag_scan(p.grid, wa, p.grid, wb, lags, m)[1])
    gain = float(np.polyfit(wa, np.interp(p.grid + lag, p.grid, wb), 1)[0])
    return dict(lag=lag, peak=peak, windows=np.array(windows), gain=gain)


def reversing(p: Prepared) -> np.ndarray:
    """Epochs moving backwards: course more than 90 deg from the gyro heading."""
    d = next(iter(p.devices.values()))
    psi = integrate(lowpass(d.w, 5, FS), FS)
    t = p.ep["t"]
    m = (p.ep["speed"] > 0.3) & (t > p.grid[0]) & (t < p.grid[-1])
    r = wrap(p.ep["course"] - np.interp(t, p.grid, psi))
    r = wrap(r - np.angle(np.mean(np.exp(1j * r[m]))))
    return m & (np.abs(r) > np.pi / 2)


def along_track_lag(p: Prepared, name: str, rev: np.ndarray) -> dict:
    """IMU timing from along-track acceleration on straight running, whole run and by quarter."""
    d = p.devices[name]
    t = p.ep["t"]
    a = np.gradient(bandpass(p.ep["speed"], 0.05, 1.0, GNSS_HZ), t)
    f = bandpass(d.accel[:, 0], 0.05, 1.0, FS)
    w_at = np.interp(t, p.grid, lowpass(d.w, 1, FS))
    base = (np.abs(w_at) < 0.15) & (t > p.grid[0] + 1) & (t < p.grid[-1] - 1) & ~rev
    lags = np.arange(-0.3, 0.3, 0.002)
    _, lag, peak = lag_scan(t, a, p.grid, f, lags, base)
    edges = np.linspace(t[0], t[-1], 5)
    quarters = [lag_scan(t, a, p.grid, f, lags, base & (t >= lo) & (t < hi))[1]
                for lo, hi in zip(edges[:-1], edges[1:])]
    return dict(lag=lag, peak=peak, epochs=int(base.sum()), quarters=np.array(quarters))


def event_lags(p: Prepared, name: str, span: float = 3.0) -> list[dict]:
    """IMU timing at each start and stop: velocity integrated from rest against GNSS speed.

    The stop's mean along-track specific force (bias plus the ground's tilt)
    is removed before integrating, so the first and last seconds of motion
    carry almost no drift.
    """
    d = p.devices[name]
    g, fx = p.grid, d.accel[:, 0]
    t, sp = p.ep["t"], p.ep["speed"]
    out = []
    for a, b in p.stops:
        bias = fx[(g >= a) & (g <= b)].mean()
        for kind, lo, hi, anchor in (("start", b, b + span, b), ("stop", a - span, a, a)):
            if lo < g[0] + 0.5 or hi > g[-1] - 0.5:
                continue
            mg = (g >= lo - 0.5) & (g <= hi + 0.5)
            v = integrate(fx[mg] - bias, FS)
            v = np.abs(v - np.interp(anchor, g[mg], v))
            k = (t >= lo) & (t <= hi)
            cost = lambda tau: np.mean((sp[k] - np.interp(t[k] + tau, g[mg], v)) ** 2)
            r = minimize_scalar(cost, bounds=(-0.3, 0.3), method="bounded")
            out.append(dict(kind=kind, t=anchor, lag=float(r.x), rms=float(np.sqrt(r.fun)),
                            ok=bool(np.sqrt(r.fun) < 0.05 and abs(r.x) < 0.25)))
    return out


@dataclass
class CourseFit:
    lag: float
    b: float
    d: float
    L: float
    rms_deg: float
    n: int


def course_fit(p: Prepared, name: str, rev: np.ndarray, lag: float | None = None,
               min_speed: float = 0.5) -> CourseFit:
    """GNSS course = heading(t + lag) + b + d t + asin(w L / v), heading from the gyro."""
    dev = p.devices[name]
    w = lowpass(dev.w, 5, FS)
    psi = integrate(w, FS)
    t, v, c = p.ep["t"], p.ep["speed"], p.ep["course"]
    m = (v > min_speed) & ~rev & (t > p.grid[0] + 1) & (t < p.grid[-1] - 1)
    t, v, c = t[m], v[m], c[m]
    b0 = float(np.angle(np.mean(np.exp(1j * (c - np.interp(t, p.grid, psi))))))

    def resid(x, tau):
        b, dd, L = x
        wk = np.interp(t + tau, p.grid, w)
        return wrap(c - np.interp(t + tau, p.grid, psi) - b - dd * t
                    - np.arcsin(np.clip(wk * L / v, -0.99, 0.99)))

    def solve(tau):
        r = least_squares(resid, [b0, 0.0, 0.0], args=(tau,))
        return r.x, float(np.sqrt(np.mean(resid(r.x, tau) ** 2)))

    if lag is None:
        taus = np.arange(-0.3, 0.3, 0.01)
        tau = float(taus[int(np.argmin([solve(x)[1] for x in taus]))])
        r = least_squares(lambda x: resid(x[1:], x[0]), [tau, *solve(tau)[0]])
        lag, x = float(r.x[0]), r.x[1:]
        rms = float(np.sqrt(np.mean(resid(x, lag) ** 2)))
    else:
        x, rms = solve(lag)
    return CourseFit(lag, float(x[0]), float(x[1]), float(x[2]), rms * DEG, int(m.sum()))


# --- 2. rig geometry -------------------------------------------------------------

def local_slope(p: Prepared, radius: float = 3.0) -> tuple[np.ndarray, dict]:
    """Ground gradient (dh/dE, dh/dN) at each epoch from the track's own heights.

    The antenna rides at a fixed height on a rigid cart, so its heights trace
    the ground. A plane through the epochs within `radius` where they spread
    in both directions; the whole track's plane elsewhere.
    """
    e, n, h = p.ep["e"], p.ep["n"], p.ep["h"]
    X = np.column_stack([np.ones_like(e), e - e.mean(), n - n.mean()])
    g_all, *_ = np.linalg.lstsq(X, h, rcond=None)
    out = np.tile(g_all[1:], (len(e), 1))
    tree = cKDTree(np.column_stack([e, n]))
    local = 0
    for i, nb in enumerate(tree.query_ball_point(np.column_stack([e, n]), radius)):
        if len(nb) < 30:
            continue
        de, dn = e[nb] - e[i], n[nb] - n[i]
        if min(np.ptp(de), np.ptp(dn)) < radius:
            continue
        out[i] = np.linalg.lstsq(np.column_stack([np.ones(len(nb)), de, dn]), h[nb], rcond=None)[0][1:]
        local += 1
    return out, dict(grade_pct=float(np.hypot(*g_all[1:]) * 100),
                     plane_rms_cm=float(np.std(h - X @ g_all) * 100), local_share=local / len(e))


@dataclass
class LeverFit:
    mount_yaw_deg: float
    dx: float
    dy: float
    sd: tuple[float, float]
    tilt_deg: tuple[float, float]
    rms: float
    rms_note: float
    rms_zero: float
    clip: tuple[float, float, float, float]     # mean x near/away, rms near/away
    n: int


def lever_fit(p: Prepared, name: str, cf: CourseFit, slope: np.ndarray,
              note: tuple[float, float] | None = None, window: tuple[float, float] | None = None,
              fixed: tuple[float, float] | None = None, g: float = 9.80) -> LeverFit | None:
    """Lever arm (IMU minus antenna, cart axes) and mount yaw from accelerations.

    The antenna's acceleration (from RTK positions) in cart axes, plus the
    ground's slope as the cart's tilt, moved to the IMU through
    yaw-acceleration and centripetal terms, then rotated by the mount yaw,
    against the IMU's specific force; constant terms take the IMU's tilt on
    the cart and its biases. Everything low-passed at 1 Hz, zero phase.
    """
    d = p.devices[name]
    t = p.ep["t"]
    m = (t > p.grid[0] + 2) & (t < p.grid[-1] - 2)
    if window is not None:
        m &= (t >= window[0]) & (t < window[1])
    if m.sum() < 50:
        return None
    t = t[m]
    ts = t + cf.lag
    w_g = lowpass(d.w, 1, FS)
    psi = np.interp(ts, p.grid, integrate(w_g, FS)) + cf.b + cf.d * t
    w, wdot = np.interp(ts, p.grid, w_g), np.interp(ts, p.grid, np.gradient(w_g, 1 / FS))
    ae = np.gradient(lowpass(p.ep["ve"], 1, GNSS_HZ), p.ep["t"])[m]
    an = np.gradient(lowpass(p.ep["vn"], 1, GNSS_HZ), p.ep["t"])[m]
    fwd = np.column_stack([np.sin(psi), np.cos(psi)])
    right = np.column_stack([np.cos(psi), -np.sin(psi)])
    s = slope[m]
    A0 = np.column_stack([ae * fwd[:, 0] + an * fwd[:, 1] + g * np.sum(s * fwd, 1),
                          ae * right[:, 0] + an * right[:, 1] + g * np.sum(s * right, 1)])
    # lever(dx, dy) = wdot (-dy, dx) + w^2 (-dx, -dy), in cart axes
    Lx = np.column_stack([-w ** 2, -wdot])
    Ly = np.column_stack([wdot, -w ** 2])
    f = np.column_stack([np.interp(ts, p.grid, lowpass(d.accel[:, k], 1, FS)) for k in (0, 1)])
    n = len(t)
    one, zero = np.ones(n), np.zeros(n)

    def solve(mu, lever=None):
        cm, sm = np.cos(mu), np.sin(mu)
        # IMU x = cart x cos mu + cart y sin mu; IMU y = -cart x sin mu + cart y cos mu
        r = np.concatenate([f[:, 0] - (cm * A0[:, 0] + sm * A0[:, 1]),
                            f[:, 1] - (-sm * A0[:, 0] + cm * A0[:, 1])])
        M = np.vstack([np.column_stack([cm * Lx + sm * Ly, one, zero]),
                       np.column_stack([-sm * Lx + cm * Ly, zero, one])])
        if lever is not None:
            r = r - M[:, :2] @ np.asarray(lever)
            x = np.concatenate([lever, np.linalg.lstsq(M[:, 2:], r, rcond=None)[0]])
            return x, r - M[:, 2:] @ x[2:], M
        x = np.linalg.lstsq(M, r, rcond=None)[0]
        return x, r - M @ x, M

    mus = np.radians(np.arange(-180, 180, 1.0))
    mu = mus[int(np.argmin([np.sum(solve(u, fixed)[1] ** 2) for u in mus]))]
    mu = float(least_squares(lambda u: solve(u[0], fixed)[1], [mu]).x[0])
    x, res, M = solve(mu, fixed)
    # Smoothed at 1 Hz and sampled at 10 Hz: about 5 epochs per independent one.
    cov = np.linalg.pinv(M.T @ M) * np.sum(res ** 2) / (2 * n - 5) * 5
    near = np.zeros(n, bool)
    if len(d.clip_t):
        i = np.clip(np.searchsorted(d.clip_t, t), 1, len(d.clip_t) - 1)
        near = np.minimum(np.abs(d.clip_t[i] - t), np.abs(d.clip_t[i - 1] - t)) < 0.5
    rx, rr = res[:n], np.column_stack([res[:n], res[n:]])
    rms = lambda r: float(np.sqrt(np.mean(r ** 2))) if r.size else np.nan
    return LeverFit(
        mount_yaw_deg=float(np.degrees(wrap(mu))), dx=float(x[0]), dy=float(x[1]),
        sd=(float(np.sqrt(cov[0, 0])), float(np.sqrt(cov[1, 1]))),
        tilt_deg=(float(np.degrees(x[2] / g)), float(np.degrees(x[3] / g))),
        rms=rms(res),
        rms_note=rms(solve(mu, note)[1]) if note is not None else np.nan,
        rms_zero=rms(solve(mu, (0.0, 0.0))[1]),
        clip=(float(rx[near].mean()) if near.any() else np.nan, float(rx[~near].mean()),
              rms(rr[near]), rms(rr[~near])),
        n=n,
    )


def tilt_at_stops(p: Prepared, name: str) -> np.ndarray:
    """Each stop's pitch and roll of the device, deg, from its gravity vector."""
    d = p.devices[name]
    out = []
    for a, b in p.stops:
        m = (p.grid >= a) & (p.grid <= b)
        down = -d.accel[m].mean(axis=0)
        down /= np.linalg.norm(down)
        out.append(np.degrees([np.arcsin(-down[0]), np.arcsin(down[1])]))
    return np.array(out)


# --- 3. noise and biases at the stops ---------------------------------------------

def normal_gravity(lat_deg: float, h: float) -> float:
    s2 = np.sin(np.radians(lat_deg)) ** 2
    return 9.7803253359 * (1 + 0.00193185265241 * s2) / np.sqrt(1 - 0.00669437999013 * s2) - 3.086e-6 * h


def stop_stats(p: Prepared, name: str, g_ref: float) -> list[dict]:
    imu = p.devices[name].imu
    out = []
    for a, b in p.stops:
        r: dict = {"span": (a, b), "note": bool(np.any((p.notes >= a - 2) & (p.notes <= b + 2)))}
        for kind, t, x in (("gyro", imu.t_gyro - p.t0, imu.gyro), ("accel", imu.t_accel - p.t0, imu.accel)):
            m = (t >= a) & (t <= b)
            if m.sum() < 100:
                continue
            xs, ts = x[m], t[m]
            blocks = np.floor(ts - a).astype(int)
            means = np.array([xs[blocks == k].mean(axis=0) for k in np.unique(blocks) if (blocks == k).sum() > 10])
            # White noise averaged over 1 s: its sd over blocks is the random walk at 1 s.
            r[kind] = dict(mean=xs.mean(axis=0), std=xs.std(axis=0),
                           walk=means.std(axis=0, ddof=1) * 60 if len(means) > 2 else np.full(3, np.nan))
        if "accel" in r:
            r["g_ratio"] = float(np.linalg.norm(r["accel"]["mean"]) / g_ref - 1)
        me = (p.ep["t"] >= a) & (p.ep["t"] <= b)
        r["antenna_mm"] = np.array([np.std(p.ep[k][me]) * 1000 for k in ("e", "n", "h")])
        if "gyro_bias_android" in imu.extra:
            tg = imu.t_gyro - p.t0
            r["android_bias"] = imu.extra["gyro_bias_android"][(tg >= a) & (tg <= b)].mean(axis=0)
        out.append(r)
    return out


# --- 4. vibration ---------------------------------------------------------------------

BANDS = [(0, 2), (2, 10), (10, 50), (50, 200), (200, 1000), (1000, 4000)]


def vibration(p: Prepared, name: str, kind: str = "accel") -> dict:
    d = p.devices[name]
    t = (d.imu.t_accel if kind == "accel" else d.imu.t_gyro) - p.t0
    tu, xu, fs = uniform(t, d.imu.accel if kind == "accel" else d.imu.gyro)
    moving, prev = [], tu[0]
    for a, b in p.stops + [(tu[-1], tu[-1])]:
        if a - prev > 5:
            moving.append((prev, a))
        prev = b
    out: dict = {"fs": fs}
    for label, spans in (("moving", moving), ("stopped", p.stops)):
        parts = []
        for a, b in spans:
            m = (tu >= a) & (tu <= b)
            if m.sum() >= int(fs * 4):
                f, P = welch(xu[m] - xu[m].mean(axis=0), fs=fs, nperseg=int(fs * 2), axis=0)
                parts.append((P, m.sum()))
        if parts:
            P = sum(pp * k for pp, k in parts) / sum(k for _, k in parts)
            df = f[1] - f[0]
            bands = [[float(np.sqrt(P[(f >= lo) & (f < hi), j].sum() * df)) if lo < f[-1] else np.nan
                      for lo, hi in BANDS] for j in range(3)]
            out[label] = dict(f=f, psd=P, bands=np.array(bands))
    if "moving" in out:
        f, P = out["moving"]["f"], out["moving"]["psd"].sum(axis=1)
        pk, _ = find_peaks(P, distance=max(1, int(5 / (f[1] - f[0]))))
        out["peaks_hz"] = sorted(float(f[i]) for i in pk[np.argsort(P[pk])[::-1][:6]])
    return out


# --- report ----------------------------------------------------------------------------

def _v(x, fmt="%.2f"):
    return "/".join("-" if np.isnan(v) else fmt % v for v in np.atleast_1d(x))


def report(folder: str) -> str:
    sess = load(folder)
    p = prepare(sess)
    L: list[str] = []
    say = L.append
    e = sess.epochs
    names = list(p.devices)
    rev = reversing(p)
    rt = p.ep["t"][rev]
    say(f"Session {sess.folder.name}: {len(e)} epochs over {p.ep['t'][-1]:.0f} s, "
        f"{(e['fix'] == 4).mean() * 100:.0f}% fixed; stops " + ", ".join(f"{a:.0f}-{b:.0f}" for a, b in p.stops)
        + " s; notes typed at " + ", ".join(f"{x:.0f}" for x in p.notes) + " s"
        + ((f"; reversing {rev.sum()} epochs (" + ", ".join(
            f"{a:.0f}-{b:.0f}" for a, b in zip(rt[np.r_[True, np.diff(rt) > 0.5]], rt[np.r_[np.diff(rt) > 0.5, True]]))
            + " s)") if rev.any() else ""))

    say("\n1. CLOCKS")
    for part, c in sess.cube_clocks.items():
        say(f"  {part}: TIMESYNC {c.samples} round trips, {c.windows} windows used; Cube vs phone drift "
            f"{c.drift_ppm:+.1f} ppm, residual {c.residual_us:.0f} us rms")
    u = sess.phone_utc
    say(f"  phone vs GNSS (GGA envelope): drift {u.drift_ppm:+.1f} ppm; GGAs arrive a median "
        f"{u.median_above_ms:.1f} ms above it, {u.share_within_5ms * 100:.0f}% within 5 ms")
    say(f"  the receiver's Doppler speed runs {doppler_lag(p) * 1e3:.0f} ms behind its own positions")
    if "cube" in p.devices and "phone" in p.devices:
        r = imu_vs_imu(p, "cube", "phone")
        say(f"  phone vs Cube, yaw rate: phone {r['lag'] * 1e3:+.1f} ms (correlation {r['peak']:.4f}); "
            f"by minute {_v(r['windows'] * 1e3, '%+.1f')} ms; yaw-rate gain {r['gain']:.4f}")
    timing = {}
    for nm in names:
        at = along_track_lag(p, nm, rev)
        ev = event_lags(p, nm)
        good = [x["lag"] for x in ev if x["ok"]]
        timing[nm] = float(np.median(good)) if good else at["lag"]
        say(f"  {nm} vs GNSS, along-track acceleration ({at['epochs']} straight epochs): {at['lag'] * 1e3:+.0f} ms "
            f"(correlation {at['peak']:.2f}); by quarter {_v(at['quarters'] * 1e3, '%+.0f')} ms")
        say(f"  {nm} vs GNSS, starts and stops: " + "; ".join(
            f"{x['kind']} {x['t']:.0f} s {x['lag'] * 1e3:+.0f} ms ({x['rms'] * 100:.1f} cm/s{'' if x['ok'] else ', rejected'})"
            for x in ev) + (f"; median of the good {timing[nm] * 1e3:+.0f} ms" if good else ""))
    vmax = float(np.percentile(p.ep["speed"], 99))
    say(f"  at this run's p99 speed ({vmax:.2f} m/s) 10 ms of timing is {vmax * 10:.0f} mm along track")

    say("\n2. RIG GEOMETRY")
    taus = (-0.12, -0.09, -0.06, -0.03, 0.0)
    fits = {}
    for nm in names:
        free = course_fit(p, nm, rev)
        fits[nm] = course_fit(p, nm, rev, timing[nm])
        Ls = [course_fit(p, nm, rev, x) for x in taus]
        say(f"  {nm}, course model ({free.n} epochs above 0.5 m/s, reversing left out): best fit at "
            f"{free.lag * 1e3:+.0f} ms with the antenna {free.L:+.2f} m ahead of the non-slipping point, "
            f"{free.rms_deg:.2f} deg rms")
        say(f"      ahead of the non-slipping point if the timing is "
            + ", ".join(f"{x * 1e3:+.0f} ms: {c.L:+.2f} m ({c.rms_deg:.2f} deg)" for x, c in zip(taus, Ls)))
    slope, info = local_slope(p)
    say(f"  ground from the track's heights: {info['grade_pct']:.2f}% overall grade, plane fits to "
        f"{info['plane_rms_cm']:.1f} cm rms; local planes at {info['local_share'] * 100:.0f}% of epochs")
    notes = {"cube": (-0.30, 0.0), "phone": (-0.40, 0.0)}
    levers = {}
    for nm in names:
        for tau in sorted({timing[nm], -0.06, 0.0}):
            cf = course_fit(p, nm, rev, tau)
            lf = lever_fit(p, nm, cf, slope, notes.get(nm))
            if tau == timing[nm]:
                levers[nm] = (lf, cf)
            say(f"  {nm} at {tau * 1e3:+.0f} ms: mount yaw {lf.mount_yaw_deg:+.1f} deg; IMU from the antenna "
                f"{lf.dx:+.3f} +/- {lf.sd[0]:.3f} m forward, {lf.dy:+.3f} +/- {lf.sd[1]:.3f} m right "
                f"(note {notes.get(nm)}); tilt and bias terms {lf.tilt_deg[0]:+.2f}/{lf.tilt_deg[1]:+.2f} deg; "
                f"residual {lf.rms:.3f} m/s^2 (note's lever arm {lf.rms_note:.3f}, none {lf.rms_zero:.3f})")
        lf = levers[nm][0]
        if not np.isnan(lf.clip[0]):
            say(f"      clipping: residual x mean {lf.clip[0]:+.3f} within 0.5 s of a clipped sample, "
                f"{lf.clip[1]:+.3f} elsewhere; rms {lf.clip[2]:.3f} vs {lf.clip[3]:.3f} m/s^2")
    if "cube" in p.devices and "phone" in p.devices:
        yaws = []
        for s in np.arange(p.grid[0] + 2, p.grid[-1] - 15, 30.0):
            # Mount yaw needs the cart accelerating: skip windows mostly stopped.
            if np.mean(p.ep["speed"][(p.ep["t"] >= s) & (p.ep["t"] < s + 30)] > 0.3) < 0.5:
                continue
            row = []
            for nm in ("cube", "phone"):
                lf, cf = levers[nm]
                wf = lever_fit(p, nm, cf, slope, window=(s, s + 30), fixed=(lf.dx, lf.dy))
                row.append(wf.mount_yaw_deg if wf else np.nan)
            yaws.append((s, *row))
        y = np.array(yaws)
        say("  mount yaw by 30 s window, Cube / phone / phone minus Cube (deg): " + "; ".join(
            f"{s:.0f} s {c:+.1f}/{ph:+.1f}/{ph - c:+.1f}" for s, c, ph in y))
        tc, tp = tilt_at_stops(p, "cube"), tilt_at_stops(p, "phone")
        say("  pitch/roll at each stop, phone minus Cube (deg): " + "; ".join(
            f"{a:.0f} s {d[0]:+.2f}/{d[1]:+.2f}" for (a, _), d in zip(p.stops, tp - tc)))

    say("\n3. NOISE AND BIASES IN THE STOPS (gyro deg/s, accel m/s^2, ARW deg/sqrt(h), VRW m/s/sqrt(h))")
    g_ref = normal_gravity(float(e["lat"].mean()), float(e["h"].mean()))
    for nm in names:
        stats = stop_stats(p, nm, g_ref)
        for r in stats:
            a, b = r["span"]
            gm, am = r.get("gyro"), r.get("accel")
            say(f"  {nm} {a:.0f}-{b:.0f} s{' (note typed)' if r['note'] else ''}: gyro mean "
                f"{_v(gm['mean'] * DEG, '%+.3f')} sd {_v(gm['std'] * DEG)} ARW {_v(gm['walk'] * DEG)}; accel sd "
                f"{_v(am['std'], '%.3f')} VRW {_v(am['walk'], '%.3f')}, |g| {r['g_ratio'] * 100:+.2f}% of normal; "
                f"antenna sd {_v(r['antenna_mm'], '%.1f')} mm"
                + (f"; Android's bias {_v(r['android_bias'] * DEG, '%+.3f')}" if "android_bias" in r else ""))
        # Typing a note handles the phone, not the Cube.
        quiet = [r for r in stats if "gyro" in r and (nm != "phone" or not r["note"])]
        if len(quiet) > 1:
            means = np.array([r["gyro"]["mean"] for r in quiet]) * DEG * 3600
            say(f"  {nm} gyro bias spread over {len(quiet)} stops: {_v(np.ptp(means, axis=0), '%.0f')} deg/h")
    if sess.ulogs:
        log = next(iter(sess.ulogs.values()))
        off = [log.params.get(f"CAL_GYRO0_{a}OFF") for a in "XYZ"]
        aoff = [log.params.get(f"CAL_ACC0_{a}OFF") for a in "XYZ"]
        if None not in off:
            say(f"  Cube's stored calibration (CAL_*0_*OFF): gyro {_v(np.array(off) * DEG, '%+.3f')} deg/s, "
                f"accel {_v(np.array(aoff), '%+.3f')} m/s^2")

    say("\n4. VIBRATION (band rms x/y/z for " + ", ".join(f"{lo}-{hi}" for lo, hi in BANDS) + " Hz)")
    for nm in names:
        for kind, unit, k in (("accel", "m/s^2", 1.0), ("gyro", "deg/s", DEG)):
            v = vibration(p, nm, kind)
            for label in ("moving", "stopped"):
                if label in v:
                    say(f"  {nm} {kind} {label} ({unit}, {v['fs']:.0f} Hz): "
                        + "; ".join(_v(v[label]["bands"][:, j] * k) for j in range(len(BANDS))))
            if "peaks_hz" in v:
                say(f"  {nm} {kind} strongest frequencies moving: {_v(np.array(v['peaks_hz']), '%.0f')} Hz")
        d = p.devices[nm]
        full = d.imu.extra.get("full_scale", {})
        if full:
            say(f"  {nm} clipping: accelerometer full scale {full['accel']:.0f} m/s^2, {len(d.clip_t)} samples "
                f"({len(d.clip_t) / len(d.imu.t_accel) * 100:.3f}%) in {len(np.unique(np.floor(d.clip_t)))} seconds; "
                f"gyro peak {np.abs(d.imu.gyro).max() * DEG:.0f} of {full['gyro'] * DEG:.0f} deg/s")
    f, P = welch(np.gradient(p.ep["speed"], p.ep["t"]), fs=GNSS_HZ, nperseg=256)
    c = np.cumsum(P) / np.sum(P)
    say(f"  the cart's own motion (GNSS along-track acceleration): half its power below "
        f"{f[np.searchsorted(c, 0.5)]:.2f} Hz, 90% below {f[np.searchsorted(c, 0.9)]:.2f} Hz")
    return "\n".join(L)


if __name__ == "__main__":
    print(report(sys.argv[1]))
