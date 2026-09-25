"""Reader interface and shared normalisation helpers.

SW Maps is the current field app but explicitly not a fixed assumption, so
readers are registered rather than hardcoded. Anything that can produce the
canonical PointSet schema can be plugged in - a PPK/RTKLIB solution, a
different collector, a plain CSV.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from ..model.pointset import PointSet


@dataclass
class SurveyExport:
    """One import from the field, holding whatever layers the source provided."""

    name: str
    source_path: Path
    layers: dict[str, PointSet] = field(default_factory=dict)
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)  # non-point
    # What the reader left out or read differently, for the user.
    notes: list[str] = field(default_factory=list)

    def __getitem__(self, key: str) -> PointSet:
        return self.layers[key]

    def get(self, key: str) -> PointSet | None:
        return self.layers.get(key)

    def summary(self) -> str:
        lines = [f"{self.name}  ({self.source_path.name})"]
        for k, ps in self.layers.items():
            lines.append(f"  {k:16s} {ps.describe()}")
        for k, t in self.tables.items():
            lines.append(f"  {k:16s} {len(t)} rows (non-point)")
        return "\n".join(lines)


class SurveyReader(ABC):
    """Reads some field-app export format into the canonical schema."""

    name: str = "reader"

    @abstractmethod
    def can_read(self, path: Path) -> bool: ...

    @abstractmethod
    def read(self, path: Path) -> SurveyExport: ...


_READERS: list[SurveyReader] = []


def register_reader(reader: SurveyReader) -> SurveyReader:
    _READERS.append(reader)
    return reader


def read_any(path: str | Path) -> SurveyExport:
    """Dispatch to whichever registered reader recognises this path."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    for r in _READERS:
        if r.can_read(path):
            return r.read(path)
    raise ValueError(
        f"no registered reader recognises {path.name}. "
        f"Tried: {', '.join(r.name for r in _READERS) or 'none'}")


# --- normalisation helpers ------------------------------------------------

# Trailing timezone abbreviation on SW Maps timestamps:
#   "08/22/2026 13:10:37.700 CDT"
_TZ_SUFFIX = re.compile(r"\s+([A-Z]{2,5})\s*$")


def split_timezone(s: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Separate the timestamp text from its trailing timezone abbreviation.

    Returned times are naive local. The abbreviation is kept rather than
    discarded because it is the only record of which offset was in force, and
    sessions spanning a DST change would otherwise be silently misordered.
    """
    s = s.astype("string").str.strip()
    tz = s.str.extract(_TZ_SUFFIX, expand=False)
    return s.str.replace(_TZ_SUFFIX, "", regex=True), tz


def parse_time(s: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Parse SW Maps timestamps, falling back to inference on odd formats."""
    text, tz = split_timezone(s)
    t = pd.to_datetime(text, format="%m/%d/%Y %H:%M:%S.%f", errors="coerce")
    if t.isna().all() and len(text):
        t = pd.to_datetime(text, errors="coerce")
    return t, tz


def normalise_kind(s: pd.Series) -> pd.Series:
    """The `type` attribute, stripped of whitespace and lower-cased.

    It is typed by hand on a phone, and everything downstream matches it
    exactly: `KindSelect`, and the level network's choice of terrain shots.
    "Lawn" or "lawn " used to drop out of both without a word.
    """
    return s.map(lambda v: v.strip().lower() if isinstance(v, str) else v)


def rod_readings(s: pd.Series) -> pd.Series:
    """Rod readings in inches, from numbers or from text as written.

    A text attribute such as "63 1/4" or "5-3-1/4" used to go through
    `numeric`, which turned it into NaN and silently dropped the reading.
    Anything `units.parse_rod` refuses is still NaN.
    """
    from ..units import parse_rod

    def one(value):
        try:
            return parse_rod(value)
        except (TypeError, ValueError):
            return float("nan")

    return s.map(one).astype(float)


def numeric(df: pd.DataFrame, cols) -> None:
    """Coerce columns to numeric in place, leaving unparseable values as NaN."""
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
