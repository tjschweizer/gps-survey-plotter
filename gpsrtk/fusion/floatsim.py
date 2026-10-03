"""How well the IMU carries a survey through RTK float: simulated on a real run.

Take a session that was RTK fixed throughout, so its positions are truth to
about a centimetre. Replace stretches of it ("episodes") with float, made
by adding float-like errors to the fixed positions, or drop GNSS entirely
(an outage). Run the INS filter and smoother on the real IMU data against
that, and score the antenna position inside the episodes against the
withheld fixed positions.

Float errors follow the measured figures in CLAUDE.md: per episode an
offset with sd 27.6 cm, plus within-episode wander with sd 29.2 cm
(heights; horizontal taken as half per axis, an assumption). How fast the
wander moves is not known, so it is a parameter: its correlation time.

    python -m gpsrtk.fusion.floatsim <session folder>
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import numpy as np

from . import checks as C
from . import ins
from .rawsession import load

FLOAT_OFFSET = np.array([0.14, 0.14, 0.276])
FLOAT_WANDER = np.array([0.15, 0.15, 0.292])


@dataclass
class Context:
    t: np.ndarray            # epoch times, s
    truth: np.ndarray        # NED antenna positions from the fixed solution
    steps: ins.Steps
    cfg: ins.Config
    lat0: float
    x0: ins.Nominal
    P0: np.ndarray
    moving: np.ndarray       # epochs moving above 0.3 m/s
    speed: np.ndarray        # forward speed at the epochs, negative reversing (from positions)


def setup(folder: str, lag: float = -0.06, imu: str = "cube", rate: float = 50.0,
          lever_antenna=(0.274, 0.023, 0.0), pivot_ahead: float = 0.10,
          mount_yaw_deg: float = -0.3, cfg_overrides: dict | None = None) -> Context:
    sess = load(folder)
    p = C.prepare(sess)
    e = sess.epochs
    ned, (lat0, *_) = ins.local_ned(e["lat"].to_numpy(), e["lon"].to_numpy(), e["h"].to_numpy())
    t = p.ep["t"]
    start, end = p.grid[0] + 0.5, p.grid[-1] - 0.5
    steps = ins.imu_steps(sess.imus[imu], p.t0, lag, rate, start, end)
    la = np.array(lever_antenna)
    lp = la + np.array([pivot_ahead, 0.0, 0.0])
    cfg = ins.Config(lever_antenna=la, lever_pivot=lp, mount_yaw=np.radians(mount_yaw_deg))
    for k, v in (cfg_overrides or {}).items():
        setattr(cfg, k, v)

    # Heading at the start from the gyro's heading fitted to GNSS course.
    rev = C.reversing(p)
    cf = C.course_fit(p, imu, rev, lag)
    psi = C.integrate(C.lowpass(p.devices[imu].w, 5, C.FS), C.FS)
    heading = float(np.interp(start + lag, p.grid, psi) + cf.b + cf.d * start) + cfg.mount_yaw
    still = steps.still.copy()
    first = np.flatnonzero(~still)[0] if (~still).any() else len(still)
    sel = slice(0, max(first, 10))
    hstep = np.diff(steps.t)[sel][:, None]
    f_mean = (steps.dvel[sel] / hstep).mean(axis=0)
    Cbn = ins.initial_attitude(f_mean, heading)
    w_ie = ins.OMEGA_E * np.array([np.cos(lat0), 0.0, -np.sin(lat0)])
    bg = (steps.dtheta[sel] / hstep).mean(axis=0) - Cbn.T @ w_ie
    ant0 = np.array([np.interp(start, t, ned[:, k]) for k in range(3)])
    x0 = ins.Nominal(ant0 - Cbn @ la, np.zeros(3), Cbn, bg, np.zeros(3), np.zeros(3), np.zeros(3), np.zeros(3))
    sd0 = np.concatenate([[0.02] * 3, [0.02] * 3, np.radians([0.3, 0.3, 3.0]), np.radians([0.05] * 3),
                          [0.05] * 3, np.radians([0.5] * 3), FLOAT_OFFSET * 1e-3, FLOAT_WANDER])
    speed = np.where(rev, -p.ep["speed"], p.ep["speed"])
    return Context(t, ned, steps, cfg, lat0, x0, np.diag(sd0 ** 2), p.ep["speed"] > 0.3, speed)


def make_wheel(ctx: Context, rng: np.random.Generator, scale_error: float = 0.01,
               noise: float = 0.02) -> tuple[np.ndarray, np.ndarray]:
    """A simulated wheel-speed sensor at 10 Hz: 1% scale error the filter doesn't know, 2 cm/s noise."""
    return ctx.t, ctx.speed * (1 + scale_error) + rng.normal(0, noise, len(ctx.t))


def episodes(ctx: Context, length: float, count: int, rng: np.random.Generator,
             gap: float = 25.0) -> list[tuple[float, float]]:
    """Non-overlapping episodes starting while moving, `gap` s of fixed between them."""
    lo, hi = ctx.steps.t[0] + 30.0, ctx.steps.t[-1] - 5.0 - length
    starts = ctx.t[ctx.moving & (ctx.t > lo) & (ctx.t < hi)]
    out: list[tuple[float, float]] = []
    for _ in range(200):
        if len(out) == count or len(starts) == 0:
            break
        s = float(rng.choice(starts))
        if all(s > b + gap or s + length < a - gap for a, b in out):
            out.append((s, s + length))
    return sorted(out)


