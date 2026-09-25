"""Project files.

A project records how a surface was produced, not the surface itself: the site,
which exports were loaded, the filter chain, the vertical model, and which
basemaps were on. Everything derived is rebuilt on load, which keeps the file
small and, more importantly, keeps it honest - a stale cached raster can never
disagree with the parameters that claim to have produced it.

Source paths are stored relative to the project file whenever they sit under
the same directory, so a project folder can be moved or copied without
breaking. Absolute paths are kept for anything outside it.

Imagery is referenced by provider name only. The pixels live in the on-disk
cache keyed by extent, so reopening a project reuses them without a round trip
to a public service that may not be up.

Each source is also recorded by its size and SHA-256 (`source_fingerprints`,
an optional key older builds ignore). The stored vertical terms are re-applied
rather than re-solved, which is what makes a reopened project exact - and
also what would hand those terms, silently, to a file that had been
re-exported with different contents under the same name.

The shot plan travels WITH the project. It used not to, and that was a data
loss bug rather than a missing feature: typing measured coordinates into the
plan table and then saving the project wrote a file that silently did not
contain them. A plan is not a separate document from the survey it belongs
to. `.yardplan` still exists for carrying a plan between projects.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .site import Site

# 3: the site gained control marks (A3). A version 2 build refuses a
# version 3 file with its "newer version" message rather than dropping them.
VERSION = 3
SUFFIX = ".yardproj"


def fingerprint(path: str | Path) -> dict:
    """Size and SHA-256 of an export: what it holds, whatever it is called.

    A folder of CSVs (which the SW Maps reader also accepts) is hashed file
    by file in name order.
    """
    path = Path(path)
    digest = hashlib.sha256()
    size = 0
    files = sorted(path.glob("*.csv")) if path.is_dir() else [path]
    for f in files:
        if path.is_dir():
            digest.update(f.name.encode("utf-8") + b"\0")
        with open(f, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                digest.update(block)
                size += len(block)
    return {"size": size, "sha256": digest.hexdigest()}


@dataclass
class BasemapState:
    """One fetched raster and how it is displayed."""

    provider: str
    visible: bool = False
    opacity: float = 1.0

    def to_dict(self) -> dict:
        return {"provider": self.provider, "visible": self.visible,
                "opacity": self.opacity}

    @classmethod
    def from_dict(cls, d: dict) -> "BasemapState":
        return cls(provider=d["provider"], visible=bool(d.get("visible", False)),
                   opacity=float(d.get("opacity", 1.0)))


@dataclass
class Project:
    """Everything needed to reconstruct a working session."""

    site: Site
    sources: list[str] = field(default_factory=list)
    active_layer: str = ""
    chain: list[dict] = field(default_factory=list)
    vertical: dict | None = None
    basemaps: list[BasemapState] = field(default_factory=list)
    vectors: list[str] = field(default_factory=list)
    view: dict = field(default_factory=dict)
    plan: dict | None = None
    imagery_offset: list[float] | None = None
    source_fingerprints: dict[str, dict] = field(default_factory=dict)
    version: int = VERSION
    path: Path | None = None

    # --- serialisation ---------------------------------------------------

    def to_dict(self, base: Path | None = None) -> dict:
        return {
            "version": self.version,
            "site": self.site.to_dict(),
            "sources": [_store_path(s, base) for s in self.sources],
            "active_layer": self.active_layer,
            "chain": self.chain,
            "vertical": self.vertical,
            "basemaps": [b.to_dict() for b in self.basemaps],
            "vectors": list(self.vectors),
            "view": dict(self.view),
            "plan": self.plan,
            "imagery_offset": self.imagery_offset,
            "source_fingerprints": {_store_path(s, base): dict(fp) for s, fp
                                    in self.source_fingerprints.items()},
        }

    @classmethod
    def from_dict(cls, d: dict, base: Path | None = None) -> "Project":
        version = int(d.get("version", 0))
        if version > VERSION:
            raise ValueError(
                f"This project was written by a newer version of the tool "
                f"(format {version}, this build understands {VERSION}).")
        return cls(
            site=Site.from_dict(d["site"]),
            sources=[_resolve_path(s, base) for s in d.get("sources", [])],
            active_layer=d.get("active_layer", ""),
            chain=list(d.get("chain") or []),
            vertical=d.get("vertical"),
            basemaps=[BasemapState.from_dict(b) for b in d.get("basemaps", [])],
            vectors=list(d.get("vectors") or []),
            view=dict(d.get("view") or {}),
            plan=d.get("plan") or None,
            imagery_offset=d.get("imagery_offset") or None,
            source_fingerprints={
                _resolve_path(s, base): dict(fp) for s, fp
                in (d.get("source_fingerprints") or {}).items()},
            version=version,
        )

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        if path.suffix != SUFFIX:
            path = path.with_suffix(SUFFIX)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Always stamp the CURRENT format, whatever version was read in.
        # A v1 project reopened and saved now carries its plan, and would
        # be a liar if it still claimed to be v1.
        self.version = VERSION
        payload = self.to_dict(base=path.parent)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self.path = path
        return path

    @classmethod
    def load(cls, path: str | Path) -> "Project":
        path = Path(path)
        d = json.loads(path.read_text(encoding="utf-8"))
        project = cls.from_dict(d, base=path.parent)
        project.path = path
        return project

    def missing_sources(self) -> list[str]:
        """Source exports the project references but that are not on disk.

        Reported rather than raised: a project whose imagery is still cached is
        worth opening even if a survey zip has moved, and telling the user which
        file is missing is more use than refusing to load.
        """
        return [s for s in self.sources if not Path(s).exists()]


def _store_path(p: str | Path, base: Path | None) -> str:
    """Relative to the project file when it sits underneath it, else absolute."""
    p = Path(p).resolve()
    if base is not None:
        try:
            return p.relative_to(Path(base).resolve()).as_posix()
        except ValueError:
            pass
    return p.as_posix()


def _resolve_path(s: str, base: Path | None) -> str:
    p = Path(s)
    if not p.is_absolute() and base is not None:
        p = Path(base) / p
    return str(p)
