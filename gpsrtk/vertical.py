"""Getting from ellipsoidal metres to elevations worth trusting.

The receiver reports ellipsoidal height of the *antenna*. Four separate things
stand between that and a usable ground elevation, and each is handled here:

  geoid separation   ellipsoidal -> NAVD88 orthometric. A single constant for a
                     residential lot: GEOID18 varies far below a millimetre
                     across 150 ft, so one fetched value covers the property.
  antenna height     SW Maps records `Instrument Ht` as 0.0 in every export so
                     far, so Z is antenna height, not ground.
  session offset     what remains after the recorded antenna height: phase
                     centre vs antenna reference point, base station
                     reselection, mount and tilt, day-to-day troposphere.
                     Constant within a session, unknown between them, solvable
                     from overlap.
  local datum        the site's arbitrary benchmark, historically spot ID 1 at
                     100.000 ft.

The laser level network is here too, because it answers the same question by a
different route: rod readings against a laser plane give ground elevation
directly, independent of GNSS vertical entirely.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from . import qc
from .model.adjust import Adjustment, LeastSquares
from .model.pointset import (PointSet, E, N, Z, ELEV, ROD_IN, SETUP, SESSION,
                             LAT, LON)
from .units import M_PER_IN, ft_to_m, m_to_ft

NGS_GEOID_URL = "https://geodesy.noaa.gov/api/geoid/ght"


# --- geoid ----------------------------------------------------------------

@dataclass
class GeoidSeparation:
    """Geoid height N, such that orthometric H = ellipsoidal h - N."""

    value_m: float
    model: str
    error_m: float | None = None
    lat: float | None = None
    lon: float | None = None

    def to_orthometric(self, h):
        return h - self.value_m

    def describe(self) -> str:
        e = f" +/- {self.error_m:.3f} m" if self.error_m is not None else ""
        return (f"{self.model}: N = {self.value_m:.3f} m{e}\n"
                f"  H = h - N, so ellipsoidal heights rise by "
                f"{-self.value_m:.3f} m.\n"
                f"  The stated error is ABSOLUTE. Relative accuracy across a "
                f"lot this size is sub-millimetre, so grading and slope are "
                f"unaffected by it.")


def fetch_geoid_separation(lat: float, lon: float, *, timeout: float = 20.0,
                           cache_path: str | Path | None = None
                           ) -> GeoidSeparation:
    """Look up the geoid height at one coordinate from NOAA NGS.

    One call per site is enough; the result is cached because the network is
    not always reachable in the field and the value never changes.
    """
    cache_path = Path(cache_path) if cache_path else None
    if cache_path and cache_path.exists():
        d = json.loads(cache_path.read_text("utf-8"))
        return GeoidSeparation(**d)

    import requests

    r = requests.get(NGS_GEOID_URL, params={"lat": lat, "lon": lon},
                     timeout=timeout)
    r.raise_for_status()
    payload = r.json()
    if "geoidHeight" not in payload:
        raise ValueError(f"NGS returned no geoid height: {payload}")

    sep = GeoidSeparation(
        value_m=float(payload["geoidHeight"]),
        model=str(payload.get("geoidModel", "unknown")),
        error_m=float(payload["error"]) if payload.get("error") is not None else None,
        lat=lat, lon=lon)

    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(sep.__dict__, indent=2), "utf-8")
    return sep


# --- laser level network --------------------------------------------------

@dataclass
class LevelNetwork:
    """Solved laser levelling: instrument heights and point elevations."""

    adjustment: Adjustment
    elevations: dict[int, float]        # point id -> elevation, metres
    instrument_heights: dict[str, float]  # setup id -> HI, metres
    benchmark: int
    benchmark_elev_m: float

    def describe(self) -> str:
        lines = ["Laser level network", self.adjustment.describe(unit="in",
                                                                 scale=1 / M_PER_IN)]
        for setup, hi in sorted(self.instrument_heights.items()):
            lines.append(f"  setup {setup}: HI {m_to_ft(hi):.4f} ft")
        lines.append(f"  benchmark: point {self.benchmark} held at "
                     f"{m_to_ft(self.benchmark_elev_m):.3f} ft")
        if not self.adjustment.has_redundancy:
            lines.append(
                "  To gain redundancy, shoot at least TWO common points from "
                "each pair of setups. A second setup adds one unknown (its own "
                "height of instrument), so a single shared point only "
                "determines it and buys no checking; the redundancy gained is "
                "(shared points - 1). Re-shooting a point from the SAME setup "
                "also works. Without either, a mis-read rod is undetectable.")
        return "\n".join(lines)


def resolve_setups(spots: PointSet, column: str = SETUP) -> pd.Series:
    """Assign every rod shot to an instrument setup.

    SW Maps carries the setup id as a manually typed attribute, and in practice
    it is entered once when the instrument is placed and left blank afterwards.
    Blank therefore means "same setup as the previous shot", not "unknown", so
    the column is forward-filled in time order.
    """
    d = spots.df
    if column not in d.columns:
        return pd.Series(["0"] * len(d), index=d.index)
    s = d[column].ffill()
    s = s.fillna(s.dropna().iloc[0] if s.notna().any() else 0)

    # SW Maps records the setup as a number; a shot plan labels setups "A",
    # "B". Normalise numbers so that 0 and 0.0 are the same setup, and leave
    # anything else as the label it already is.
    numeric = pd.to_numeric(s, errors="coerce")
    return pd.Series(
        [str(int(v)) if pd.notna(v) else str(raw).strip()
         for v, raw in zip(numeric, s)], index=d.index)


def level_network(spots: PointSet, *, benchmark_id: int = 1,
                  benchmark_elev_ft: float = 100.0,
                  rod_sigma_in: float = 0.125) -> LevelNetwork:
    """Solve instrument heights and point elevations from rod readings.

    Observation equation, per shot:  HI(setup) - elevation(point) = rod

    All in metres internally. `rod_sigma_in` is the assumed reading precision;
    it scales the reported standard errors but not the solution, since every
    reading carries the same weight.
    """
    d = spots.df
    if ROD_IN not in d.columns:
        raise ValueError("spot layer has no rod readings")

    have = d[ROD_IN].notna()
    if not have.any():
        raise ValueError("no usable rod readings")

    setups = resolve_setups(spots)
    ids = (d["point_id"] if "point_id" in d.columns
           else pd.Series(range(len(d)), index=d.index))

    ls = LeastSquares()
    weight = 1.0 / (rod_sigma_in * M_PER_IN) ** 2
    for i in d.index[have]:
        rod_m = float(d.at[i, ROD_IN]) * M_PER_IN
        pid = int(ids.at[i])
        ls.add({f"HI:{setups.at[i]}": 1.0, f"EL:{pid}": -1.0}, rod_m,
               weight=weight, label=f"point {pid} from setup {setups.at[i]}")

    bench_m = ft_to_m(benchmark_elev_ft)
    ls.constrain(f"EL:{benchmark_id}", bench_m, weight=weight * 1e6)

    adj = ls.solve()

    # Warn about shots the benchmark cannot reach: a setup with no tie to the
    # constrained point floats, and its elevations are meaningless in absolute
    # terms however tight the residuals look.
    groups = ls.components()
    anchored = {g for g, members in groups.items()
                if f"EL:{benchmark_id}" in members}
    floating = [m for g, members in groups.items() if g not in anchored
                for m in members if m.startswith("HI:")]
    if floating:
        adj.notes.append(
            "setups not tied to the benchmark: "
            + ", ".join(sorted(s.split(":", 1)[1] for s in floating)))

    return LevelNetwork(
        adjustment=adj,
        elevations={int(k.split(":", 1)[1]): v
                    for k, v in adj.values.items() if k.startswith("EL:")},
        instrument_heights={k.split(":", 1)[1]: v
                            for k, v in adj.values.items() if k.startswith("HI:")},
        benchmark=benchmark_id,
        benchmark_elev_m=bench_m)


# --- session offsets ------------------------------------------------------

@dataclass
class SessionOffsets:
    """Per-session constant vertical offsets solved from overlap."""

    adjustment: Adjustment
    offsets: dict[str, float]           # session -> offset, metres
    reference: str
    pair_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)

    def describe(self) -> str:
        lines = ["Session vertical offsets",
                 self.adjustment.describe(unit="cm", scale=100.0),
                 f"  reference session held at zero: {self.reference}"]
        for name, off in sorted(self.offsets.items()):
            se = self.adjustment.std_errors.get(f"OFF:{name}", float("nan"))
            flag = "  <- UNRESOLVED" if name in self.unresolved else ""
            lines.append(f"  {name:24s} {off * 100:+8.2f} cm "
                         f"(+/- {se * 100:.2f}){flag}")
        for (a, b), n in sorted(self.pair_counts.items()):
            lines.append(f"  {a} <-> {b}: {n} overlapping pairs")
        if self.unresolved:
            lines.append(
                "  Unresolved sessions share no overlapping ground with the "
                "reference. Their offset cannot be recovered from this data at "
                "all - it needs a re-visit covering common ground or a "
                "benchmark.")
        return "\n".join(lines)


def session_offsets(ps: PointSet, *, radius_m: float = 0.30,
                    min_seconds: float = 60.0,
                    reference: str | None = None,
                    column: str = Z) -> SessionOffsets:
    """Solve one constant vertical offset per session from crossover overlap.

    Observation, for a pair of nearby points from different sessions:

        offset(session_j) - offset(session_i) = z_j - z_i

    because the ground did not move between visits. Only differences are
    determined, so one session is held at zero; the others are relative to it.
    """
    if SESSION not in ps.df.columns:
        raise ValueError("point set carries no session labels")

    # Offsets are solved from GNSS heights disagreeing with each other, so
    # anything without a GNSS height has nothing to contribute. Laser rod
    # shots are the case in point: they reach the datum through the level
    # network instead, and leaving them in would have them reported as
    # "unresolved" for want of an overlap they could never have.
    if column in ps.df.columns:
        ps = ps.select(ps.df[column].notna().to_numpy(), f"has {column}")

    d = ps.df
    if len(d) == 0:
        raise ValueError(f"no points carry a {column} to compare")

    sessions = d[SESSION].astype(str).to_numpy()
    names = sorted(set(sessions))
    reference = reference or names[0]

    pairs = qc.crossover_pairs(ps, radius_m, min_seconds)
    z = d[column].to_numpy()

    ls = LeastSquares()
    counts: dict[tuple[str, str], int] = {}
    for i, j in pairs:
        si, sj = sessions[i], sessions[j]
        if si == sj:
            continue                      # same session says nothing about bias
        ls.add({f"OFF:{sj}": 1.0, f"OFF:{si}": -1.0}, z[j] - z[i],
               label=f"{si}~{sj}")
        key = (si, sj) if si < sj else (sj, si)
        counts[key] = counts.get(key, 0) + 1

    # Every session needs a column even if it never overlapped anything, so
    # that its absence is reported rather than silently dropped.
    for nm in names:
        ls.anchor(f"OFF:{nm}")
    ls.constrain(f"OFF:{reference}", 0.0)

    adj = ls.solve()

    # Connectivity comes from real overlapping pairs only. A session reachable
    # from the reference through a chain of overlaps has a determined offset;
    # one that is not, does not, however tightly the solver reports it.
    linked = {reference}
    changed = True
    while changed:
        changed = False
        for (a, b), _ in counts.items():
            if a in linked and b not in linked:
                linked.add(b); changed = True
            elif b in linked and a not in linked:
                linked.add(a); changed = True
    unresolved = sorted(set(names) - linked)

    return SessionOffsets(
        adjustment=adj,
        offsets={k.split(":", 1)[1]: v for k, v in adj.values.items()
                 if k.startswith("OFF:")},
        reference=reference,
        pair_counts=counts,
        unresolved=unresolved)


def offset_to_reference(ps: PointSet, reference: PointSet, *,
                        column: str = Z,
                        reference_column: str = ELEV) -> dict:
    """Constant vertical offset between a point set and a reference surface.

    This is the walked-versus-planted comparison from process_full.py,
    generalised: interpolate the reference at each point's location and take
    the mean difference. The scatter around that mean is the honest measure of
    how well a single constant describes the relationship.
    """
    from scipy.interpolate import griddata

    ref = reference.df
    zi = griddata(ref[[E, N]].to_numpy(), ref[reference_column].to_numpy(),
                  ps.df[[E, N]].to_numpy(), method="linear")
    diff = ps.df[column].to_numpy() - zi
    ok = np.isfinite(diff)
    if not ok.any():
        return {"n": 0}

    d = diff[ok]
    return {
        "n": int(ok.sum()),
        "offset_m": float(np.mean(d)),
        "median_m": float(np.median(d)),
        "scatter_m": float(np.std(d)),
        "max_dev_m": float(np.abs(d - np.mean(d)).max()),
    }


# --- applying a vertical model -------------------------------------------

def apply_vertical(ps: PointSet, *, geoid: GeoidSeparation | None = None,
                   antenna_height_m: float = 0.0,
                   offsets: dict[str, float] | None = None,
                   datum_shift_m: float = 0.0) -> PointSet:
    """Derive the canonical elevation column from ellipsoidal height.

        elevation = z - antenna_height - session_offset - geoid_N + datum_shift

    Each term is optional and each is recorded in the point set's history, so
    a surface can always be traced back to the assumptions that produced it.
    """
    d = ps.df.copy()
    z = d[Z].to_numpy(dtype=float)
    steps = []

    if antenna_height_m:
        z = z - antenna_height_m
        steps.append(f"antenna -{antenna_height_m:.3f} m")

    if offsets:
        if SESSION not in d.columns:
            raise ValueError("cannot apply session offsets without session labels")
        per = d[SESSION].astype(str).map(offsets).fillna(0.0).to_numpy(dtype=float)
        z = z - per
        steps.append(f"session offsets ({len(offsets)})")

    if geoid is not None:
        z = geoid.to_orthometric(z)
        steps.append(f"geoid {geoid.model} N={geoid.value_m:.3f} m")

    if datum_shift_m:
        z = z + datum_shift_m
        steps.append(f"datum shift {datum_shift_m:+.3f} m")

    d[ELEV] = z
    return ps.with_frame(d, "elevation: " + (", ".join(steps) or "copy of z"))


def datum_shift_for(elev_m: float, target_ft: float) -> float:
    """Shift that puts a known point at a chosen elevation on the local datum."""
    return ft_to_m(target_ft) - elev_m


def spots_with_laser_elevations(spots: PointSet, network: LevelNetwork
                                ) -> PointSet:
    """Attach solved laser elevations to the spot layer as the ELEV column."""
    d = spots.df.copy()
    ids = d["point_id"].astype("Int64")
    d[ELEV] = [network.elevations.get(int(i)) if pd.notna(i) else None
               for i in ids]
    keep = d[ELEV].notna()
    return spots.select(keep.to_numpy(), "has laser elevation").with_frame(
        d.loc[keep].reset_index(drop=True), "laser elevations")


# --- the whole vertical model --------------------------------------------

@dataclass
class VerticalModel:
    """Everything needed to turn reported height into trusted elevation."""

    mode: str = "local"                 # "local" | "navd88" | "ellipsoidal"
    geoid: GeoidSeparation | None = None
    offsets: dict[str, float] = field(default_factory=dict)
    datum_shift_m: float = 0.0
    reference_session: str = ""
    tied_to_model: bool = False
    model_frame: str = ""
    level: LevelNetwork | None = None
    sessions: SessionOffsets | None = None
    tie: dict | None = None
    notes: list[str] = field(default_factory=list)

    def apply(self, ps: PointSet) -> PointSet:
        return apply_vertical(
            ps,
            geoid=self.geoid if self.mode == "navd88" else None,
            offsets=self.offsets or None,
            datum_shift_m=self.datum_shift_m)

    # --- persistence -----------------------------------------------------

    def to_dict(self) -> dict:
        """Only the terms that change the numbers.

        The level network, session solution and tie are diagnostics: large,
        derived, and reproducible by re-solving. What must round-trip exactly
        is the arithmetic in `apply`, so that reopening a project reproduces
        identical elevations rather than merely similar ones.
        """
        return {
            "mode": self.mode,
            "geoid": (self.geoid.__dict__.copy() if self.geoid else None),
            "offsets": dict(self.offsets),
            "datum_shift_m": self.datum_shift_m,
            "reference_session": self.reference_session,
            "tied_to_model": self.tied_to_model,
            "model_frame": self.model_frame,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "VerticalModel":
        g = d.get("geoid")
        return cls(
            mode=d.get("mode", "local"),
            geoid=GeoidSeparation(**g) if g else None,
            offsets=dict(d.get("offsets") or {}),
            datum_shift_m=float(d.get("datum_shift_m", 0.0)),
            reference_session=d.get("reference_session", ""),
            tied_to_model=bool(d.get("tied_to_model", False)),
            model_frame=d.get("model_frame", ""),
            notes=list(d.get("notes") or []),
        )

    def describe(self) -> str:
        lines = [f"Vertical model: {self.mode}"]
        if self.mode == "local":
            lines.append(
                f"  datum: TIED to {self.model_frame}" if self.tied_to_model
                else "  datum: LOCAL ARBITRARY - elevations are self-consistent "
                     "but sit nowhere in particular")
        if self.mode == "navd88" and self.geoid:
            lines.append("  " + self.geoid.describe().replace("\n", "\n  "))
        if self.offsets:
            lines.append(f"  session offsets, reference "
                         f"{self.reference_session}:")
            for k, v in sorted(self.offsets.items()):
                lines.append(f"    {k:26s} {v * 100:+8.2f} cm")
        if self.tie and self.tie.get("n"):
            t = self.tie
            lines.append(
                f"  tie to laser surface, {t['n']:,} walked points inside the "
                f"shot hull: {t['offset_m'] * 100:+.1f} cm, scatter "
                f"{t['scatter_m'] * 100:.1f} cm, max deviation "
                f"{t['max_dev_m'] * 100:.1f} cm")
            lines.append(
                "    This constant absorbs antenna height above ground and the "
                "arbitrary datum together; the scatter is how well one number "
                "describes the relationship.")
        if self.datum_shift_m:
            lines.append(f"  datum shift {self.datum_shift_m:+.3f} m")
        lines.extend("  " + n for n in self.notes)
        return "\n".join(lines)


def solve_vertical(tracks: PointSet, spots: PointSet | None = None, *,
                   mode: str = "local",
                   benchmark_id: int = 1,
                   benchmark_elev_ft: float = 100.0,
                   radius_m: float = 1.0,
                   geoid: GeoidSeparation | None = None,
                   tied_to_model: bool = False,
                   model_frame: str = "",
                   lawn_kinds: tuple[str, ...] = ("lawn",)) -> VerticalModel:
    """Work out the full vertical model from the data available.

    The chain, in the order the information actually flows:

      1. Solve the laser level network. This gives true GROUND elevations on
         the site datum, independent of GNSS vertical entirely.
      2. Solve per-session offsets from GNSS crossover overlap, so every
         session speaks in the same vertical frame.
      3. Tie the session-corrected GNSS to the laser ground surface. That one
         constant absorbs the antenna height and the arbitrary datum together.

    In "navd88" mode step 3 is replaced by the geoid separation, giving
    orthometric heights of the ANTENNA unless an antenna height is also
    supplied - which is why "local" is the default for grading work.
    """
    from .filters import KindSelect
    from .model.pointset import concat

    model = VerticalModel(mode=mode, geoid=geoid,
                          tied_to_model=tied_to_model,
                          model_frame=model_frame)

    lawn = None
    if spots is not None and len(spots):
        lawn = KindSelect(names=list(lawn_kinds)).apply(spots)
        if ROD_IN in lawn.df.columns and lawn.df[ROD_IN].notna().any():
            ids = set(lawn.df["point_id"].dropna().astype(int)) \
                if "point_id" in lawn.df.columns else set()
            if ids and benchmark_id not in ids:
                # Silently falling back would produce a surface on a datum the
                # user believes is tied when it is not.
                raise ValueError(
                    f"benchmark point {benchmark_id} is not among the rod "
                    f"shots ({min(ids)}-{max(ids)}). Pick a point that was "
                    "actually shot, or clear the datum tie.")
            model.level = level_network(
                lawn, benchmark_id=benchmark_id,
                benchmark_elev_ft=benchmark_elev_ft)

    # --- session offsets ---
    combined = concat([tracks, lawn]) if lawn is not None and len(lawn) else tracks
    if SESSION in combined.df.columns and combined.df[SESSION].nunique() > 1:
        ref = str(tracks.df[SESSION].mode().iloc[0])
        model.sessions = session_offsets(combined, radius_m=radius_m,
                                         reference=ref)
        model.offsets = dict(model.sessions.offsets)
        model.reference_session = ref
        if model.sessions.unresolved:
            model.notes.append(
                "unresolved sessions (no overlap with the reference): "
                + ", ".join(model.sessions.unresolved))
    else:
        model.notes.append("only one session present; no offsets to solve")

    if mode == "ellipsoidal":
        return model

    if mode == "navd88":
        if geoid is None:
            raise ValueError("navd88 mode needs a geoid separation")
        return model

    # --- local: tie the corrected GNSS to the laser ground surface ---
    if model.level is None or lawn is None:
        model.notes.append(
            "no laser network available, so the local datum cannot be tied; "
            "elevations remain on the reference session's arbitrary frame")
        return model

    reference = spots_with_laser_elevations(lawn, model.level)
    provisional = apply_vertical(tracks, offsets=model.offsets or None)
    tie = offset_to_reference(provisional, reference, column=ELEV,
                              reference_column=ELEV)
    model.tie = tie
    if tie.get("n"):
        model.datum_shift_m = -tie["offset_m"]
    else:
        model.notes.append(
            "the laser spots and the walked data do not overlap, so no tie "
            "could be made")
    return model
