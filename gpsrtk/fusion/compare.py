"""Every fusion option on the same simulated float and gaps, plotted side by side.

    python -m gpsrtk.fusion.compare <session folder> <output folder> [--workers 8] [--runs 4]
    python -m gpsrtk.fusion.compare <session folder> <output folder> --plots-only

Runs `floatsim` for each option in `OPTIONS` (the Cube's IMU, the phone's,
the Cube's with a simulated wheel-speed sensor) on exactly the same
scenarios: stretches of 10, 30 and 60 s, each either a gap with no GNSS or
simulated float wandering fast, medium or slow. Then a sweep of the assumed
IMU timing, and one 60 s float stretch kept second by second. Runs go in
parallel, one process per core; the scenarios are drawn once, up front, so
every option and worker sees the same ones.

Writes to the output folder:

- `results.pkl`: the errors (no positions), so `--plots-only` can redraw;
- `1_float.png`, `2_gaps.png`: rms error inside the stretches against
  their length, height and horizontal, post-processed and real time, with
  the no-IMU baselines (raw float; a straight line across the stretch);
- `3_sensitivity.png`: against how fast the float wanders, and against the
  assumed IMU timing;
- `4_example.png`: one float stretch, second by second;
- `summary.csv`: every number in the plots.
"""

from __future__ import annotations

import argparse
import csv
import pickle
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from . import floatsim as F
from . import ins

LENGTHS = (10, 30, 60)
MODES = (("outage", 0), ("float", 1.0), ("float", 10.0), ("float", 60.0))
WANDERS = (1.0, 10.0, 60.0)
LAGS = (-0.10, -0.08, -0.06, -0.04, -0.02, 0.0)
RUNS = 4

# Option -> (floatsim.setup arguments, simulated wheel speed). The rig is the
# 2026-10-02 cart as the step-1 checks measured it (lever arms, mount yaw,
# the phone's timing against the Cube's); another rig needs its own.
OPTIONS = {
    "cube": ({}, False),
    "phone": ({"imu": "phone", "lag": -0.0626, "lever_antenna": (0.383, 0.018, 0.0), "mount_yaw_deg": 0.9}, False),
    "cube+wheel": ({}, True),
}
# The IMU timings the step-1 checks allowed on that run, ms.
TIMING_RANGE_MS = (-70, 25)

_CTX: dict = {}


def _init(ctxs: dict) -> None:
    _CTX.update(ctxs)


def _work(task):
    group, key, ctx_name, (eps, gnss, wheel), use_wheel = task
    ctx = _CTX[ctx_name]
    r = ins.run(ctx.steps, gnss, ctx.cfg, ctx.lat0, ctx.x0, ctx.P0, smooth=True,
                wheel=wheel if use_wheel else None)
    return group, key, F.score(ctx, r, gnss, eps)