def gauss_markov(n: int, dt: float, tc: float, sd: np.ndarray, rng) -> np.ndarray:
    a = np.exp(-dt / tc)
    x = np.empty((n, 3))
    x[0] = rng.normal(0, sd)
    for k in range(1, n):
        x[k] = a * x[k - 1] + rng.normal(0, sd * np.sqrt(1 - a * a))
    return x


def make_gnss(ctx: Context, eps: list[tuple[float, float]], mode: str, tc: float,
              rng: np.random.Generator) -> ins.Gnss:
    """mode 'outage': no GNSS in the episodes; 'float': float errors added there."""
    t = ctx.t
    pos = ctx.truth.copy()
    is_float = np.zeros(len(t), bool)
    episode = -np.ones(len(t), int)
    use = np.ones(len(t), bool)
    for i, (a, b) in enumerate(eps):
        m = (t >= a) & (t < b)
        episode[m] = i
        if mode == "outage":
            use[m] = False
        else:
            is_float[m] = True
            err = rng.normal(0, FLOAT_OFFSET) + gauss_markov(int(m.sum()), 0.1, tc, FLOAT_WANDER, rng)
            pos[m] += err + rng.normal(0, [0.02, 0.02, 0.03], (int(m.sum()), 3))
    return ins.Gnss(t, pos, is_float, episode, use)


def score(ctx: Context, res: ins.Result, gnss: ins.Gnss, eps) -> dict:
    """Errors at the epochs inside episodes: horizontal and vertical.

    'filter' (real time) and 'smoother' (post-processed) from the IMU; 'gnss'
    the raw float; 'line' no IMU at all, a straight line between the fixed
    positions either side of each episode.
    """
    t = ctx.t
    m = gnss.episode >= 0
    good = gnss.use & ~gnss.float_
    line = np.column_stack([np.interp(t[m], t[good], ctx.truth[good, k]) for k in range(3)])
    out = {"line": line - ctx.truth[m]}
    if gnss.float_.any():
        out["gnss"] = gnss.pos[m] - ctx.truth[m]
    for name, est in (("filter", res.antenna), ("smoother", res.antenna_smooth)):
        if est is not None:
            e = np.column_stack([np.interp(t[m], res.t, est[:, k]) for k in range(3)])
            out[name] = e - ctx.truth[m]
    return {k: (np.hypot(d[:, 0], d[:, 1]), np.abs(d[:, 2])) for k, d in out.items()}


def validate(ctx: Context) -> str:
    """All fixed: how closely the filter follows, and what it learnt."""
    gnss = ins.Gnss(ctx.t, ctx.truth, np.zeros(len(ctx.t), bool), -np.ones(len(ctx.t), int),
                    np.ones(len(ctx.t), bool))
    res = ins.run(ctx.steps, gnss, ctx.cfg, ctx.lat0, ctx.x0, ctx.P0, smooth=False)
    e = np.column_stack([np.interp(ctx.t, res.t, res.antenna[:, k]) for k in range(3)])
    m = (ctx.t > res.t[0] + 5) & (ctx.t < res.t[-1])
    d = e[m] - ctx.truth[m]
    x = res.nominal_last
    return (f"all fixed: filter minus GNSS {np.sqrt(np.mean(d[:, 0] ** 2 + d[:, 1] ** 2)) * 100:.1f} cm "
            f"horizontal, {np.sqrt(np.mean(d[:, 2] ** 2)) * 100:.1f} cm vertical rms; "
            f"gyro bias {np.degrees(x.bg) * 3600} deg/h, accel bias {x.ba} m/s^2, "
            f"mount misalignment {np.degrees(x.mount)} deg")


def experiment(ctx: Context, lengths=(10, 30, 60), modes=(("outage", 0), ("float", 1.0), ("float", 10.0), ("float", 60.0)),
               runs: int = 3, seed: int = 1, log=print, wheel: bool = False) -> dict:
    """The same seed gives the same episodes and float errors for any IMU on the same session."""
    rng = np.random.default_rng(seed)
    wrng = np.random.default_rng(seed + 1000)
    results = {}
    for length in lengths:
        count = max(1, int(250 // (length + 25)))
        for mode, tc in modes:
            acc: dict[str, list] = {}
            for r in range(runs):
                eps = episodes(ctx, length, count, rng)
                gnss = make_gnss(ctx, eps, mode, tc if tc else 10.0, rng)
                res = ins.run(ctx.steps, gnss, ctx.cfg, ctx.lat0, ctx.x0, ctx.P0, smooth=True,
                              wheel=make_wheel(ctx, wrng) if wheel else None)
                for k, (hz, vt) in score(ctx, res, gnss, eps).items():
                    a = acc.setdefault(k, [[], []])
                    a[0].append(hz)
                    a[1].append(vt)
            key = (length, mode, tc)
            results[key] = {k: (np.concatenate(h), np.concatenate(v)) for k, (h, v) in acc.items()}
            log(line(key, results[key]))
    return results


def line(key, r) -> str:
    length, mode, tc = key
    label = f"{length:>3} s {mode}" + (f" (wander {tc:g} s)" if mode == "float" else "")
    parts = []
    for name in ("gnss", "line", "filter", "smoother"):
        if name in r:
            h, v = r[name]
            parts.append(f"{name} {np.sqrt(np.mean(h ** 2)) * 100:5.1f}/{np.sqrt(np.mean(v ** 2)) * 100:5.1f} "
                         f"(p95 {np.percentile(h, 95) * 100:4.0f}/{np.percentile(v, 95) * 100:4.0f})")
    return f"  {label:<26} " + "   ".join(parts)


if __name__ == "__main__":
    ctx = setup(sys.argv[1])
    print(validate(ctx))
    print("errors inside the episodes, cm rms horizontal/vertical (p95):")
    experiment(ctx)
