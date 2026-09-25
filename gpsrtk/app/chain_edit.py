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
from ..model.pointset import FIX_FLOAT, FIX_RTK

# What each stage does, in words, for the Add list. A registered stage with
# no entry here is offered under its internal name rather than not at all.
PLAIN_NAMES = {
    "fix_select": "Fix quality (fixed / float)",
    "percentile_despike": "Remove height spikes (percentiles)",
    "speed_threshold": "Speed limits",
    "pdop_threshold": "PDOP limit",
    "accuracy_threshold": "Reported accuracy limit",
    "bin_to_cell": "Bin to cells",
    "track_select": "Keep or drop tracks",
    "session_select": "Keep or drop sessions",
    "kind_select": "Keep or drop by type",
    "time_window": "Time window",
    "polygon_select": "Inside or outside a polygon",
    "surface_residual": "Distance from a reference surface",
}

# Registered, so a saved chain that uses them still loads, but not offered:
# nothing ever sets the surface-residual stage's reference surface, so it
# passes every point through and would only look as if it filtered.
HIDDEN_KINDS = frozenset({"surface_residual"})

# The fix filter's values as the two choices anyone means by them, instead
# of the text "[4]".
FIX_CHOICES = (("fixed", FIX_RTK), ("float", FIX_FLOAT))


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
    params = []
    for key, value in stage.params().items():
        entry = {"key": key, "bool": isinstance(value, bool),
                 "value": value if isinstance(value, bool) else None,
                 "text": "" if isinstance(value, bool) else param_text(value)}
        if stage.kind == "fix_select" and key == "values":
            # The page sends the whole list back; values other than fixed
            # and float, if a chain ever held any, ride along untouched.
            chosen = [int(v) for v in value]
            entry["choices"] = [{"label": label, "value": code,
                                 "checked": code in chosen}
                                for label, code in FIX_CHOICES]
            entry["values"] = chosen
        params.append(entry)
    return {
        "kind": stage.kind,
        "label": stage.label,
        "name": PLAIN_NAMES.get(stage.kind, stage.kind),
        "enabled": stage.enabled,
        "params": params,
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
    kinds = [{"kind": k, "name": PLAIN_NAMES.get(k, k)} for k in REGISTRY
             if k not in HIDDEN_KINDS]
    return {"stages": stages, "total": total,
            "kinds": sorted(kinds, key=lambda k: k["name"].lower())}


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
        if isinstance(value, list):              # the fix filter's checkboxes
            value = tuple(int(v) for v in value)
        elif isinstance(known[key], bool):
            value = bool(value)
        else:
            value = parse_param(value)
        setattr(stage, key, value)
