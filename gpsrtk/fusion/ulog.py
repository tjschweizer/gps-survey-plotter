"""PX4 ULog files, read into numpy record arrays.

The format is documented at
https://docs.px4.io/main/en/dev_log/ulog_file_format.html. The app rebuilds
one from the Cube's streamed log for each USB connection (`raw/cube.ulg`,
`raw/cube-2.ulg` after a replug). This is a numpy port of the app
repository's standard-library reader (`tools/ulog.py`), which stays there for
use without this package.

    log = ULog.read(path)
    gyro = log.topic("sensor_gyro_fifo")      # structured array; fields by name
    t, xyz = fifo(log, "sensor_gyro_fifo")   # one row per sample, SI units
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

MAGIC = b"ULog\x01\x12\x35"
SYNC = bytes([0x2F, 0x73, 0x13, 0x20, 0x25, 0x0C, 0xBB, 0x12])

DTYPES = {
    "int8_t": "i1", "uint8_t": "u1", "int16_t": "<i2", "uint16_t": "<u2",
    "int32_t": "<i4", "uint32_t": "<u4", "int64_t": "<i8", "uint64_t": "<u8",
    "float": "<f4", "double": "<f8", "bool": "?", "char": "S1",
}
KNOWN = set(b"BFIMPQARDLCSO")


def _split_type(t: str) -> tuple[str, int | None]:
    if t.endswith("]"):
        base, n = t[:-1].split("[")
        return base, int(n)
    return t, None


class ULog:
    def __init__(self) -> None:
        self.version = 0
        self.start_us = 0
        self.formats: dict[str, list[tuple[str, int | None, str]]] = {}
        self.info: dict[str, object] = {}
        self.params: dict[str, object] = {}
        self.subscriptions: dict[int, tuple[str, int]] = {}
        self.dropouts: list[tuple[int, int]] = []   # (last timestamp us, ms)
        self.messages: list[tuple[int, int, str]] = []
        self.truncated = False
        self.resyncs = 0
        self._chunks: dict[int, list[bytes]] = {}
        self._dtypes: dict[str, np.dtype] = {}
        self._last_ts = 0
        self._keep: set | None = None

    @classmethod
    def read(cls, path: str | Path, topics=None) -> "ULog":
        """`topics`: keep data only for these names (all if None), to save memory."""
        return cls.parse(Path(path).read_bytes(), topics)

    @classmethod
    def parse(cls, buf: bytes, topics=None) -> "ULog":
        log = cls()
        log._keep = set(topics) if topics is not None else None
        if len(buf) < 16 or buf[:7] != MAGIC:
            raise ValueError("not a ULog file")
        log.version = buf[7]
        log.start_us = struct.unpack_from("<Q", buf, 8)[0]
        pos, n = 16, len(buf)
        unpack = struct.Struct("<HB").unpack_from
        while pos + 3 <= n:
            size, kind = unpack(buf, pos)
            end = pos + 3 + size
            if kind not in KNOWN:
                found = buf.find(SYNC, pos + 1)
                if found < 0:
                    break
                pos = found - 3
                log.resyncs += 1
                continue
            if end > n:
                break
            if kind == 0x44:  # 'D', by far the most common: keep it cheap
                msg_id = buf[pos + 3] | buf[pos + 4] << 8
                chunks = log._chunks.get(msg_id)
                if chunks is not None:
                    chunks.append(buf[pos + 5:end])
                    if size >= 10:
                        log._last_ts = struct.unpack_from("<Q", buf, pos + 5)[0]
            else:
                log._message(kind, buf[pos + 3:end])
            pos = end
        log.truncated = pos < n
        return log

    def _message(self, kind: int, body: bytes) -> None:
        if kind == 0x41:  # 'A' subscription
            multi_id, msg_id = struct.unpack_from("<BH", body)
            name = body[3:].decode("ascii", "replace")
            self.subscriptions[msg_id] = (name, multi_id)
            if self._keep is None or name in self._keep:
                self._chunks.setdefault(msg_id, [])
        elif kind == 0x46:  # 'F' format
            name, _, rest = body.decode("ascii", "replace").partition(":")
            fields = []
            for f in filter(None, rest.split(";")):
                t, _, fname = f.strip().partition(" ")
                base, length = _split_type(t)
                fields.append((base, length, fname))
            self.formats[name] = fields
            self._dtypes.clear()
        elif kind in (0x49, 0x50):  # 'I' info, 'P' parameter
            key, value = self._key_value(body)
            (self.info if kind == 0x49 else self.params)[key] = value
        elif kind == 0x4C:  # 'L' logged string
            level, ts = struct.unpack_from("<BQ", body)
            self.messages.append((ts, level, body[9:].decode("utf-8", "replace")))
        elif kind == 0x43:  # 'C' tagged logged string
            level, _, ts = struct.unpack_from("<BHQ", body)
            self.messages.append((ts, level, body[11:].decode("utf-8", "replace")))
        elif kind == 0x4F:  # 'O' dropout
            self.dropouts.append((self._last_ts, struct.unpack_from("<H", body)[0]))

    @staticmethod
    def _key_value(body: bytes) -> tuple[str, object]:
        klen = body[0]
        key = body[1:1 + klen].decode("ascii", "replace")
        raw = body[1 + klen:]
        t, _, name = key.partition(" ")
        base, length = _split_type(t)
        if base == "char":
            return name, raw.split(b"\0")[0].decode("utf-8", "replace")
        dt = np.dtype(DTYPES.get(base, "u1"))
        vals = np.frombuffer(raw[: dt.itemsize * (length or 1)], dtype=dt)
        if len(vals) == 0:
            return name, raw
        return name, vals.tolist() if length else vals[0].item()

    def dtype(self, name: str) -> np.dtype:
        """The record layout of a format, nested types and padding included."""
        dt = self._dtypes.get(name)
        if dt is None:
            names, formats, offsets, off = [], [], [], 0
            for base, length, fname in self.formats[name]:
                if base == "char" and length:
                    sub = np.dtype("S%d" % length)
                    length = None
                elif base in DTYPES:
                    sub = np.dtype(DTYPES[base])
                else:
                    sub = self.dtype(base)
                full = np.dtype((sub, (length,))) if length else sub
                if not fname.startswith("_padding"):
                    names.append(fname)
                    formats.append(full)
                    offsets.append(off)
                off += full.itemsize
            dt = self._dtypes[name] = np.dtype(
                {"names": names, "formats": formats, "offsets": offsets, "itemsize": off})
        return dt

    def topics(self) -> list[tuple[str, int, int]]:
        """(name, multi_id, records) for each subscription that logged data."""
        return sorted((name, mid, len(self._chunks[i]))
                      for i, (name, mid) in self.subscriptions.items() if self._chunks.get(i))

    def drop(self, name: str) -> None:
        """Forget a topic's data once it has been used, to free its memory."""
        for msg_id, (n, _) in self.subscriptions.items():
            if n == name:
                self._chunks.pop(msg_id, None)

    def topic(self, name: str, multi_id: int = 0) -> np.ndarray | None:
        """A topic's records. PX4 leaves trailing padding out; it is put back."""
        for msg_id, (n, m) in self.subscriptions.items():
            if n == name and m == multi_id and self._chunks.get(msg_id):
                dt = self.dtype(name)
                size = dt.itemsize
                buf = b"".join(c[:size].ljust(size, b"\0") for c in self._chunks[msg_id])
                return np.frombuffer(buf, dtype=dt)
        return None


