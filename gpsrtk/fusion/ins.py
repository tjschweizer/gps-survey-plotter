"""A loosely coupled INS/GNSS filter and smoother for a wheeled rig.

Error-state Kalman filter on the Cube's IMU, local NED, with an RTS
smoother for post-processing. 24 error states:

    0-2 position   3-5 velocity   6-8 attitude (nav-frame small angle)
    9-11 gyro bias   12-14 accelerometer bias
    15-17 mount misalignment (IMU axes against the vehicle's)
    18-20 GNSS float offset (constant within an episode)
    21-23 GNSS float wander (first-order Gauss-Markov)

Measurements:
- GNSS antenna position, through the lever arm. Fixed epochs are taken at
  their word (cm); float epochs also carry the float offset and wander
  states, which is how the IMU separates float error from motion.
- Non-holonomic: the vehicle's pivot point (a fixed axle) doesn't move
  sideways or vertically in vehicle axes. Together with the attitude this
  carries height along the ground's slope when GNSS heights are poor.
- Zero velocity when the IMU says the rig is still.
- Optionally, wheel speed: the pivot point's forward speed in vehicle axes.

The IMU is integrated from the raw FIFOs: each filter step's delta angle
and delta velocity are exact integrals of the high-rate samples, so
vibration averages out rather than aliasing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from pyproj import Transformer

from .rawsession import Imu

OMEGA_E = 7.2921150e-5
G = 9.80


def skew(v: np.ndarray) -> np.ndarray:
    return np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])


def rodrigues(a: np.ndarray) -> np.ndarray:
    th = np.linalg.norm(a)
    K = skew(a)
    if th < 1e-9:
        return np.eye(3) + K
    return np.eye(3) + np.sin(th) / th * K + (1 - np.cos(th)) / th ** 2 * K @ K


def vee_log(R: np.ndarray) -> np.ndarray:
    """Small rotation vector of R (R close to identity)."""
    return 0.5 * np.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]])


def local_ned(lat: np.ndarray, lon: np.ndarray, h: np.ndarray) -> tuple[np.ndarray, tuple]:
    """Positions in a local NED frame at the track's mean point (ground metres, no grid scale)."""
    to_ecef = Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True)
    x, y, z = to_ecef.transform(lon, lat, h)
    lat0, lon0 = np.radians(np.mean(lat)), np.radians(np.mean(lon))
    x0, y0, z0 = to_ecef.transform(np.mean(lon), np.mean(lat), np.mean(h))
    sl, cl, so, co = np.sin(lat0), np.cos(lat0), np.sin(lon0), np.cos(lon0)
    R = np.array([[-sl * co, -sl * so, cl], [-so, co, 0.0], [-cl * co, -cl * so, -sl]])
    return (R @ np.vstack([x - x0, y - y0, z - z0])).T, (lat0, lon0)


@dataclass
class Steps:
    """The IMU as per-step increments on true time (s from the session's first epoch)."""
    t: np.ndarray            # step boundaries, n + 1
    dtheta: np.ndarray       # n x 3, rad
    dvel: np.ndarray         # n x 3, m/s
    still: np.ndarray        # n, bool


def imu_steps(imu: Imu, t0: float, lag: float, rate: float, start: float, end: float) -> Steps:
    """Exact per-step integrals of the FIFO samples; `lag`: IMU timestamps late by this, s."""
    def cumulative(t, x):
        c = np.concatenate([np.zeros((1, 3)), np.cumsum((x[1:] + x[:-1]) / 2 * np.diff(t)[:, None], axis=0)])
        return c

    tg = imu.t_gyro - t0 - lag
    ta = imu.t_accel - t0 - lag
    cg, ca = cumulative(tg, imu.gyro), cumulative(ta, imu.accel)
    tb = np.arange(start, end, 1 / rate)
    dth = np.diff(np.column_stack([np.interp(tb, tg, cg[:, k]) for k in range(3)]), axis=0)
    dv = np.diff(np.column_stack([np.interp(tb, ta, ca[:, k]) for k in range(3)]), axis=0)
    # Still: little rotation and steady specific force over half a second.
    w = np.linalg.norm(dth, axis=1) * rate
    a = np.linalg.norm(dv, axis=1) * rate
    k = max(1, int(rate / 2))
    wm = np.convolve(w, np.ones(k) / k, "same")
    asd = np.sqrt(np.maximum(np.convolve(a ** 2, np.ones(k) / k, "same") - np.convolve(a, np.ones(k) / k, "same") ** 2, 0))
    return Steps(tb, dth, dv, (wm < np.radians(1.0)) & (asd < 0.15))