def scenarios(ctx: F.Context, seed: int, lengths, modes, runs: int) -> list:
    """(case, (episodes, GNSS as given to the filter, wheel speed)) for each run."""
    rng = np.random.default_rng(seed)
    wrng = np.random.default_rng(seed + 1000)
    out = []
    for length in lengths:
        count = max(1, int(250 // (length + 25)))
        for mode, tc in modes:
            for _ in range(runs):
                eps = F.episodes(ctx, length, count, rng)
                gnss = F.make_gnss(ctx, eps, mode, tc if tc else 10.0, rng)
                out.append(((length, mode, tc), (eps, gnss, F.make_wheel(ctx, wrng))))
    return out


def _ctx_name(kwargs: dict) -> str:
    return repr(sorted(kwargs.items()))


def run(session: str, workers: int = 8, runs: int = RUNS, log=print) -> dict:
    ctxs = {}
    for kwargs, _ in OPTIONS.values():
        ctxs.setdefault(_ctx_name(kwargs), F.setup(session, **kwargs))
    for lag in LAGS:
        ctxs[f"lag{lag:+.3f}"] = F.setup(session, lag=lag)
    base = ctxs[_ctx_name({})]
    main = scenarios(base, 1, LENGTHS, MODES, runs)
    timing = scenarios(base, 7, (30,), (("outage", 0), ("float", 10.0)), runs)
    tasks = [(name, key, _ctx_name(kwargs), sc, wheel)
             for name, (kwargs, wheel) in OPTIONS.items() for key, sc in main]
    tasks += [(f"lag{lag:+.3f}", key, f"lag{lag:+.3f}", sc, False) for lag in LAGS for key, sc in timing]
    log(f"{len(tasks)} runs on {workers} workers")

    acc: dict = {}
    with ProcessPoolExecutor(max_workers=workers, initializer=_init, initargs=(ctxs,)) as pool:
        futures = [pool.submit(_work, t) for t in tasks]
        for i, f in enumerate(as_completed(futures), 1):
            group, key, sc = f.result()
            d = acc.setdefault(group, {}).setdefault(key, {})
            for which, (hz, vt) in sc.items():
                h, v = d.setdefault(which, ([], []))
                h.append(hz)
                v.append(vt)
            if i % 20 == 0 or i == len(futures):
                log(f"{i}/{len(futures)} runs")
    pooled = {g: {k: {w: (np.concatenate(h), np.concatenate(v)) for w, (h, v) in d.items()}
                  for k, d in r.items()} for g, r in acc.items()}

    # One 60 s float stretch, second by second.
    rng = np.random.default_rng(42)
    eps = F.episodes(base, 60, 1, rng)
    gnss = F.make_gnss(base, eps, "float", 10.0, rng)
    m = gnss.episode >= 0
    good = gnss.use & ~gnss.float_
    ex = {"episode": eps[0], "t": base.t, "mask": m, "gnss": gnss.pos[m] - base.truth[m],
          "line": np.column_stack([np.interp(base.t[m], base.t[good], base.truth[good, k])
                                   for k in range(3)]) - base.truth[m]}
    wheel = F.make_wheel(base, np.random.default_rng(5))
    for name, (kwargs, use_wheel) in OPTIONS.items():
        ctx = ctxs[_ctx_name(kwargs)]
        r = ins.run(ctx.steps, gnss, ctx.cfg, ctx.lat0, ctx.x0, ctx.P0, smooth=True,
                    wheel=wheel if use_wheel else None)
        for kind, est in (("filter", r.antenna), ("smoother", r.antenna_smooth)):
            ex[f"{name} {kind}"] = np.column_stack(
                [np.interp(base.t[m], r.t, est[:, k]) for k in range(3)]) - base.truth[m]
    return {"res": {n: pooled[n] for n in OPTIONS},
            "timing": {lag: pooled[f"lag{lag:+.3f}"] for lag in LAGS},
            "example": ex}


# --- plots ------------------------------------------------------------------------------

SURFACE, INK, INK2, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9"
SERIES = {"cube": "#2a78d6", "phone": "#eb6834", "cube+wheel": "#1baf7a"}
LABEL = {"cube": "Cube IMU", "phone": "Phone IMU", "cube+wheel": "Cube IMU + wheel speed"}
BASE = {"gnss": ("#52514e", "Raw float, no IMU"), "line": (MUTED, "No IMU: straight line across")}
STYLE = {
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "font.size": 10, "axes.titlesize": 11, "axes.labelsize": 10,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "text.color": INK, "axes.titlecolor": INK, "axes.grid": True, "grid.color": GRID,
    "grid.linewidth": 0.8, "grid.linestyle": "-", "axes.spines.top": False, "axes.spines.right": False,
    "legend.frameon": False, "lines.linewidth": 2, "lines.solid_capstyle": "round",
}
TICKS = (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000)
DPI = 150


def rms_cm(a) -> float:
    return float(np.sqrt(np.mean(np.square(a)))) * 100


def pooled(r: dict, length: int, mode: str, wanders, which: str, part: int) -> float:
    """rms (cm) of `which` ('filter', 'smoother', 'gnss', 'line') over the given wander speeds.

    `part` 0 is horizontal, 1 height.
    """
    keys = [(length, mode, w) for w in wanders] if mode == "float" else [(length, "outage", 0)]
    arr = [r[k][which][part] for k in keys if k in r and which in r[k]]
    return rms_cm(np.concatenate(arr)) if arr else np.nan


def _log_axis(ax, lo, hi, data=None) -> None:
    """Log scale in cm; with data, the top is the first tick clear of it."""
    from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator

    if data is not None:
        hi = next(t for t in TICKS if t >= max(np.nanmax(data) * 1.15, hi))
    ax.set_yscale("log")
    ax.yaxis.set_major_locator(FixedLocator([t for t in TICKS if lo <= t <= hi]))
    ax.yaxis.set_minor_locator(NullLocator())
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v / 100:g} m" if v >= 100 else f"{v:g} cm"))
    ax.set_ylim(lo, hi)


def _line(ax, x, y, color, label=None, marker="o", filled=True) -> None:
    ax.plot(x, y, color=color, marker=marker, markersize=6, markeredgecolor=SURFACE if filled else color,
            markerfacecolor=color if filled else SURFACE, markeredgewidth=1.5, label=label, zorder=3)


def _finish(fig, plt, path: Path, title: str, note: str = "", bottom: float = 0.05, ncol: int | None = None,
            legend_from=None) -> None:
    h, l = legend_from.get_legend_handles_labels()
    fig.legend(h, l, loc="lower center", ncol=ncol or len(l), bbox_to_anchor=(0.5, -0.005), fontsize=9.5)
    if note:
        fig.text(0.01, 0.935, note, fontsize=9, color=INK2, ha="left")
    fig.suptitle(title, x=0.01, ha="left", fontsize=13, color=INK)
    fig.tight_layout(rect=(0, bottom, 1, 0.93 if note else 0.97))
    fig.savefig(path, dpi=DPI)
    plt.close(fig)


def _comparison(plt, res, mode, title, path, baselines, note) -> None:
    fig, axes = plt.subplots(2, 2, figsize=(10, 7.4), sharex=True)
    parts = ((1, "Height"), (0, "Horizontal"))
    rows = (("smoother", "post-processed (smoothed after the outing)"), ("filter", "real time"))
    for i, (which, rowname) in enumerate(rows):
        for j, (part, partname) in enumerate(parts):
            ax = axes[i, j]
            # The straight line needs the stretch's far end: post-processing only.
            series = [("base", b) for b in baselines if which == "smoother" or b == "gnss"]
            series += [("imu", n) for n in SERIES if n in res]
            vals = []
            for idx, (kind, nm) in enumerate(series):
                dx = (idx - (len(series) - 1) / 2) * 1.2   # nudged apart so overlapping series stay visible
                x = [L + dx for L in LENGTHS]
                if kind == "base":
                    y = [pooled(res["cube"], L, mode, WANDERS, nm, part) for L in LENGTHS]
                    _line(ax, x, y, BASE[nm][0], BASE[nm][1], marker="s", filled=False)
                else:
                    y = [pooled(res[nm], L, mode, WANDERS, which, part) for L in LENGTHS]
                    _line(ax, x, y, SERIES[nm], LABEL[nm])
                vals += y
            _log_axis(ax, 1, 100, vals)
            ax.set_xticks(LENGTHS)
            ax.set_xticklabels([f"{L} s" for L in LENGTHS])
            ax.set_xlim(5, 70)
            ax.set_title(f"{partname} - {rowname}", loc="left")
            if i == 1:
                ax.set_xlabel(f"Length of each {'float stretch' if mode == 'float' else 'gap'}")
            if j == 0:
                ax.set_ylabel("rms error inside the stretch")
    _finish(fig, plt, path, title, note, legend_from=axes[0, 0])


def _sensitivity(plt, res, timing, path) -> None:
    fig, axes = plt.subplots(2, 2, figsize=(10, 7.2))
    for j, (part, partname) in enumerate(((1, "Height"), (0, "Horizontal"))):
        ax = axes[0, j]
        xs = np.arange(len(WANDERS))
        y = [pooled(res["cube"], 30, "float", (w,), "gnss", part) for w in WANDERS]
        _line(ax, xs - 0.09, y, BASE["gnss"][0], BASE["gnss"][1], marker="s", filled=False)
        for idx, name in enumerate(n for n in SERIES if n in res):
            y = [pooled(res[name], 30, "float", (w,), "smoother", part) for w in WANDERS]
            _line(ax, xs + (idx - 1) * 0.06 + 0.03, y, SERIES[name], LABEL[name])
        ax.set_xticks(xs)
        ax.set_xticklabels([f"{w:g} s" for w in WANDERS])
        ax.set_xlabel("How fast the float error wanders (correlation time)")
        _log_axis(ax, 1, 100)
        ax.set_title(f"{partname}: 30 s float stretches, post-processed", loc="left")
        if j == 0:
            ax.set_ylabel("rms error inside the stretch")

        ax = axes[1, j]
        lags = sorted(timing)
        for key, marker, filled, lab in (((30, "float", 10.0), "o", True, "30 s float"),
                                         ((30, "outage", 0), "s", False, "30 s gap")):
            y = [rms_cm(timing[lg][key]["smoother"][part]) for lg in lags]
            _line(ax, [lg * 1000 for lg in lags], y, SERIES["cube"], lab, marker=marker, filled=filled)
        ax.legend(loc="upper left", fontsize=9)
        ax.axvspan(*TIMING_RANGE_MS, color=GRID, alpha=0.5, zorder=0, lw=0)
        ax.text(sum(TIMING_RANGE_MS) / 2, 1.25, "range the checks allow", ha="center", fontsize=8.5, color=INK2)
        _log_axis(ax, 1, 100)
        ax.set_xlim(min(lags) * 1000 - 10, max(TIMING_RANGE_MS[1], max(lags) * 1000) + 5)
        ax.set_xlabel("Assumed IMU timing against GNSS (ms; negative = IMU early)")
        ax.set_title(f"{partname}: Cube IMU, post-processed", loc="left")
        if j == 0:
            ax.set_ylabel("rms error inside the stretch")
    _finish(fig, plt, path, "What the estimate depends on",
            "Top: how fast the simulated float error wanders. Bottom: the assumed IMU timing, "
            "across and beyond the range the step-1 checks allow.", legend_from=axes[0, 0])


def _example(plt, ex, path) -> None:
    t = ex["t"][ex["mask"]] - ex["episode"][0]
    fig, axes = plt.subplots(2, 1, figsize=(10, 7.2), sharex=True)
    for ax, part, title in ((axes[0], "h", "Height error (+ up)"), (axes[1], "xy", "Horizontal error")):
        def val(a):
            return -a[:, 2] * 100 if part == "h" else np.hypot(a[:, 0], a[:, 1]) * 100

        ax.plot(t, val(ex["gnss"]), color=BASE["gnss"][0], lw=1.2, label=BASE["gnss"][1], zorder=2)
        ax.plot(t, val(ex["cube filter"]), color=SERIES["cube"], lw=1.2, alpha=0.5,
                label="Cube IMU, real time", zorder=2)
        # Wider underneath, so where the Cube with and without wheel speed agree both still show.
        if "cube+wheel smoother" in ex:
            ax.plot(t, val(ex["cube+wheel smoother"]), color=SERIES["cube+wheel"], lw=3.5,
                    label=LABEL["cube+wheel"] + ", post-processed", zorder=3)
        for nm in ("phone", "cube"):
            if f"{nm} smoother" in ex:
                ax.plot(t, val(ex[f"{nm} smoother"]), color=SERIES[nm],
                        label=LABEL[nm] + ", post-processed", zorder=4)
        if part == "h":
            ax.axhline(0, color=INK2, lw=0.8, zorder=1)
        ax.set_ylabel("cm")
        ax.set_title(title, loc="left")
    axes[1].set_xlabel("Seconds into one 60 s float stretch (float wander 10 s)")
    _finish(fig, plt, path, "One float stretch, second by second", bottom=0.08, ncol=3, legend_from=axes[0])


def _summary(res, path: Path) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["case", "length_s", "wander_s", "option", "output",
                    "horizontal_rms_cm", "height_rms_cm", "height_p95_cm"])
        for name, r in res.items():
            for (L, mode, tc), v in sorted(r.items(), key=lambda kv: (kv[0][1], kv[0][0], kv[0][2])):
                for which, (hz, vt) in v.items():
                    if which in ("gnss", "line") and name != "cube":
                        continue    # the baselines don't depend on the IMU: once is enough
                    w.writerow([mode, L, tc if mode == "float" else "",
                                name if which in ("filter", "smoother") else "none", which,
                                f"{rms_cm(hz):.1f}", f"{rms_cm(vt):.1f}", f"{np.percentile(vt, 95) * 100:.1f}"])