def fifo(log: ULog, name: str, multi_id: int = 0, window: int = 400) -> tuple[np.ndarray, np.ndarray]:
    """sensor_gyro_fifo / sensor_accel_fifo as one row per sample.

    Returns times (us, Cube clock) and an (n, 3) array in rad/s or m/s^2, on
    the driver's axes (PX4's FRD board frame; SENS_BOARD_ROT not applied).

    A record's `timestamp_sample` is when PX4 read the FIFO, after its
    newest sample: late by a scheduling delay of up to a few hundred us.
    And `dt` is nominal: the sensor runs on its own oscillator (on the
    2026-10-02 Cube, 0.7% slow, wandering 2 ms over 5 minutes). So the
    sample clock is rebuilt: samples are counted (records lost from the
    stream show as a gap and are counted too), and the earliest reads
    around each record (a running minimum of read time against sample
    count over `window` records, smoothed) give its newest sample's time.
    """
    r = log.topic(name, multi_id)
    if r is None or len(r) < 2:
        return np.empty(0), np.empty((0, 3))
    newest, period = _fifo_clock(r, window)
    return _expand(r, newest, period)


def _fifo_clock(r: np.ndarray, window: int = 400) -> tuple[np.ndarray, float]:
    """Each record's newest-sample time (us, Cube clock), and the mean sample period (us)."""
    from scipy.ndimage import minimum_filter1d, uniform_filter1d

    n = r["samples"].astype(np.int64)
    ts = r["timestamp_sample"].astype(np.int64)
    gaps = np.diff(ts)
    # Samples between reads: the record's own, plus those of any records lost
    # from the stream (whole records go: the read jitter is far below a
    # record's span, so they count exactly). The mean period follows from
    # the count, the count from it.
    spans = n[1:].copy()
    for _ in range(3):
        period = float((ts[-1] - ts[0]) / spans.sum())
        records = np.maximum(1, np.round(gaps / (n[1:] * period))).astype(np.int64)
        spans = n[1:] * records
    k = np.concatenate([[0], np.cumsum(spans)]).astype(float)
    late = ts - period * k
    w = min(2 * window + 1, len(ts))
    floor = uniform_filter1d(minimum_filter1d(late, w, mode="nearest"), w, mode="nearest")
    return period * k + floor, period