@dataclass
class Config:
    lever_antenna: np.ndarray            # antenna from the IMU, body FRD, m
    lever_pivot: np.ndarray              # pivot point from the IMU, body FRD, m
    mount_yaw: float = 0.0               # IMU x right of vehicle forward, rad
    arw: float = np.radians(0.05)        # rad/sqrt(s)
    vrw: float = 0.05                    # m/s/sqrt(s)
    gyro_bias_rw: float = np.radians(0.002)   # rad/s/sqrt(s)
    accel_bias_rw: float = 0.001         # m/s^2/sqrt(s)
    mount_rw: float = 1e-5               # rad/sqrt(s)
    fixed_sd: tuple = (0.01, 0.01, 0.02)     # N, E, D, m
    float_white_sd: tuple = (0.02, 0.02, 0.03)
    float_offset_sd: tuple = (0.14, 0.14, 0.276)
    float_wander_sd: tuple = (0.15, 0.15, 0.292)
    float_wander_tc: float = 10.0        # s, assumed by the filter
    nhc_sd: tuple = (0.05, 0.03)         # lateral, vertical, m/s
    zupt_sd: float = 0.005
    wheel_sd: float = 0.03               # m/s


N = 24
P_, V_, A_, BG, BA, MT, FC, FW = (slice(0, 3), slice(3, 6), slice(6, 9), slice(9, 12),
                                  slice(12, 15), slice(15, 18), slice(18, 21), slice(21, 24))


@dataclass
class Nominal:
    p: np.ndarray
    v: np.ndarray
    C: np.ndarray            # body to NED
    bg: np.ndarray
    ba: np.ndarray
    mount: np.ndarray        # misalignment, rad
    fc: np.ndarray
    fw: np.ndarray

    def copy(self) -> "Nominal":
        return Nominal(*(x.copy() for x in (self.p, self.v, self.C, self.bg, self.ba, self.mount, self.fc, self.fw)))

    def correct(self, d: np.ndarray) -> None:
        self.p += d[P_]
        self.v += d[V_]
        self.C = rodrigues(d[A_]) @ self.C
        self.bg += d[BG]
        self.ba += d[BA]
        self.mount += d[MT]
        self.fc += d[FC]
        self.fw += d[FW]

    def minus(self, o: "Nominal") -> np.ndarray:
        d = np.zeros(N)
        d[P_], d[V_] = self.p - o.p, self.v - o.v
        d[A_] = vee_log(self.C @ o.C.T)
        d[BG], d[BA], d[MT] = self.bg - o.bg, self.ba - o.ba, self.mount - o.mount
        d[FC], d[FW] = self.fc - o.fc, self.fw - o.fw
        return d


@dataclass
class Gnss:
    t: np.ndarray            # s
    pos: np.ndarray          # NED antenna, m
    float_: np.ndarray       # bool: a float epoch
    episode: np.ndarray      # int: float episode number, -1 when fixed
    use: np.ndarray          # bool: give it to the filter


@dataclass
class Result:
    t: np.ndarray
    antenna: np.ndarray                  # filter (real time), at the steps
    antenna_smooth: np.ndarray | None
    sd: np.ndarray                       # filter's own antenna position sd
    nominal_last: Nominal | None = None
    extra: dict = field(default_factory=dict)


