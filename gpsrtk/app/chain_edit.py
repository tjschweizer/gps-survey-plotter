"""Editing the filter stack.

Parameter editors are generated from each stage's `params()` rather than being
hand-built per stage, so a newly registered stage appears in the browser with
working controls and no UI code. What crosses to the browser is therefore a
description of each parameter - its current value as text, and whether it is a
switch - and what comes back is text to be parsed here, in one place.

The counts travel with the stages. A chain that quietly drops most of the data
is the easiest way to produce a confident, wrong surface, and that should be
visible without being asked for.
"""

from __future__ import annotations

import ast

from ..filters import REGISTRY, FilterChain, Stage


def parse_param(text):
    """Best-effort literal parse; anything unparseable stays a string."""
    if not isinstance(text, str):
        return text
    text = text.strip()
    if not text:
        return None
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return text


def param_text(value) -> str:
    """How a parameter is shown for editing. Strings keep their quotes, so
    that parsing the text back gives the same string rather than a number."""
    if value is None:
        return ""
    return repr(value) if isinstance(value, str) else str(value)


def stage_payload(stage: Stage, result) -> dict:
    counts = None
    if result is not None:
        counts = ("disabled" if not result.enabled else
                  f"{result.n_in:,} → {result.n_out:,}  "
                  f"({result.kept * 100:.1f}%)")
    return {
        "kind": stage.kind,
        "label": stage.label,
        "enabled": stage.enabled,
        "params": [{"key": key, "bool": isinstance(value, bool),
                    "value": value if isinstance(value, bool) else None,
                    "text": "" if isinstance(value, bool) else param_text(value)}
                   for key, value in stage.params().items()],
        "counts": counts,
    }


def chain_payload(chain: FilterChain) -> dict:
    results = list(chain.results)
    stages = [stage_payload(s, results[i] if i < len(results) else None)
              for i, s in enumerate(chain.stages)]
    total = ""
    if results:
        head, tail = results[0].n_in, results[-1].n_out
        pct = tail / head * 100 if head else 0
        total = f"{head:,} → {tail:,} points  ({pct:.1f}% kept)"
    return {"stages": stages, "total": total, "kinds": sorted(REGISTRY)}


# --- edits -------------------------------------------------------------------
# Each takes the chain and mutates it. Recomputing is the caller's business,
# because the caller also has to undo the edit if the recompute fails.

def _stage(chain: FilterChain, index: int) -> Stage:
    if not 0 <= index < len(chain.stages):
        raise IndexError(f"no stage at position {index}")
    return chain.stages[index]


def add_stage(chain: FilterChain, kind: str) -> None:
    if kind not in REGISTRY:
        raise ValueError(f"unknown stage '{kind}'")
    chain.stages.append(REGISTRY[kind]())


def remove_stage(chain: FilterChain, index: int) -> None:
    chain.stages.remove(_stage(chain, index))


def move_stage(chain: FilterChain, index: int, delta: int) -> None:
    _stage(chain, index)
    j = index + delta
    if 0 <= j < len(chain.stages):
        stages = chain.stages
        stages[index], stages[j] = stages[j], stages[index]


def edit_stage(chain: FilterChain, index: int, *, enabled: bool | None = None,
               params: dict | None = None) -> None:
    """Toggle a stage and/or set parameters from edited text.

    A switch arrives as a bool and is taken as-is; anything else arrives as
    the text typed and is parsed.
    """
    stage = _stage(chain, index)
    if enabled is not None:
        stage.enabled = bool(enabled)
    known = stage.params()
    for key, value in (params or {}).items():
        if key not in known:
            raise KeyError(f"{stage.label} has no parameter '{key}'")
        setattr(stage, key,
                bool(value) if isinstance(known[key], bool) else parse_param(value))
