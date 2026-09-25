"""Where one export holds more than one session, and how to tell.

A session is the stretch of data over which the vertical offset is constant:
the antenna was mounted once, the base was selected once. The calendar date
used to stand in for it, and it is a poor proxy both ways round. The mount
changes within a day - mower, then a walked pole - and logging runs past
midnight, which cut one outing into two sessions that could then be called
unrecoverable for want of an overlap between halves of the same mount.

So a split has to be proved by the data. The export is cut into blocks at
every pause of three minutes or more and every change of track name. The
blocks are then walked in time order, each compared with the session built
so far, over the 0.5 m cells both of them cover:

  * at least `MIN_SHARED_CELLS` shared cells,
  * a median cell difference of at least `MIN_STEP_M`,
  * and that difference at least `MIN_T` times its standard error

start a new session. Anything else - a bag-emptying pause, a block on ground
nothing else covered - joins the current session, because no step can be
proved. When a step is proved, the new session starts at the longest pause
since the last block known to be on the old mount, since re-mounting takes a
stop: a short block logged between that stop and the proof belongs to the
new mount, and leaving it behind would put it a whole step out.

The one exception is a gap of `OUTING_GAP_S` or more: that is a
separate outing, re-mounted, and is always its own session. If it shares no
ground with the others, the merge report says so, and that is the truth
rather than an artefact.

A session keeps the date it started on. The first session of a day is named
`stem/YYYY-MM-DD`, as before, so existing projects mostly load unchanged;
later sessions on the same day are `stem/YYYY-MM-DD HH:MM`, from their start.

numpy and pandas only: this runs inside the readers.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .model.pointset import E, FIX, FIX_RTK, N, TIME, TRACK, Z

GAP_S = 180.0                    # a pause this long is a candidate boundary
OUTING_GAP_S = 6 * 3600.0        # a gap this long is always a new session
CELL_M = 0.5                     # the surface's bin size
MIN_SHARED_CELLS = 25            # the same as merge.THIN_OVERLAP
MIN_STEP_M = 0.03
MIN_T = 3.0


def _blocks(t: np.ndarray, track: np.ndarray | None):
    """Start and end row of each block (rows in time order), and whether a
    hard break - a separate outing - precedes it."""
    gap = np.diff(t)
    cut = gap >= GAP_S
    if track is not None:
        cut |= track[1:] != track[:-1]
    starts = np.concatenate([[0], np.flatnonzero(cut) + 1])
    ends = np.concatenate([starts[1:], [len(t)]])
    hard = np.concatenate([[False], gap >= OUTING_GAP_S])[starts]
    return starts, ends, hard


def _cells(e: np.ndarray, n: np.ndarray) -> np.ndarray:
    """One integer per 0.5 m cell, anchored at 0,0 like the site origin grid."""
    ix = np.floor(e / CELL_M).astype(np.int64)
    iy = np.floor(n / CELL_M).astype(np.int64)
    return ix * 100_000_000 + iy


def _medians(cells: np.ndarray, z: np.ndarray) -> pd.Series:
    return pd.Series(z).groupby(cells).median()


UNKNOWN, SAME, STEP = "unknown", "same", "step"


def compare(session: pd.Series, block: pd.Series) -> str:
    """Whether a block sits on the session's datum, from per-cell medians.

    UNKNOWN when they share too little ground to say. STEP when the median
    cell difference is large, and large against its own standard error
    (1.2533 sigma / sqrt(n) for a median). SAME otherwise.
    """
    shared = session.index.intersection(block.index)
    if len(shared) < MIN_SHARED_CELLS:
        return UNKNOWN
    diff = (block.loc[shared] - session.loc[shared]).to_numpy()
    step = float(np.median(diff))
    se = 1.2533 * float(np.std(diff, ddof=1)) / np.sqrt(len(diff))
    return STEP if abs(step) >= MIN_STEP_M and abs(step) >= MIN_T * se else SAME


def split_sessions(df: pd.DataFrame, prefix: str) -> pd.Series:
    """Session label per row. See the module docstring for the rule.

    Rows with no time keep the bare prefix, as they always have.
    """
    labels = np.full(len(df), prefix, dtype=object)
    if TIME not in df.columns or not df[TIME].notna().any():
        return pd.Series(labels, index=df.index, dtype=object)

    times = df[TIME].to_numpy(dtype="datetime64[ns]")
    pos = np.flatnonzero(~np.isnat(times))
    pos = pos[np.argsort(times[pos], kind="stable")]
    t = times[pos].astype(np.int64) / 1e9
    track = (df[TRACK].astype(str).to_numpy()[pos]
             if TRACK in df.columns else None)
    starts, ends, hard = _blocks(t, track)

    # The evidence: RTK-fixed heights by cell. Float would invent steps.
    evidence = {E, N, Z}.issubset(df.columns)
    if evidence:
        e = df[E].to_numpy(dtype=float)[pos]
        n = df[N].to_numpy(dtype=float)[pos]
        z = df[Z].to_numpy(dtype=float)[pos]
        good = np.isfinite(e) & np.isfinite(n) & np.isfinite(z)
        if FIX in df.columns:
            good &= df[FIX].to_numpy()[pos] == FIX_RTK
        cells = _cells(np.where(good, e, 0.0), np.where(good, n, 0.0))

    # A block too small to test stays with the session - but when a later
    # block proves a step, the change is placed at the longest pause since
    # the last block known to belong: re-mounting an antenna takes a stop,
    # and the few points logged between it and the proof are on the new
    # mount. Leaving them behind would put them a whole step out.
    pause = np.concatenate([[0.0], t[starts[1:]] - t[ends[:-1] - 1]])
    first = [0]                           # first row of each session
    known = 0                             # last block whose session is proved
    for k in range(1, len(starts)):
        lo, hi = starts[k], ends[k]
        if hard[k]:
            first.append(lo)
            known = k
            continue
        if not evidence:
            continue
        b = good[lo:hi]
        if len(np.unique(cells[lo:hi][b])) < MIN_SHARED_CELLS:
            continue
        s0 = first[-1]
        s = good[s0:lo]
        verdict = compare(_medians(cells[s0:lo][s], z[s0:lo][s]),
                          _medians(cells[lo:hi][b], z[lo:hi][b]))
        if verdict == SAME:
            known = k
        elif verdict == STEP:
            since = range(known + 1, k + 1)
            first.append(starts[max(since, key=lambda m: (pause[m], m))])
            known = k
    session = np.searchsorted(first, np.arange(len(pos)), side="right") - 1

    names, dates, used = [], set(), set()
    for row in first:
        t0 = pd.Timestamp(times[pos[row]])
        day = t0.strftime("%Y-%m-%d")
        if day not in dates:
            name = f"{prefix}/{day}"
        else:
            name = f"{prefix}/{day} {t0:%H:%M}"
            if name in used:
                name = f"{prefix}/{day} {t0:%H:%M:%S}"
        dates.add(day)
        used.add(name)
        names.append(name)

    labels[pos] = np.asarray(names, dtype=object)[session]
    return pd.Series(labels, index=df.index, dtype=object)