def initial_attitude(f_mean: np.ndarray, heading: float) -> np.ndarray:
    """Body-to-NED from the mean specific force at rest and the IMU's heading."""
    d = -f_mean / np.linalg.norm(f_mean)                  # down, in body axes
    x = np.array([1.0, 0, 0]) - d[0] * d
    x /= np.linalg.norm(x)
    y = np.cross(d, x)
    level = np.vstack([x, y, d])                          # body -> (forward, right, down)
    c, s = np.cos(heading), np.sin(heading)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1.0]]) @ level


def run(steps: Steps, gnss: Gnss, cfg: Config, lat0: float, x0: Nominal, P0: np.ndarray,
        smooth: bool = True, rate_meas: float = 10.0,
        wheel: tuple[np.ndarray, np.ndarray] | None = None) -> Result:
    """Filter, and smooth if asked. `wheel`: (times, signed forward speed) of the pivot."""
    n = len(steps.dtheta)
    dt = np.diff(steps.t)
    w_ie = OMEGA_E * np.array([np.cos(lat0), 0.0, -np.sin(lat0)])
    g_n = np.array([0, 0, G])
    la, lp = cfg.lever_antenna, cfg.lever_pivot
    cm, sm = np.cos(cfg.mount_yaw), np.sin(cfg.mount_yaw)
    R0 = np.array([[cm, -sm, 0], [sm, cm, 0], [0, 0, 1.0]])   # IMU components to vehicle
    S = np.array([[0, 1.0, 0], [0, 0, 1.0]])
    q_off = np.array(cfg.float_offset_sd) ** 2
    tc = cfg.float_wander_tc
    wander_var = np.array(cfg.float_wander_sd) ** 2

    x = x0.copy()
    P = P0.copy()
    # Measurement times on the step grid
    gi = np.searchsorted(steps.t[1:], gnss.t)
    meas_at = {}
    for j, k in enumerate(gi):
        if 0 <= k < n and gnss.use[j]:
            meas_at.setdefault(int(k), []).append(j)
    nhc_every = max(1, int(round(1 / (rate_meas * np.mean(dt)))))
    wheel_at = {}
    if wheel is not None:
        for tw, sw in zip(*wheel):
            k = int(np.searchsorted(steps.t[1:], tw))
            if 0 <= k < n:
                wheel_at[k] = sw
    started = set()

    ant = np.empty((n, 3))
    sd = np.empty((n, 3))
    if smooth:
        store_pred, store_upd = [], []
        P_pred = np.empty((n, N, N), np.float32)
        P_upd = np.empty((n, N, N), np.float32)
        phis = np.empty((n, N, N), np.float32)

    I = np.eye(N)
    for k in range(n):
        h = dt[k]
        # --- propagate
        dth = steps.dtheta[k] - x.bg * h - x.C.T @ w_ie * h
        dvb = steps.dvel[k] - x.ba * h
        C_mid = x.C @ rodrigues(dth / 2)
        f_n = C_mid @ dvb / h
        v_old = x.v.copy()
        x.v = x.v + C_mid @ dvb + (g_n - 2 * np.cross(w_ie, x.v)) * h
        x.p = x.p + (v_old + x.v) / 2 * h
        x.C = x.C @ rodrigues(dth)
        phi_w = np.exp(-h / tc)
        x.fw = x.fw * phi_w

        F = np.zeros((N, N))
        F[P_, V_] = np.eye(3)
        F[V_, A_] = -skew(f_n)
        F[V_, BA] = -x.C
        F[A_, BG] = -x.C
        Phi = I + F * h
        Phi[FW, FW] = np.eye(3) * phi_w
        Q = np.zeros(N)
        Q[V_] = cfg.vrw ** 2 * h
        Q[A_] = cfg.arw ** 2 * h
        Q[BG] = cfg.gyro_bias_rw ** 2 * h
        Q[BA] = cfg.accel_bias_rw ** 2 * h
        Q[MT] = cfg.mount_rw ** 2 * h
        Q[FW] = wander_var * (1 - phi_w ** 2)
        # A new float episode: its offset is a fresh unknown.
        for j in meas_at.get(k, []):
            e = int(gnss.episode[j])
            if e >= 0 and e not in started:
                started.add(e)
                Q[FC] += q_off
        P = Phi @ P @ Phi.T + np.diag(Q)
        if smooth:
            store_pred.append(x.copy())
            P_pred[k] = P
            phis[k] = Phi

        # --- measurements
        def update(Hm, r, Rm):
            nonlocal P
            Sm = Hm @ P @ Hm.T + Rm
            K = np.linalg.solve(Sm, Hm @ P).T
            d = K @ r
            x.correct(d)
            IKH = I - K @ Hm
            P = IKH @ P @ IKH.T + K @ Rm @ K.T

        for j in meas_at.get(k, []):
            Cl = x.C @ la
            Hm = np.zeros((3, N))
            Hm[:, P_] = np.eye(3)
            Hm[:, A_] = -skew(Cl)
            pred = x.p + Cl
            if gnss.float_[j]:
                Hm[:, FC] = np.eye(3)
                Hm[:, FW] = np.eye(3)
                pred = pred + x.fc + x.fw
                Rm = np.diag(np.array(cfg.float_white_sd) ** 2)
            else:
                Rm = np.diag(np.array(cfg.fixed_sd) ** 2)
            update(Hm, gnss.pos[j] - pred, Rm)

        if k % nhc_every == 0:
            if steps.still[k]:
                Hm = np.zeros((3, N))
                Hm[:, V_] = np.eye(3)
                update(Hm, -x.v, np.eye(3) * cfg.zupt_sd ** 2)
            else:
                wb = steps.dtheta[k] / h - x.bg
                u = x.C.T @ x.v + np.cross(wb, lp)
                Rmt = R0 @ (np.eye(3) - skew(x.mount))
                Hm = np.zeros((2, N))
                Hm[:, V_] = S @ Rmt @ x.C.T
                Hm[:, A_] = S @ Rmt @ x.C.T @ skew(x.v)
                Hm[:, MT] = S @ R0 @ skew(u)
                update(Hm, -(S @ Rmt @ u), np.diag(np.array(cfg.nhc_sd) ** 2))
        if k in wheel_at:
            wb = steps.dtheta[k] / h - x.bg
            u = x.C.T @ x.v + np.cross(wb, lp)
            Rmt = R0 @ (np.eye(3) - skew(x.mount))
            e1 = np.array([[1.0, 0, 0]])
            Hm = np.zeros((1, N))
            Hm[:, V_] = e1 @ Rmt @ x.C.T
            Hm[:, A_] = e1 @ Rmt @ x.C.T @ skew(x.v)
            Hm[:, MT] = e1 @ R0 @ skew(u)
            update(Hm, np.array([wheel_at[k]]) - e1 @ Rmt @ u, np.array([[cfg.wheel_sd ** 2]]))

        ant[k] = x.p + x.C @ la
        Hp = np.zeros((3, N))
        Hp[:, P_] = np.eye(3)
        Hp[:, A_] = -skew(x.C @ la)
        sd[k] = np.sqrt(np.diag(Hp @ P @ Hp.T))
        if smooth:
            store_upd.append(x.copy())
            P_upd[k] = P

    ant_s = None
    if smooth:
        ant_s = np.empty((n, 3))
        xs = store_upd[-1].copy()
        ant_s[-1] = xs.p + xs.C @ la
        for k in range(n - 2, -1, -1):
            Pp = P_pred[k + 1].astype(float)
            A = np.linalg.solve(Pp, phis[k + 1].astype(float) @ P_upd[k].astype(float)).T
            d = A @ xs.minus(store_pred[k + 1])
            xs = store_upd[k].copy()
            xs.correct(d)
            ant_s[k] = xs.p + xs.C @ la
    return Result(steps.t[1:], ant, ant_s, sd, x)
