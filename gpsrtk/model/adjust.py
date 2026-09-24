"""Least-squares adjustment.

One small engine serves two problems that are the same shape:

  laser level network   unknowns are a height of instrument per setup plus an
                        elevation per shot point; observations are rod readings
  session offsets       unknowns are a constant vertical offset per acquisition
                        session; observations are elevation differences at
                        crossover points, which should be zero

Both are linear, both are rank-deficient by one until a datum is held, and both
want the same output: solved values, per-observation residuals, and an honest
statement of how much redundancy there was.

That last point matters more than it sounds. A network where every point was
shot exactly once has zero degrees of freedom: it will fit perfectly, produce
residuals of zero, and tell you nothing about whether any reading was a blunder.
Reporting `dof` alongside the answer is what stops a perfect fit being mistaken
for a good one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


class _UnionFind:
    """Tracks which unknowns are tied together by observations."""

    def __init__(self):
        self.parent: dict[str, str] = {}

    def add(self, x: str) -> None:
        self.parent.setdefault(x, x)

    def find(self, x: str) -> str:
        self.add(x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for x in self.parent:
            out.setdefault(self.find(x), []).append(x)
        return out


@dataclass
class Adjustment:
    """Solution of a least-squares problem, with its quality statement."""

    values: dict[str, float]
    std_errors: dict[str, float]
    residuals: np.ndarray
    labels: list[str]
    obs_mask: np.ndarray                # True for real observations
    n_obs: int
    n_unknowns: int
    n_constraints: int
    sigma0: float
    singular: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def dof(self) -> int:
        """Redundancy: real observations beyond what is needed to determine the
        free parameters. Zero means the fit is forced, not verified."""
        return self.n_obs - (self.n_unknowns - self.n_constraints)

    @property
    def has_redundancy(self) -> bool:
        return self.dof > 0

    @property
    def obs_residuals(self) -> np.ndarray:
        """Residuals of real observations only.

        Constraint and soft-anchor rows are bookkeeping, not measurements;
        including them makes a well-fitting network look like it has a huge
        outlier equal to whatever the datum shift happened to be.
        """
        return self.residuals[self.obs_mask]

    def residual_frame(self) -> pd.DataFrame:
        return pd.DataFrame({
            "label": np.asarray(self.labels)[self.obs_mask],
            "residual": self.obs_residuals,
        }).sort_values("residual", key=np.abs,
                       ascending=False).reset_index(drop=True)

    def worst(self, n: int = 5) -> pd.DataFrame:
        return self.residual_frame().head(n)

    def describe(self, unit: str = "m", scale: float = 1.0) -> str:
        lines = [
            f"{self.n_obs} observations, {self.n_unknowns} unknowns, "
            f"{self.n_constraints} constraints, dof {self.dof}",
        ]
        if not self.has_redundancy:
            lines.append(
                "  NO REDUNDANCY - residuals are structurally zero and cannot "
                "detect a blunder.")
        else:
            r = self.obs_residuals
            lines.append(
                f"  sigma0 {self.sigma0 * scale:.3f} {unit}   "
                f"max |residual| {np.abs(r).max() * scale:.3f} {unit}   "
                f"rms {np.sqrt((r ** 2).mean()) * scale:.3f} {unit}")
        if self.singular:
            lines.append("  SINGULAR - solved by minimum norm; check constraints.")
        lines.extend("  " + n for n in self.notes)
        return "\n".join(lines)


class LeastSquares:
    """Accumulates observation equations, then solves them.

    Each observation is a dict of {unknown name: coefficient} equalling a value.
    Unknowns are discovered from the names used, so callers never index columns.
    """

    def __init__(self):
        self._rows: list[dict[str, float]] = []
        self._values: list[float] = []
        self._weights: list[float] = []
        self._labels: list[str] = []
        self._kinds: list[str] = []
        self._n_constraints = 0
        self._uf = _UnionFind()

    def add(self, coeffs: dict[str, float], value: float,
            weight: float = 1.0, label: str = "", kind: str = "obs",
            connects: bool = True) -> None:
        self._rows.append(dict(coeffs))
        self._values.append(float(value))
        self._weights.append(float(weight))
        self._labels.append(label or f"obs{len(self._rows)}")
        self._kinds.append(kind)
        names = list(coeffs)
        for nm in names:
            self._uf.add(nm)
        if connects:
            for nm in names[1:]:
                self._uf.union(names[0], nm)

    def constrain(self, name: str, value: float, weight: float = 1e6) -> None:
        """Hold an unknown at a value. This is what supplies the datum."""
        self.add({name: 1.0}, value, weight, label=f"constraint {name}",
                 kind="constraint")
        self._n_constraints += 1

    def anchor(self, name: str, value: float = 0.0,
               weight: float = 1e-9) -> None:
        """Register an unknown with a negligible pull toward a value.

        Keeps a column in the system for something that no observation touches,
        so its absence is reported rather than silently dropped. The weight is
        small enough not to influence anything that is actually determined.
        """
        self.add({name: 1.0}, value, weight, label=f"anchor {name}",
                 kind="soft")

    def components(self) -> dict[str, list[str]]:
        """Groups of unknowns tied together by observations.

        Unknowns in different components are not connected by any observation,
        so their relative values are undetermined no matter how much data
        exists elsewhere.
        """
        return self._uf.groups()

    def solve(self) -> Adjustment:
        if not self._rows:
            raise ValueError("no observations to solve")

        names = sorted({n for r in self._rows for n in r})
        index = {n: i for i, n in enumerate(names)}

        n, u = len(self._rows), len(names)
        a = np.zeros((n, u))
        for i, row in enumerate(self._rows):
            for name, coeff in row.items():
                a[i, index[name]] = coeff
        l = np.asarray(self._values)
        w = np.asarray(self._weights)

        # Weighted least squares by scaling rows, which is better conditioned
        # than forming the normal equations directly.
        sw = np.sqrt(w)
        aw, lw = a * sw[:, None], l * sw

        x, _, rank, _ = np.linalg.lstsq(aw, lw, rcond=None)
        singular = rank < u

        residuals = a @ x - l
        obs_mask = np.array([k == "obs" for k in self._kinds])
        n_real = int(obs_mask.sum())

        # sigma0 is computed over real observations only. Constraint rows carry
        # an enormous weight and anchor rows a negligible one; letting either
        # into the variance makes the number meaningless.
        dof = n_real - (u - self._n_constraints)
        if dof > 0 and n_real:
            sigma0 = float(np.sqrt(
                (w[obs_mask] * residuals[obs_mask] ** 2).sum() / dof))
        else:
            sigma0 = 0.0

        try:
            cov = np.linalg.pinv(aw.T @ aw) * (sigma0 ** 2 if sigma0 else 1.0)
            errs = np.sqrt(np.clip(np.diag(cov), 0, None))
        except np.linalg.LinAlgError:
            errs = np.full(u, np.nan)

        return Adjustment(
            values={nm: float(x[index[nm]]) for nm in names},
            std_errors={nm: float(errs[index[nm]]) for nm in names},
            residuals=residuals,
            labels=list(self._labels),
            obs_mask=obs_mask,
            n_obs=n_real,
            n_unknowns=u,
            n_constraints=self._n_constraints,
            sigma0=sigma0,
            singular=singular,
        )
