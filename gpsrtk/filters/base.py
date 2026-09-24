"""Filter stages and the chain that runs them.

A stage is a pure `(PointSet) -> PointSet`. The chain is an ordered, named,
serialisable list of them. Two things fall out of that shape:

  * The project file records exactly how a surface was produced, so a result
    from six months ago can be reproduced rather than guessed at.
  * The GUI is a stack you reorder and toggle. Editing stage 4 only recomputes
    from stage 4 onward, because earlier results are cached.

Every stage reports points in and out. A filter chain that silently discards
90% of the data is the easiest way to produce a confident, wrong surface.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..model.pointset import PointSet


class Stage(ABC):
    """One step in a filter chain."""

    kind: str = "stage"
    label: str = "stage"

    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    @abstractmethod
    def apply(self, ps: PointSet) -> PointSet: ...

    def params(self) -> dict:
        """Parameters that affect the result. Used for caching and for saving."""
        return {}

    def describe(self) -> str:
        p = ", ".join(f"{k}={v}" for k, v in self.params().items())
        return f"{self.label}({p})" if p else self.label

    # --- serialisation ---------------------------------------------------

    def to_dict(self) -> dict:
        return {"kind": self.kind, "enabled": self.enabled, **self.params()}

    @classmethod
    def from_dict(cls, d: dict) -> "Stage":
        d = dict(d)
        kind = d.pop("kind")
        return REGISTRY[kind](**d)

    def signature(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, default=str)


REGISTRY: dict[str, type[Stage]] = {}


def register(cls: type[Stage]) -> type[Stage]:
    REGISTRY[cls.kind] = cls
    return cls


@dataclass
class StageResult:
    label: str
    enabled: bool
    n_in: int
    n_out: int

    @property
    def kept(self) -> float:
        return self.n_out / self.n_in if self.n_in else 0.0

    def __str__(self) -> str:
        if not self.enabled:
            return f"  [off] {self.label}"
        return (f"  {self.label:38s} {self.n_in:>7,} -> {self.n_out:>7,}"
                f"  ({self.kept * 100:5.1f}%)")


@dataclass
class FilterChain:
    """An ordered list of stages with incremental recomputation."""

    stages: list[Stage] = field(default_factory=list)
    _cache: list[PointSet] = field(default_factory=list, repr=False)
    _signatures: list[str] = field(default_factory=list, repr=False)
    _source_id: int | None = field(default=None, repr=False)
    results: list[StageResult] = field(default_factory=list, repr=False)

    def add(self, stage: Stage) -> "FilterChain":
        self.stages.append(stage)
        return self

    def run(self, ps: PointSet) -> PointSet:
        """Apply enabled stages in order, reusing cached prefixes."""
        sigs = [s.signature() for s in self.stages]

        # Find the first stage whose parameters changed. Everything before it
        # is still valid; everything from it onward must be recomputed.
        start = 0
        if self._source_id == id(ps):
            while (start < len(sigs) and start < len(self._signatures)
                   and start < len(self._cache)
                   and sigs[start] == self._signatures[start]):
                start += 1
        else:
            self._cache = []

        self._cache = self._cache[:start]
        current = self._cache[-1] if self._cache else ps

        for stage in self.stages[start:]:
            n_in = len(current)
            if stage.enabled:
                current = stage.apply(current)
            self._cache.append(current)

        self._signatures = sigs
        self._source_id = id(ps)

        # Rebuild the full report, including the cached prefix.
        self.results = []
        prev = ps
        for stage, out in zip(self.stages, self._cache):
            self.results.append(
                StageResult(stage.describe(), stage.enabled, len(prev), len(out)))
            prev = out
        return current

    def report(self) -> str:
        if not self.results:
            return "chain has not been run"
        head = self.results[0].n_in
        tail = self.results[-1].n_out
        lines = [str(r) for r in self.results]
        lines.append(f"  {'TOTAL':38s} {head:>7,} -> {tail:>7,}"
                     f"  ({tail / head * 100 if head else 0:5.1f}%)")
        return "\n".join(lines)

    # --- serialisation ---------------------------------------------------

    def to_list(self) -> list[dict]:
        return [s.to_dict() for s in self.stages]

    @classmethod
    def from_list(cls, items: list[dict]) -> "FilterChain":
        return cls(stages=[Stage.from_dict(d) for d in items])
