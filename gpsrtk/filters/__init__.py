"""Filter stages. Importing this package registers every built-in stage.

Note there is no point-domain smoothing stage. Median and gaussian smoothing
are raster operations and live in `gpsrtk.surface`, applied after gridding,
because smoothing scattered points before they are on a grid blurs across the
gaps between mowing passes rather than along the terrain.
"""

from .base import FilterChain, Stage, StageResult, REGISTRY, register
from .select import (FixSelect, KindSelect, PolygonSelect, SessionSelect,
                     TimeWindow, TrackSelect)
from .threshold import (AccuracyThreshold, PDOPThreshold, PercentileDespike,
                        SpeedThreshold, SurfaceResidual)
from .reduce import BinToCell

__all__ = [
    "FilterChain", "Stage", "StageResult", "REGISTRY", "register",
    "FixSelect", "KindSelect", "PolygonSelect", "SessionSelect", "TimeWindow",
    "TrackSelect", "AccuracyThreshold", "PDOPThreshold", "PercentileDespike",
    "SpeedThreshold", "SurfaceResidual", "BinToCell",
    "default_chain",
]


def default_chain(site=None) -> FilterChain:
    """The standing conventions from CLAUDE.md, as a starting chain.

    Fixed only, gross blunders clipped. Speed filtering is included but off by
    default: it buys real accuracy but preferentially discards the shaded
    western edge, which is exactly the area with the least coverage already.
    """
    chain = FilterChain()
    chain.add(FixSelect())
    chain.add(PercentileDespike())
    chain.add(SpeedThreshold(minimum=0.9, enabled=False))
    return chain