def _expand(r: np.ndarray, newest: np.ndarray, period: float) -> tuple[np.ndarray, np.ndarray]:
    """Records to one row per sample: times (us) and values (n, 3) in SI units."""
    n = r["samples"].astype(np.int64)
    width = r["x"].shape[1]
    j = np.arange(width)[None, :]
    keep = j < n[:, None]
    t = newest[:, None] - (n[:, None] - 1 - j) * period
    xyz = np.stack([r[a].astype(float) * r["scale"][:, None] for a in "xyz"], axis=-1)
    return t[keep], xyz[keep]


def fifo_integrals(log: ULog, name: str, to_s, edges: np.ndarray, chunk: int = 20_000,
                   multi_id: int = 0, max_fill: int = 10) -> tuple[np.ndarray, int]:
    """A FIFO topic integrated over consecutive intervals, a chunk of records at a time.

    `to_s` maps Cube-clock us to seconds on the caller's timeline; `edges`
    (increasing, in those seconds) bound the intervals. Returns the (len(edges)
    - 1, 3) integrals (rad or m/s), NaN where the data has a hole longer
    than `max_fill` intervals, and the number of samples at full scale
    (clipped). Each sample stands for one sample period before its time.

    A gap must read as missing, never as zero: no specific force is free
    fall to an INS, and before a USB dropout the stream thins out for a
    while (2026-10-03: a filter fed zeros there fell a metre).

    Memory stays at one chunk of samples, so a long log needn't be expanded
    whole: 25 minutes of the Cube's 8 kHz gyro is 12 million samples.
    """
    r = log.topic(name, multi_id)
    cum = np.full((len(edges), 4), np.nan)        # x, y, z, and time covered
    if r is None or len(r) < 2:
        return np.diff(cum, axis=0), 0
    newest, period = _fifo_clock(r)
    full = float(r["scale"][0]) * 32767 * 0.999
    running = np.zeros(4)
    prev_end = None
    clipped = 0
    for start in range(0, len(r), chunk):
        sl = slice(start, start + chunk)
        t_us, x = _expand(r[sl], newest[sl], period)
        clipped += int((np.abs(x) >= full).any(axis=1).sum())
        ts = to_s(t_us)
        step = to_s(np.array([t_us[0] - period, t_us[0]]))
        first = ts[0] - (step[1] - step[0])
        dt = np.diff(np.concatenate([[first if prev_end is None else prev_end], ts]))
        dt = np.minimum(dt, 2 * (step[1] - step[0]))    # lost records: no data, not held values
        c = running + np.cumsum(np.column_stack([x * dt[:, None], dt]), axis=0)
        lo = first if prev_end is None else prev_end
        tt = np.concatenate([[lo], ts])
        cc = np.vstack([running, c])
        sel = (edges >= lo) & (edges <= ts[-1])
        for k in range(4):
            cum[sel, k] = np.interp(edges[sel], tt, cc[:, k])
        running = c[-1]
        prev_end = ts[-1]
    out = np.diff(cum, axis=0)
    width = np.diff(edges)
    # A record lost here and there (a few ms): carry the rate across. A rate
    # is the integral over the time actually covered; where nothing was
    # covered, interpolated from either side if the hole is at most
    # `max_fill` intervals. Longer holes stay missing.
    covered = np.nan_to_num(out[:, 3])
    rate = np.where(covered[:, None] > 0.2 * width[:, None], out[:, :3] / np.maximum(covered, 1e-12)[:, None], np.nan)
    idx = np.arange(len(rate))
    good = np.isfinite(rate).all(axis=1)
    if good.any() and not good.all():
        holes = np.flatnonzero(np.diff(np.concatenate([[0], (~good).astype(int), [0]])))
        for a, b in zip(holes[::2], holes[1::2]):
            if b - a <= max_fill and a > 0 and b < len(rate):
                for k in range(3):
                    rate[a:b, k] = np.interp(idx[a:b], [a - 1, b], [rate[a - 1, k], rate[b, k]])
    return rate * width[:, None], clipped
