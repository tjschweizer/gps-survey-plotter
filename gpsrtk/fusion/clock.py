"""The session's three clocks, put on one.

- The Cube stamps every ULog record in us since it booted.
- The phone stamps every logged byte and every IMU sample on its monotonic
  clock (elapsedRealtimeNanos).
- GNSS epochs carry UTC.

The app logs a TIMESYNC round trip with the Cube each second: sent and
received on the phone's clock, the Cube's clock in between. The quickest
round trip in each 10 s window pins the offset to half its round trip; a
robust line through those gives offset and drift.

GGA sentences reach the phone some time after their epoch. Most arrive about
35 ms late and a few within 3 ms (USB and the read loop), so the lower
envelope of (arrival - UTC) maps the phone's clock to GNSS time. That
envelope still holds the receiver's smallest output delay, which nothing
here can see: `checks.clock_checks` measures it against the IMUs.

A numpy port of the app repository's `tools/cubetime.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

WINDOW_NS = 10_000_000_000


@dataclass
class Line:
    """y = a + b (x - x0)"""
    x0: float
    a: float
    b: float

    def __call__(self, x):
        return self.a + self.b * (np.asarray(x, dtype=float) - self.x0)


def fit_line(x: np.ndarray, y: np.ndarray) -> Line:
    x0, y0 = float(np.mean(x)), float(np.mean(y))
    sxx = float(np.sum((x - x0) ** 2))
    b = float(np.sum((x - x0) * (y - y0)) / sxx) if sxx > 0 else 0.0
    return Line(x0, y0, b)


def robust_line(x: np.ndarray, y: np.ndarray, k: float = 4.0, floor: float = 0.0) -> tuple[Line, np.ndarray]:
    keep = np.ones(len(x), bool)
    line = fit_line(x, y)
    for _ in range(3):
        res = np.abs(y - line(x))
        mad = np.median(res[keep]) * 1.4826
        new = res <= max(k * mad, floor)
        if new.sum() < 2 or (new == keep).all():
            break
        keep = new
        line = fit_line(x[keep], y[keep])
    return line, keep


def lower_envelope(x: np.ndarray, y: np.ndarray) -> Line:
    """The line no point falls below that sits closest to them on average.

    That linear programme's answer is the edge of the lower convex hull that
    spans the mean x. Works on int64 nanoseconds without losing precision.
    """
    order = np.lexsort((y, x))
    pts = list(zip(x[order].tolist(), y[order].tolist()))
    hull: list[tuple[int, int]] = []
    for p in pts:
        while len(hull) >= 2:
            (x1, y1), (x2, y2) = hull[-2], hull[-1]
            if (x2 - x1) * (p[1] - y1) - (y2 - y1) * (p[0] - x1) <= 0:
                hull.pop()
            else:
                break
        hull.append(p)
    if len(hull) == 1:
        return Line(hull[0][0], hull[0][1], 0.0)
    xm = float(np.mean(x))
    for (x1, y1), (x2, y2) in zip(hull, hull[1:]):
        if x2 >= xm:
            break
    return Line(x1, y1, (y2 - y1) / (x2 - x1))


class CubeClock:
    """Cube time against phone time, from TIMESYNC (sent_ns, recv_ns, cube_ns)."""

    def __init__(self, sent: np.ndarray, recv: np.ndarray, cube: np.ndarray):
        self.samples = len(sent)
        rtt = recv - sent
        window = recv // WINDOW_NS
        best = [np.flatnonzero(window == w)[np.argmin(rtt[window == w])] for w in np.unique(window)]
        best = np.array(best)
        mid = (sent[best] + recv[best]) / 2
        off = cube[best] - mid
        self.offset, keep = robust_line(mid, off, floor=1e6)
        self.windows = int(keep.sum())
        self.rtt_min_ms = float(rtt.min() / 1e6)
        self.rtt_median_ms = float(np.median(rtt) / 1e6)
        self.residual_us = float(np.sqrt(np.mean((off[keep] - self.offset(mid[keep])) ** 2)) / 1e3)
        self.drift_ppm = self.offset.b * 1e6

    def to_phone_ns(self, cube_us):
        cube_ns = np.asarray(cube_us, dtype=float) * 1e3
        mono = cube_ns - self.offset.a
        for _ in range(3):
            mono = cube_ns - self.offset(mono)
        return mono


class PhoneUtc:
    """Phone time against GNSS time, from GGA arrivals (lower envelope)."""

    def __init__(self, mono_ns: np.ndarray, utc_ns: np.ndarray):
        self.epochs = len(mono_ns)
        delay = mono_ns - utc_ns
        self.delay = lower_envelope(mono_ns, delay)
        res = delay - self.delay(mono_ns)
        self.median_above_ms = float(np.median(res) / 1e6)
        self.share_within_5ms = float(np.mean(res < 5e6))
        self.drift_ppm = self.delay.b * 1e6

    def to_utc_s(self, mono_ns):
        mono_ns = np.asarray(mono_ns, dtype=float)
        return (mono_ns - self.delay(mono_ns)) / 1e9