def plots(results: dict, out: Path) -> list[Path]:
    """Draw everything from `run`'s results into `out`; returns the files written."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    res, timing, ex = results["res"], results["timing"], results["example"]
    paths = [out / n for n in ("1_float.png", "2_gaps.png", "3_sensitivity.png", "4_example.png", "summary.csv")]
    with plt.rc_context(STYLE):
        _comparison(plt, res, "float", "RTK float: error inside 10-60 s float stretches (simulated float)",
                    paths[0], ("gnss", "line"),
                    "Pooled over float that wanders fast, medium and slow. The straight line needs the "
                    "stretch's far end, so it is post-processing only.")
        _comparison(plt, res, "outage", "No GNSS at all: error inside 10-60 s gaps", paths[1], ("line",),
                    "Without an IMU there is no real-time position in a gap at all.")
        _sensitivity(plt, res, timing, paths[2])
        _example(plt, ex, paths[3])
    _summary(res, paths[4])
    return paths


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("session", help="a session folder as the app records it")
    ap.add_argument("out", help="folder for results.pkl, the plots and summary.csv")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--runs", type=int, default=RUNS, help="repeats of each case")
    ap.add_argument("--plots-only", action="store_true", help="redraw from an earlier results.pkl")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if a.plots_only:
        results = pickle.loads((out / "results.pkl").read_bytes())
    else:
        results = run(a.session, a.workers, a.runs, log=lambda s: print(s, flush=True))
        (out / "results.pkl").write_bytes(pickle.dumps(results))
    for p in plots(results, out):
        print(p)


if __name__ == "__main__":
    main()
