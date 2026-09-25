"""Aligning imagery to measured ground.

An orthophoto is georeferenced, but not to survey accuracy. County and state
mosaics are typically good to a foot or two horizontally: enough to find your
house, not enough to click a driveway edge and call the result a coordinate.
The error is dominated by a translation over a lot this small, because the
sources of it - DEM error in the ortho-rectification, aerotriangulation
residual, mosaic seam choice - all vary on scales of hundreds of metres, not
tens.

So the correction that matters is a single shift, and it can be measured with
the data the plan already holds:

    planned position   where a feature APPEARS in the photo (you clicked it)
    measured position  where that feature actually IS (you shot it with RTK)

    shift = mean(measured - planned)

applied to the imagery so its content lands on truth.

Two things this depends on, both of which the solver checks rather than
assumes:

  The planned mark must have been placed by clicking the feature in the photo.
  A lawn spot clicked in open grass says nothing about where the photo puts
  anything, because there is no identifiable thing at that spot. Only purposes
  in `PHOTO_IDENTIFIABLE` are used by default.

  The residuals about the mean are the quality statement. A tight cluster means
  a genuine translation was found. A wide scatter means either the clicks were
  sloppy, the shots were, or the misalignment is not a translation at all - and
  in that case a single shift is the wrong model and forcing one just moves the
  error around.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


from .units import m_to_ft

# Purposes whose planned mark is plausibly a click on something visible in an
# aerial photo. Terrain spots are excluded on purpose: open lawn has no
# identifiable feature to click, so the planned position carries no information
# about where the imagery puts the ground. Nor is a property corner: a pin
# cannot be seen from the air, and a corner clicked on the cartographic
# parcel lines says nothing about the photo.
PHOTO_IDENTIFIABLE = frozenset({
    "building corner", "foundation", "driveway edge", "sidewalk edge",
    "curb flowline", "wall", "step", "fence", "utility", "bed edge",
    "monument", "plat reference",
})

# Beyond this the residuals are no longer consistent with a rigid shift, and a
# single offset is the wrong correction. Chosen against what the imagery is
# worth: the default 2016-2018 ortho is ~22 cm, so 0.5 m of unexplained scatter
# is already more than two pixels and well past useful.
SCATTER_WARN_M = 0.5


@dataclass
class ImageryOffset:
    """A translation to apply to every basemap, and how well it is determined."""

    de: float = 0.0
    dn: float = 0.0
    n: int = 0
    used: list[int] = field(default_factory=list)
    residuals_m: dict[int, tuple[float, float]] = field(default_factory=dict)
    rms_m: float = 0.0
    max_residual_m: float = 0.0

    @property
    def zero(self) -> bool:
        return self.de == 0.0 and self.dn == 0.0

    @property
    def magnitude_m(self) -> float:
        return float(np.hypot(self.de, self.dn))

    @property
    def dof(self) -> int:
        """Redundancy. Two unknowns, two observations per point."""
        return 2 * self.n - 2

    def to_list(self) -> list[float]:
        return [self.de, self.dn]

    @classmethod
    def from_list(cls, values) -> "ImageryOffset":
        if not values:
            return cls()
        de, dn = float(values[0]), float(values[1])
        return cls(de=de, dn=dn)

    def describe(self) -> str:
        if self.n == 0:
            return (f"Imagery shifted by hand: {m_to_ft(self.de):+.2f}, "
                    f"{m_to_ft(self.dn):+.2f} ft.")
        head = (f"Imagery shifted {m_to_ft(self.magnitude_m):.2f} ft "
                f"({m_to_ft(self.de):+.2f} E, {m_to_ft(self.dn):+.2f} N) "
                f"from {self.n} point{'s' if self.n != 1 else ''}")
        if self.n < 2:
            return (head + ". One point gives a shift with no check on it — "
                    "any error in that single click or shot goes straight "
                    "into the imagery.")
        return (head + f", dof {self.dof}. Residual scatter "
                f"{m_to_ft(self.rms_m) * 12:.1f} in RMS, worst "
                f"{m_to_ft(self.max_residual_m) * 12:.1f} in."
                + ("  That is wider than a rigid shift explains — the "
                   "misalignment is not a pure translation, or a click or a "
                   "shot is wrong. Check the residuals before trusting it."
                   if self.rms_m > SCATTER_WARN_M else ""))


def offset_candidates(plan, numbers=None) -> list:
    """Points that can contribute: a photo-identifiable feature, shot and clicked.

    A point qualifies only when it carries a measured position that is actually
    better than the click - `resolve` returning anything other than "planned".
    Passing `numbers` overrides the purpose filter, because a user who selected
    rows deliberately has said something the purpose column cannot.
    """
    chosen = None if numbers is None else set(numbers)
    out = []
    for p in plan.points:
        if chosen is not None:
            if p.number not in chosen:
                continue
        elif p.purpose not in PHOTO_IDENTIFIABLE:
            continue
        _, _, method, _ = plan.resolve(p.number)
        if method == "planned":
            continue
        out.append(p)
    return out


def solve_imagery_offset(plan, numbers=None) -> ImageryOffset:
    """Mean shift from planned clicks to measured positions.

    Unweighted on purpose. The dominant error here is how precisely a corner
    could be clicked on a 22 cm photo, which is the same for every point and
    which the receiver's accuracy figure says nothing about; weighting by a
    GNSS sigma would pretend to know a ratio that is not known.
    """
    points = offset_candidates(plan, numbers)
    if not points:
        raise ValueError(
            "No point can define the offset. A point qualifies when it is a "
            "feature you can see in the photo (a building corner, a driveway "
            "edge, a monument), its planned mark was placed by clicking that "
            "feature, and it carries a measured position. Select rows to "
            "override the purpose filter.")

    delta = np.array(
        [[plan.resolve(p.number)[0] - p.planned_e,
          plan.resolve(p.number)[1] - p.planned_n] for p in points], dtype=float)
    shift = delta.mean(axis=0)
    residuals = delta - shift

    return ImageryOffset(
        de=float(shift[0]), dn=float(shift[1]), n=len(points),
        used=[p.number for p in points],
        residuals_m={p.number: (float(r[0]), float(r[1]))
                     for p, r in zip(points, residuals)},
        rms_m=float(np.sqrt((residuals ** 2).sum(axis=1).mean())),
        max_residual_m=float(np.hypot(residuals[:, 0], residuals[:, 1]).max()),
    )


def residual_report(offset: ImageryOffset) -> list[str]:
    """Per-point residuals, worst first — the thing to read before accepting."""
    rows = sorted(offset.residuals_m.items(),
                  key=lambda kv: -np.hypot(*kv[1]))
    return [f"#{num}: {m_to_ft(np.hypot(de, dn)) * 12:5.1f} in "
            f"({m_to_ft(de) * 12:+.1f}, {m_to_ft(dn) * 12:+.1f})"
            for num, (de, dn) in rows]
