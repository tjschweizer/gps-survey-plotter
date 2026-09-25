"""Application state.

One object owns the site, the loaded layers, the filter chain, the vertical
model, imagery, and the derived surface. The views observe it and never talk to
each other, so adding a view later does not touch the existing ones.

It knows nothing about how it is displayed. Every change is announced through a
`Signal` that bumps a revision counter for its topic; the web server hands
those counters to the browser, which fetches again only what moved.

The pipeline has two stages and the distinction matters:

    source -> filter chain -> filtered -> vertical model -> corrected
           -> [shown sessions] -> result -> surface

Filtering decides which observations to believe. The vertical model decides
what their heights mean. Keeping them separate is what lets the filter stack be
re-tuned without re-solving the datum, and the datum be re-solved without
disturbing the filters.

The session step is a comparison, not a belief. Hiding a session takes its
points off the map; optionally it also takes them out of the surface and the
QC, so one outing can be judged on its own. It comes after the vertical model
on purpose: the datum is always solved from every outing, so looking at one
of them never moves it.

Surfaces are built at a lower resolution for interaction than for export.
Gridding 1024x1024 takes a second or two, which is fine once at export time and
intolerable on every checkbox toggle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..filters import FilterChain, default_chain
from ..io import read_any
from ..io.imagery import (ImageryProvider, NoCoverageError, RasterLayer,
                          default_providers, terrain_providers)
from ..io.vector import VectorLayer, default_vector_providers
from ..merge import MergeReport, crs_disagreement_m, diagnose, \
    instrument_height_notes, merge_layers, reproject
from ..model.pointset import PointSet, E, N, SESSION
from ..georef import ImageryOffset, solve_imagery_offset
from ..plan import FillReport, Plan
from ..project import BasemapState, Project
from ..site import Site, default_site
from ..surface import Extent, Surface, build_surface
from ..vertical import VerticalModel
from .events import Signal

# A check shot belongs to the track session nearest in time, if one is this
# close: an outing's start and end shots bracket its passes.
CHECK_WINDOW_S = 60 * 60.0

PREVIEW_SIZE = 320
EXPORT_SIZE = 1024
FIGURE_CELL_M = 0.08
IMAGERY_MARGIN_M = 20.0


@dataclass
class Basemap:
    """A fetched raster plus its display state."""

    provider: str
    layer: RasterLayer
    visible: bool = False
    opacity: float = 1.0
    terrain: bool = False        # a LiDAR product rather than a photograph
    # The raster encoded once for the browser, rather than on every redraw.
    png: bytes | None = field(default=None, repr=False, compare=False)


@dataclass
class FetchReport:
    """Outcome of fetching every provider at once."""

    fetched: list[str]
    no_coverage: list[str]
    failed: list[tuple[str, str]]

    def describe(self) -> str:
        lines = [f"{len(self.fetched)} fetched, "
                 f"{len(self.no_coverage)} with no coverage here, "
                 f"{len(self.failed)} failed."]
        if self.fetched:
            lines += ["", "Fetched:"] + [f"  {n}" for n in self.fetched]
        if self.no_coverage:
            lines += ["", "No coverage for this lot:"] + [
                f"  {n}" for n in self.no_coverage]
        if self.failed:
            lines += ["", "Failed:"] + [f"  {n} — {why}"
                                        for n, why in self.failed]
        return "\n".join(lines)


class AppState:
    """Owns everything the views draw."""

    PLAN_LAYER = "plan shots"

    def __init__(self, site: Site | None = None,
                 cache_dir: str | Path = "cache"):
        # One counter per topic. A view that remembers the numbers it last
        # drew from can tell exactly which parts of itself are stale.
        self.revisions: dict[str, int] = {}
        self.layersChanged = Signal("layers", self.revisions)
        self.selectionChanged = Signal("selection", self.revisions)
        self.resultChanged = Signal("result", self.revisions)
        self.verticalChanged = Signal("vertical", self.revisions)
        self.imageryChanged = Signal("imagery", self.revisions)
        self.planChanged = Signal("plan", self.revisions)
        self.sessionsChanged = Signal("sessions", self.revisions)
        self.siteChanged = Signal("site", self.revisions)
        self.viewChanged = Signal("view", self.revisions)
        self.statusMessage = Signal("status", self.revisions)
        self.status = ""
        self.statusMessage.connect(self._set_status)

        self.site: Site = site or default_site()
        self.cache_dir = Path(cache_dir)
        self.layers: dict[str, PointSet] = {}
        self.visible: dict[str, bool] = {}
        self.active_layer: str = ""
        self.chain: FilterChain = default_chain()

        self.filtered: PointSet | None = None      # after the filter chain
        self.corrected: PointSet | None = None     # after the vertical model
        self.result: PointSet | None = None        # the sessions being surfaced

        # Sessions taken off the map, and whether they are also taken out of
        # the surface and the QC. Names absent from the data are ignored.
        self.hidden_sessions: set[str] = set()
        self.surface_from_shown: bool = False
        self.surface: Surface | None = None
        self.vertical: VerticalModel | None = None

        # Basemaps are kept as an ordered mapping rather than a single layer,
        # so several can be stacked - a LiDAR hillshade at partial opacity over
        # an aerial photo says more than either on its own. Draw order is
        # insertion order, which puts photos underneath terrain rasters because
        # that is the order the providers are declared in.
        self.basemaps: dict[str, Basemap] = {}
        self.vectors: dict[str, VectorLayer] = {}
        # Every export currently loaded, in the order they were added. A
        # project is routinely more than one outing, and the vertical model
        # exists precisely because those outings disagree.
        self.sources: list[Path] = []
        # What each source held when it was loaded, by its path. A renamed
        # copy is the same data and must not merge in twice; a re-exported
        # file under an old name is different data and must not silently
        # inherit the offsets solved for the old one.
        self.fingerprints: dict[str, dict] = {}
        self.project_path: Path | None = None
        self.plan = Plan()

        # One translation for every basemap, not one per raster. They are
        # all derived from the same aerial survey campaigns and share the
        # bulk of their georeferencing error; and a per-raster offset
        # would let two photos of the same ground disagree, which is a
        # worse lie than either being off.
        self.imagery_offset = ImageryOffset()

        # The plan readings the current vertical model was solved with, so an
        # edit that does not touch them does not trigger a re-solve.
        self._solved_plan: str | None = None

        # What the last plan open filled in from SW Maps records.
        self.last_fill = FillReport()

        # Crossover pairs of the point sets the readouts describe, held with
        # the set itself so that a new set can never be mistaken for an old
        # one that happened to live at the same address.
        self._pairs: list[tuple[PointSet, object]] = []

        # View settings (colour map, layer toggles) as last saved or opened.
        # The state does not use them; it carries them so that a project
        # reopens looking the way it was left.
        self.view: dict = {}

    def _set_status(self, message: str) -> None:
        self.status = message

    @property
    def title(self) -> str:
        return ("Yard Survey — " + self.project_path.name
                if self.project_path is not None else "Yard Survey")

    # --- loading ---------------------------------------------------------

    @property
    def source_path(self) -> Path | None:
        """The first export loaded. Names the project and nothing else."""
        return self.sources[0] if self.sources else None

    def load(self, path: str | Path, *, merge: bool = False) -> MergeReport:
        """Read an export. Replaces what is loaded unless `merge` is set.

        Merging keeps the existing layers and adds to them, which is how a
        second outing over the same lawn joins the first. It deliberately
        discards any solved vertical model: that model was solved for a set of
        sessions that no longer exists, and re-applying it to new data would
        put the new points on a datum nothing measured.
        """
        from ..project import fingerprint

        path = Path(path)
        if merge and any(p.resolve() == path.resolve() for p in self.sources):
            raise ValueError(
                f"{path.name} is already loaded. Adding it twice would double "
                "every point in it and make the duplicates look like a "
                "perfect crossover.")
        if not path.exists():
            raise FileNotFoundError(path)
        fp = fingerprint(path)
        if merge:
            twin = next((p for p in self.sources
                         if self.fingerprints.get(str(p), {}).get("sha256")
                         == fp["sha256"]), None)
            if twin is not None:
                raise ValueError(
                    f"{path.name} has exactly the same contents as "
                    f"{twin.name}, which is already loaded - a renamed copy. "
                    "Adding it would double every point in it and make the "
                    "duplicates look like a perfect crossover.")

        exp = read_any(path)
        # Refuse before touching anything. A CSV that is not a survey reads
        # as a table with no points, and replacing the session with that
        # used to throw away the layers, the solved model and the session
        # choices on a mis-click in the file dialog.
        if not exp.layers:
            raise ValueError(
                f"{path.name} contains no survey points - no layer in it "
                "has easting, northing and elevation columns. Nothing loaded "
                "has been changed.")
        report = MergeReport()
        report.notes += instrument_height_notes(exp.layers, path.name)

        incoming, reprojected = self._reconcile_crs(exp.layers)
        report.reprojected = reprojected

        if merge and self.layers:
            plan_layer = self.layers.pop(self.PLAN_LAYER, None)
            self.layers, report.layers = merge_layers(self.layers, incoming)
            if plan_layer is not None:
                self.layers[self.PLAN_LAYER] = plan_layer
            self.sources.append(path)
            self.fingerprints[str(path)] = fp
            if self.vertical is not None:
                self.vertical = None
                report.notes.append(
                    "The solved vertical model was cleared: it was solved for "
                    "a different set of sessions. Re-solve from the Datum menu.")
        else:
            self.layers = dict(incoming)
            self.visible = {}
            self.sources = [path]
            self.fingerprints = {str(path): fp}
            self.hidden_sessions = set()
            self.vertical = None
            report.layers = {k: (0, len(v)) for k, v in incoming.items()}

        for k in self.layers:
            self.visible.setdefault(k, True)

        filled = self.fill_plan_from_records()
        if filled.filled or filled.conflicts:
            report.notes.append(filled.describe())

        # Prefer continuous logging as the working layer; it is what surfaces
        # are built from. Fall back to whichever layer has the most points.
        if self.active_layer not in self.layers:
            if "track_points" in self.layers:
                self.active_layer = "track_points"
            elif self.layers:
                self.active_layer = max(self.layers,
                                        key=lambda k: len(self.layers[k]))

        self.statusMessage.emit(
            ("Merged " if merge else "Loaded ") + path.name + ": "
            + ", ".join(f"{k} ({len(v):,})" for k, v in self.layers.items()))
        self.layersChanged.emit()
        self.recompute()

        if self.source is not None:
            diagnosis = self.session_report()
            report.sessions = diagnosis.sessions
            report.overlaps = diagnosis.overlaps
            report.unlinked = diagnosis.unlinked
            report.thin = diagnosis.thin
            report.static_shots = diagnosis.static_shots
            report.check_shots = diagnosis.check_shots
        return report

    def add_export(self, path: str | Path) -> MergeReport:
        """Load an export alongside what is already there."""
        return self.load(path, merge=True)

    def _reconcile_crs(self, layers: dict[str, PointSet]):
        """Put every incoming layer in the site's CRS.

        A reader is handed a file, not a site, so the `.swmz` reader picks the
        UTM zone from the data itself. Usually that is the same zone the site
        uses and the coordinates agree to under a millimetre. When it is not -
        a lot near a zone boundary, or a second property - silently mixing the
        two would put the layers kilometres apart while both look plausible.
        """
        out: dict[str, PointSet] = {}
        reprojected: list[str] = []
        for name, ps in layers.items():
            if crs_disagreement_m(ps, self.site.epsg) > 1.0:
                ps = reproject(ps, self.site.epsg)
                reprojected.append(name)
            out[name] = ps
        return out, reprojected

    def session_report(self) -> MergeReport:
        """Sessions and overlap for the data currently loaded.

        Judged on the set the offset solver sees - the filtered working
        layer plus the terrain rod shots - so the report cannot call a
        session unrecoverable that the solve then ties, or the reverse.
        Sessions are still described from what was logged.
        """
        from ..filters import KindSelect
        from ..model.pointset import Z, concat

        source = self.source
        if source is None:
            return MergeReport()
        basis = self.filtered if self.filtered is not None else source
        spots = self.spots
        lawn = None
        if spots is not None and Z in spots.df.columns:
            # Only shots with a GNSS height: a plan reading has none, and
            # would otherwise be listed as a session that overlaps nothing.
            lawn = KindSelect(names=["lawn"]).apply(
                spots.select(spots.df[Z].notna().to_numpy(), "has z"))
        checks = self.check_shots()
        if lawn is None or not len(lawn):
            return diagnose(source, overlap=basis, checks=checks)
        return diagnose(concat([source, lawn]), overlap=concat([basis, lawn]),
                        checks=checks)

    def check_shots(self):
        """Check shots on control marks, each given the session it checks.

        A SW Maps record whose station is a control mark is a check shot. It
        belongs to the track session nearest in time within the same export
        (by the session name's export prefix), if one is within
        `CHECK_WINDOW_S`: the start and end shots of an outing bracket its
        passes. Returns a frame of `mark`, `session`, `z`, `time`.
        """
        import pandas as pd

        from ..merge import describe_sessions
        from ..model.pointset import TIME, Z
        from ..vertical import stations

        columns = ["mark", "session", "z", "time"]
        marks = set(self.site.mark_names)
        source = self.source
        if not marks or source is None:
            return pd.DataFrame(columns=columns)
        tracks = [s for s in describe_sessions(source)
                  if s.start is not None and s.end is not None]
        rows = []
        for name, ps in self.layers.items():
            if name in (self.active_layer, self.PLAN_LAYER):
                continue
            d = ps.df
            if not {Z, TIME, SESSION}.issubset(d.columns):
                continue
            keys = stations(ps, marks)
            on_mark = (keys.isin(marks) & d[Z].notna() & d[TIME].notna()).to_numpy()
            for i in d.index[on_mark]:
                t = d[TIME].at[i]
                export = str(d[SESSION].at[i]).rsplit("/", 1)[0]
                best, gap = None, None
                for s in tracks:
                    if s.name.rsplit("/", 1)[0] != export:
                        continue
                    g = (0.0 if s.start <= t <= s.end else
                         min(abs((t - s.start).total_seconds()),
                             abs((t - s.end).total_seconds())))
                    if gap is None or g < gap:
                        best, gap = s.name, g
                if best is not None and gap <= CHECK_WINDOW_S:
                    rows.append({"mark": keys.at[i], "session": best,
                                 "z": float(d[Z].at[i]), "time": t})
        return pd.DataFrame(rows, columns=columns)

    def set_control_marks(self, marks) -> VerticalModel | None:
        """Replace the site's control marks.

        Marks change which records are check shots and which rod readings
        share a station, so a solved model is re-solved.
        """
        from ..site import ControlMark

        cleaned, seen = [], set()
        for m in marks:
            mark = m if isinstance(m, ControlMark) else ControlMark.from_dict(m)
            if not mark.name:
                continue
            if mark.name in seen:
                raise ValueError(f"control mark {mark.name} is listed twice")
            if mark.name.isdigit() or mark.name.startswith("P") and mark.name[1:].isdigit():
                raise ValueError(
                    f"'{mark.name}' cannot be a mark name: numbers are SW Maps "
                    "IDs and P<number> is a planned shot")
            seen.add(mark.name)
            cleaned.append(mark)
        self.site.control = cleaned
        self.siteChanged.emit()
        self.sessionsChanged.emit()
        if self.vertical is not None:
            return self.solve_vertical(self.vertical.mode)
        self.verticalChanged.emit()
        return None

    # --- pipeline --------------------------------------------------------

    @property
    def source(self) -> PointSet | None:
        return self.layers.get(self.active_layer)

    @property
    def spots(self) -> PointSet | None:
        """Rod-reading points feeding the level network.

        Plan observations and imported spot layers are combined, because they
        are the same kind of measurement - a rod reading against the laser
        plane - and the level network should solve them together. Keeping them
        apart would mean two datums for one instrument setup.
        """
        from ..model.pointset import ROD_IN, concat

        sets = []
        for name, ps in self.layers.items():
            if name == self.active_layer or name == self.PLAN_LAYER:
                continue
            if ROD_IN in ps.df.columns and ps.df[ROD_IN].notna().any():
                sets.append(ps)
        planned = self.plan_pointset()
        if planned is not None:
            planned = self._without_copied_readings(planned, sets)
        if planned is not None and len(planned):
            sets.append(planned)

        if not sets:
            return None
        return sets[0] if len(sets) == 1 else concat(sets, layer="rod shots")

    @staticmethod
    def _without_copied_readings(planned: PointSet, records: list[PointSet]
                                 ) -> PointSet:
        """Plan rows that are not copies of a SW Maps reading.

        A reading filled into the plan from a SW Maps record (same station,
        same setup, same rod) is one observation, not two. Counting it twice
        would add a degree of freedom that checks nothing.
        """
        from ..model.pointset import ROD_IN, concat
        from ..plan import FILL_ROD_TOLERANCE_IN
        from ..vertical import resolve_setups, stations

        if not records:
            return planned
        both = records[0] if len(records) == 1 else concat(records)
        shot = both.df[ROD_IN].notna().to_numpy()
        seen: dict[tuple, list[float]] = {}
        for st, su, rod in zip(stations(both)[shot], resolve_setups(both)[shot],
                               both.df[ROD_IN][shot]):
            seen.setdefault((st, su), []).append(float(rod))
        mine = zip(stations(planned), resolve_setups(planned), planned.df[ROD_IN])
        keep = [not any(abs(float(rod) - r) <= FILL_ROD_TOLERANCE_IN
                        for r in seen.get((st, su), ()))
                for st, su, rod in mine]
        return planned if all(keep) else planned.select(keep, "not copied")

    def plan_records(self) -> list[dict]:
        """SW Maps records named after a plan shot ("P12"), earliest first."""
        import pandas as pd

        from ..model.pointset import FIX, ROD_IN, TIME
        from ..vertical import PLAN_STATION, resolve_setups, stations

        rows = []
        for name, ps in self.layers.items():
            if name in (self.active_layer, self.PLAN_LAYER):
                continue
            keys = stations(ps)
            named = keys.map(lambda k: isinstance(k, str)
                             and PLAN_STATION.match(k) is not None)
            if not named.any():
                continue
            d = ps.df
            setups = resolve_setups(ps)
            for i in d.index[named.to_numpy()]:
                pid = d["point_id"].at[i] if "point_id" in d.columns else None
                rows.append({
                    "number": int(keys.at[i][1:]),
                    "e": float(d[E].at[i]), "n": float(d[N].at[i]),
                    "fix": (int(d[FIX].at[i]) if FIX in d.columns
                            and d[FIX].notna().at[i] else None),
                    "rod_in": (float(d[ROD_IN].at[i]) if ROD_IN in d.columns
                               and d[ROD_IN].notna().at[i] else None),
                    "setup": (setups.at[i] if ROD_IN in d.columns
                              and d[ROD_IN].notna().at[i] else ""),
                    "label": (f"SW Maps {name} record {pid}" if pd.notna(pid)
                              else f"SW Maps {name} record"),
                    "time": (d[TIME].at[i] if TIME in d.columns
                             and pd.notna(d[TIME].at[i]) else pd.Timestamp.max),
                })
        rows.sort(key=lambda r: r["time"])
        return rows

    def fill_plan_from_records(self):
        """Fill plan shots from SW Maps records named after them.

        Runs after an export is opened or merged and after a plan is opened.
        Only empty cells are filled; disagreements are reported, never
        written. Returns the `FillReport`.
        """
        report = self.plan.fill_from_records(self.plan_records())
        if report.changed:
            self.refresh_plan_layer()
        return report

    def plan_pointset(self) -> PointSet | None:
        """Plan observations that have a reading, as a PointSet."""
        frame = self.plan.to_frame()
        if frame.empty:
            return None
        frame = frame.copy()
        frame["source"] = "plan"
        frame["session"] = "plan"
        frame["z_ellip_m"] = float("nan")   # the laser supplies height, not GNSS
        return PointSet(df=frame, layer=self.PLAN_LAYER,
                        history=("shot plan",))

    def refresh_plan_layer(self) -> None:
        """Expose plan observations as a layer so they are visible and countable."""
        planned = self.plan_pointset()
        if planned is None:
            self.layers.pop(self.PLAN_LAYER, None)
            self.visible.pop(self.PLAN_LAYER, None)
        else:
            self.layers[self.PLAN_LAYER] = planned
            self.visible.setdefault(self.PLAN_LAYER, True)
        self.planChanged.emit()
        self.layersChanged.emit()

    def recompute(self, build: bool = True) -> None:
        src = self.source
        if src is None:
            self.filtered = self.corrected = self.result = self.surface = None
            self.resultChanged.emit()
            return

        self.filtered = self.chain.run(src)
        self.corrected = (self.vertical.apply(self.filtered)
                          if self.vertical is not None else self.filtered)
        self.result = (self.shown(self.corrected) if self.surface_from_shown
                       else self.corrected)

        self.surface = None
        if build and len(self.result) >= 3:
            try:
                self.surface = build_surface(self.result, self.site,
                                             size=PREVIEW_SIZE,
                                             fixed=self.laser_points())
            except Exception as exc:                      # noqa: BLE001
                self.statusMessage.emit(f"Could not build surface: {exc}")
        elif build:
            self.statusMessage.emit(
                f"Only {len(self.result)} points survive the chain - "
                "too few to grid.")
        self.resultChanged.emit()

    def crossover_pairs(self, ps: PointSet):
        """`qc.crossover_pairs` of `ps` (30 cm, 60 s), found once per set.

        The QC readout and the Sessions panel both need them, for the same
        points, and the neighbour search is the expensive part of either.
        """
        from .. import qc

        for cached, pairs in self._pairs:
            if cached is ps:
                return pairs
        pairs = qc.crossover_pairs(ps)
        # `result` and `corrected` are the only sets asked about.
        self._pairs = [(ps, pairs)] + self._pairs[:1]
        return pairs

    def figure_surface(self, cell_m: float = FIGURE_CELL_M) -> Surface | None:
        """A surface for printed maps, gridded at about `cell_m` per pixel.

        Figures are judged by eye at a fixed printed size, so they want a
        ground resolution rather than a pixel count: fine enough that
        contours are smooth curves, not so fine that a lot this size takes
        seconds to grid.
        """
        if self.result is None or len(self.result) < 3:
            return None
        from ..surface import bin_cells

        b = bin_cells(self.result, self.site.surface.bin_cell_m, self.site)
        span = Extent.square_around(b.x.to_numpy(), b.y.to_numpy()).width
        size = int(min(1024, max(256, round(span / cell_m))))
        return build_surface(self.result, self.site, size=size,
                             fixed=self.laser_points())

    def export_surface(self) -> Surface | None:
        """Full-resolution surface for writing rasters."""
        if self.result is None or len(self.result) < 3:
            return None
        return build_surface(self.result, self.site, size=EXPORT_SIZE,
                             fixed=self.laser_points())

    def laser_points(self):
        """Terrain rod shots at their laser elevations, for the surface.

        Only after a LOCAL solve that was tied to the laser: then the laser
        elevations and the corrected GNSS heights are on one datum, and the
        laser - a level read to an eighth of an inch - is the better height.
        In ellipsoidal or NAVD88 mode the GNSS heights are antenna heights on
        another datum entirely, and mixing the two would put steps in the
        ground, so there are none. One point per station, `e`, `n`, `z`
        (metres), or None.

        The level network is not stored in a project, so after a reopen it
        is solved again from the same rod readings - which gives the same
        elevations, since nothing else goes into it.
        """
        import pandas as pd

        from .. import vertical as V
        from ..filters import KindSelect
        from ..model.pointset import ELEV

        model, spots = self.vertical, self.spots
        if (model is None or model.mode != "local" or not model.datum_shift_m
                or spots is None):
            return None
        marks = self.site.mark_names
        level = model.level
        if level is None:
            v = self.site.vertical
            try:
                level = V.level_network(spots, benchmark_id=v.benchmark_point_id,
                                        benchmark_elev_ft=v.benchmark_elev_ft,
                                        marks=marks)
            except ValueError:
                return None
        lawn = KindSelect(names=["lawn"]).apply(spots)
        shots = V.spots_with_laser_elevations(lawn, level, marks)
        if not len(shots):
            return None
        d = shots.df
        frame = pd.DataFrame({"station": V.stations(shots, marks).astype(str),
                              "e": d[E].to_numpy(), "n": d[N].to_numpy(),
                              "z": d[ELEV].to_numpy(dtype=float)})
        return (frame.groupby("station", sort=False)
                .agg(e=("e", "mean"), n=("n", "mean"), z=("z", "first"))
                .reset_index())

    def set_active_layer(self, name: str) -> None:
        if name != self.active_layer and name in self.layers:
            self.active_layer = name
            self.recompute()

    # --- sessions ------------------------------------------------------

    @property
    def sessions(self) -> list[str]:
        """Every acquisition session in the active layer, sorted by name."""
        src = self.source
        if src is None or SESSION not in src.df.columns:
            return []
        return sorted(set(src.df[SESSION].dropna().astype(str)))

    @property
    def hiding(self) -> set[str]:
        """Hidden sessions that are actually present."""
        return self.hidden_sessions & set(self.sessions)

    def shown(self, ps: PointSet | None) -> PointSet | None:
        """The rows of `ps` whose session is not hidden."""
        hidden = self.hiding
        if ps is None or not hidden or SESSION not in ps.df.columns:
            return ps
        keep = ~ps.df[SESSION].astype(str).isin(hidden).to_numpy()
        return ps.select(keep, "shown sessions")

    def set_sessions_shown(self, shown) -> None:
        """Show exactly these sessions and hide the rest."""
        shown = set(map(str, shown))
        self._set_hidden({n for n in self.sessions if n not in shown})

    def set_session_visible(self, name: str, visible: bool) -> None:
        hidden = set(self.hidden_sessions)
        (hidden.discard if visible else hidden.add)(str(name))
        self._set_hidden(hidden)

    def _set_hidden(self, hidden: set[str]) -> None:
        if hidden == self.hidden_sessions:
            return
        self.hidden_sessions = hidden
        self.sessionsChanged.emit()
        if self.surface_from_shown:
            self.recompute()

    def set_surface_from_shown(self, on: bool) -> None:
        """Whether hidden sessions also leave the surface and the QC."""
        on = bool(on)
        if on == self.surface_from_shown:
            return
        self.surface_from_shown = on
        self.sessionsChanged.emit()
        self.recompute()

    def set_visible(self, name: str, visible: bool) -> None:
        self.visible[name] = visible
        self.selectionChanged.emit()

    # --- vertical model --------------------------------------------------

    def solve_vertical(self, mode: str = "local", **kw) -> VerticalModel:
        from .. import vertical as V

        if self.filtered is None:
            raise ValueError("load data first")

        geoid = kw.pop("geoid", None)
        if mode == "navd88" and geoid is None:
            geoid = self.fetch_geoid()

        # The benchmark lives on the site, so that tying the datum is a change
        # to the site's definition rather than a per-run argument that could
        # silently differ between a preview and an export.
        v = self.site.vertical
        kw.setdefault("benchmark_id", v.benchmark_point_id)
        kw.setdefault("benchmark_elev_ft", v.benchmark_elev_ft)
        kw.setdefault("marks", self.site.mark_names)
        kw.setdefault("checks", self.check_shots())
        model = V.solve_vertical(self.filtered, self.spots, mode=mode,
                                 geoid=geoid, tied_to_model=v.tied_to_model,
                                 model_frame=v.model_frame, **kw)
        self.vertical = model
        self._solved_plan = self._plan_signature()
        self.verticalChanged.emit()
        self.recompute()
        return model

    def clear_vertical(self) -> None:
        self.vertical = None
        self.verticalChanged.emit()
        self.recompute()

    def fetch_geoid(self):
        """Geoid separation at the middle of the data, cached on disk."""
        from ..model.pointset import LAT, LON
        from ..vertical import fetch_geoid_separation

        src = self.filtered if self.filtered is not None else self.source
        if src is None or not src.has(LAT, LON):
            raise ValueError("no coordinates available for a geoid lookup")
        lat = float(src.df[LAT].median())
        lon = float(src.df[LON].median())
        return fetch_geoid_separation(
            lat, lon, cache_path=self.cache_dir / "geoid.json")

    # --- imagery and reference linework ----------------------------------

    def data_extent(self, margin: float = IMAGERY_MARGIN_M) -> Extent | None:
        """Square extent covering the loaded survey area, with a margin.

        Deliberately measured from the SOURCE layer, not the filtered result.
        Tying it to the filtered points would make the extent move every time a
        threshold changed, which silently invalidates the imagery cache key and
        sends a fresh request to a public service on every filter tweak. It is
        also the wrong picture: the aerial photo should not shrink because a
        speed filter discarded the points near the edge of the lot.
        """
        src = self.source
        if src is None or len(src) == 0:
            return None
        x, y = src.df[E].to_numpy(), src.df[N].to_numpy()
        ext = Extent.square_around(x, y)
        return Extent(ext.xmin - margin, ext.xmax + margin,
                      ext.ymin - margin, ext.ymax + margin)

    def all_providers(self) -> dict[str, tuple[ImageryProvider, bool]]:
        """Every bundled raster provider, flagged as terrain or photo."""
        out: dict[str, tuple[ImageryProvider, bool]] = {}
        for name, p in default_providers().items():
            out[name] = (p, False)
        for name, p in terrain_providers().items():
            out[name] = (p, True)
        return out

    def fetch_imagery(self, provider, size: int = 1024, *,
                      terrain: bool = False, visible: bool = True) -> RasterLayer:
        from ..io.imagery import fetch_cached

        ext = self.data_extent()
        if ext is None:
            raise ValueError("load data first so the extent is known")
        layer = fetch_cached(provider, ext, self.site.epsg, size,
                             cache_dir=self.cache_dir)
        self.basemaps[provider.name] = Basemap(
            provider=provider.name, layer=layer, visible=visible,
            terrain=terrain)
        self.imageryChanged.emit()
        return layer

    def fetch_all_imagery(self, size: int = 1024,
                          progress=None) -> FetchReport:
        """Fetch every bundled provider once, then let the user toggle them.

        A provider that has no coverage here, or is simply down, is recorded
        and skipped rather than aborting the run - the whole point is to find
        out which sources actually work for this lot in one pass.

        Only the first photo layer that succeeds is switched on, so the map
        does not end up showing whichever raster happened to be fetched last.
        """
        fetched, no_cover, failed = [], [], []
        for name, (provider, terrain) in self.all_providers().items():
            if progress is not None:
                progress(name)
            try:
                self.fetch_imagery(provider, size, terrain=terrain,
                                   visible=False)
                fetched.append(name)
            except NoCoverageError:
                no_cover.append(name)
            except Exception as exc:                      # noqa: BLE001
                failed.append((name, f"{type(exc).__name__}: {exc}"[:90]))

        for name in fetched:
            if not self.basemaps[name].terrain:
                self.basemaps[name].visible = True
                break

        self.imageryChanged.emit()
        return FetchReport(fetched, no_cover, failed)

    def load_imagery_file(self, path: str | Path, size: int = 1024) -> RasterLayer:
        from ..io.imagery import LocalRasterProvider

        provider = LocalRasterProvider(path)
        ok, msg = provider.available()
        if not ok:
            raise ValueError(f"{Path(path).name}: {msg}")
        ext = self.data_extent()
        if ext is None:
            raise ValueError("load data first so the extent is known")
        layer = provider.fetch(ext, self.site.epsg, size)
        name = Path(path).name
        self.basemaps[name] = Basemap(provider=name, layer=layer, visible=True)
        self.imageryChanged.emit()
        return layer

    def set_basemap_visible(self, name: str, visible: bool) -> None:
        if name in self.basemaps:
            self.basemaps[name].visible = visible
            self.imageryChanged.emit()

    def set_basemap_opacity(self, name: str, opacity: float) -> None:
        if name in self.basemaps:
            self.basemaps[name].opacity = max(0.0, min(1.0, opacity))
            self.imageryChanged.emit()

    def clear_imagery(self) -> None:
        self.basemaps.clear()
        self.imageryChanged.emit()

    @property
    def visible_basemaps(self) -> list[Basemap]:
        return [b for b in self.basemaps.values() if b.visible]

    # --- projects --------------------------------------------------------

    def to_project(self, view: dict | None = None) -> Project:
        # Stamp the CRS so a plan lifted out of this project into another
        # one announces the frame its coordinates are in.
        self.plan.epsg = self.site.epsg
        return Project(
            site=self.site,
            sources=[str(p) for p in self.sources],
            active_layer=self.active_layer,
            chain=self.chain.to_list(),
            vertical=self.vertical.to_dict() if self.vertical else None,
            basemaps=[BasemapState(b.provider, b.visible, b.opacity)
                      for b in self.basemaps.values()],
            vectors=list(self.vectors),
            view={**(view or {}),
                  # Which sessions were being compared is part of the
                  # result when they build the surface, so it travels too.
                  "hidden_sessions": sorted(self.hidden_sessions),
                  "surface_from_shown_sessions": self.surface_from_shown},
            plan=(self.plan.to_dict()
                  if self.plan.points or self.plan.setups else None),
            imagery_offset=(self.imagery_offset.to_list()
                            if not self.imagery_offset.zero else None),
            source_fingerprints={str(p): self.fingerprints[str(p)]
                                 for p in self.sources
                                 if str(p) in self.fingerprints},
        )

    def save_project(self, path: str | Path, view: dict | None = None) -> Path:
        if view is not None:
            self.view = dict(view)
        saved = self.to_project(self.view).save(path)
        self.project_path = saved
        self.statusMessage.emit(f"Saved {saved}")
        return saved

    def load_project(self, path: str | Path, size: int = 1024) -> tuple[Project, list[str]]:
        """Restore a session. Returns the project and any non-fatal warnings.

        Order matters: the site and the source data first, then the filter
        chain, then the vertical model. Applying the stored vertical model
        rather than re-solving it is deliberate - it reproduces the exact
        elevations the project was saved with, instead of numbers that merely
        resemble them because the solver saw slightly different inputs.
        """
        project = Project.load(path)
        warnings: list[str] = []

        self.site = project.site
        self.siteChanged.emit()
        self.vertical = None
        self.basemaps.clear()
        self.vectors.clear()
        self.imagery_offset = ImageryOffset.from_list(project.imagery_offset)
        # The plan is restored BEFORE the data, so `refresh_plan_layer`
        # below sees it and the level network solves with the rod readings
        # in place rather than a beat later.
        self.plan = Plan.from_dict(project.plan) if project.plan else Plan()

        # A project may reference several exports. Loading each one with
        # replace semantics would silently keep only the last, which is what
        # used to happen: merge everything after the first.
        missing = project.missing_sources()
        self.sources = []
        for src in project.sources:
            if src in missing:
                warnings.append(f"source not found: {src}")
                continue
            try:
                loaded = self.load(src, merge=bool(self.sources))
                # The model is cleared above, so the only notes a load can
                # make here are about the export itself (Instrument Ht).
                warnings += loaded.notes
            except Exception as exc:                      # noqa: BLE001
                warnings.append(f"could not load {Path(src).name}: {exc}"[:160])
                continue
            saved = project.source_fingerprints.get(src)
            now = self.fingerprints.get(str(Path(src)))
            if saved and now and saved.get("sha256") != now["sha256"]:
                warnings.append(
                    f"{Path(src).name} has changed since the project was "
                    "saved. The stored session offsets and datum were solved "
                    "from its old contents and are applied to the new ones "
                    "as they are; re-solve from the Datum menu.")

        self.hidden_sessions = set(map(str, project.view.get("hidden_sessions", [])))
        self.surface_from_shown = bool(project.view.get("surface_from_shown_sessions", False))

        if project.active_layer and project.active_layer in self.layers:
            self.active_layer = project.active_layer

        if project.chain:
            self.chain = FilterChain.from_list(project.chain)

        if project.vertical:
            self.vertical = VerticalModel.from_dict(project.vertical)
            # It was solved with the plan it was saved with. Counting it as
            # such keeps the first edit after opening from re-solving it away.
            self._solved_plan = self._plan_signature()

        self.recompute()

        # Rasters and linework need the data extent, so they come last.
        if self.source is not None:
            warnings += self._restore_basemaps(project, size)
            warnings += self._restore_vectors(project)

        self.project_path = Path(path)
        self.view = dict(project.view)
        self.viewChanged.emit()
        self.sessionsChanged.emit()
        self.refresh_plan_layer()
        self.layersChanged.emit()
        self.verticalChanged.emit()
        self.imageryChanged.emit()
        self.statusMessage.emit(f"Opened {path}")
        return project, warnings

    # --- imagery alignment ----------------------------------------------

    def set_imagery_offset(self, offset: ImageryOffset) -> None:
        self.imagery_offset = offset
        self.imageryChanged.emit()

    def solve_imagery_offset(self, numbers=None) -> ImageryOffset:
        """Fit the shift from plan points, and apply it."""
        offset = solve_imagery_offset(self.plan, numbers)
        self.set_imagery_offset(offset)
        return offset

    def clear_imagery_offset(self) -> None:
        self.set_imagery_offset(ImageryOffset())

    def _restore_basemaps(self, project: Project, size: int) -> list[str]:
        warnings: list[str] = []
        known = self.all_providers()
        for entry in project.basemaps:
            provider_pair = known.get(entry.provider)
            if provider_pair is None:
                warnings.append(
                    f"basemap '{entry.provider}' is not a known provider "
                    "(a local file, or a provider that has since been renamed)")
                continue
            provider, terrain = provider_pair
            try:
                self.fetch_imagery(provider, size, terrain=terrain,
                                   visible=entry.visible)
                self.basemaps[entry.provider].opacity = entry.opacity
            except NoCoverageError:
                warnings.append(f"basemap '{entry.provider}': no coverage here")
            except Exception as exc:                      # noqa: BLE001
                warnings.append(f"basemap '{entry.provider}': {exc}"[:110])
        return warnings

    def _restore_vectors(self, project: Project) -> list[str]:
        warnings: list[str] = []
        providers = default_vector_providers()
        for name in project.vectors:
            provider = providers.get(name)
            if provider is None:
                warnings.append(f"linework provider '{name}' is unknown")
                continue
            try:
                self.fetch_vectors(provider)
            except Exception as exc:                      # noqa: BLE001
                warnings.append(f"linework '{name}': {exc}"[:110])
        return warnings

    def fetch_vectors(self, provider) -> VectorLayer:
        ext = self.data_extent(margin=40.0)
        if ext is None:
            raise ValueError("load data first so the extent is known")
        layer = provider.fetch(ext, self.site.epsg)
        self.vectors[provider.name] = layer
        self.imageryChanged.emit()
        return layer

    def clear_vectors(self) -> None:
        self.vectors.clear()
        self.imageryChanged.emit()

    # --- operations the desktop window used to own -----------------------

    def _plan_signature(self) -> str:
        """What the plan contributes to the level network, as a string.

        Only shots with a rod reading reach the network, at their resolved
        positions. Placing, moving or relabelling a shot that has no reading
        changes none of that.
        """
        frame = self.plan.to_frame()
        return "" if frame.empty else frame.to_json()

    def plan_changed(self) -> None:
        """The plan was edited: republish it, and re-solve a solved datum.

        Rod readings feed the level network, so an edit to the plan can move
        the datum - but only an edit to what the network sees. Re-solving for
        every click would stall the map for a second or more on a merged
        data set, for no change in any number.

        A failed re-solve is reported, not raised: the edit itself succeeded,
        and refusing it because the network is momentarily unsolvable (a
        benchmark shot not yet typed in) would be backwards.
        """
        self.refresh_plan_layer()
        if (self.vertical is not None
                and self._plan_signature() != self._solved_plan):
            try:
                self.solve_vertical(self.vertical.mode)
            except Exception as exc:                        # noqa: BLE001
                self.statusMessage.emit(f"Could not re-solve: {exc}")

    def open_plan(self, path: str | Path) -> Plan:
        plan = Plan.load(path)
        if plan.epsg and plan.epsg != self.site.epsg:
            raise ValueError(
                f"That plan was made in EPSG:{plan.epsg} but this site is "
                f"EPSG:{self.site.epsg}. The positions would be wrong.")
        self.plan = plan
        self.last_fill = self.fill_plan_from_records()
        self.plan_changed()
        self.statusMessage.emit(f"Opened {Path(path).name}")
        return plan

    def save_plan(self, path: str | Path) -> Path:
        self.plan.epsg = self.site.epsg
        saved = self.plan.save(path)
        self.statusMessage.emit(f"Saved {saved.name}")
        return saved

    def spot_ids(self) -> list:
        """Stations a benchmark can be chosen from: ints first, then names."""
        from ..model.pointset import ROD_IN
        from ..vertical import sort_stations, stations

        spots = self.spots
        if spots is None or ROD_IN not in spots.df.columns:
            return []
        shot = spots.df[ROD_IN].notna()
        return sort_stations(stations(spots, self.site.mark_names)[shot])

    def spot_choices(self) -> list[dict]:
        """Every station a benchmark can be, with what it is: kind, rod
        reading(s) and date, for the datum dialog's picker."""
        import pandas as pd

        from ..model.pointset import KIND, ROD_IN, TIME
        from ..vertical import stations

        spots = self.spots
        if spots is None or ROD_IN not in spots.df.columns:
            return []
        d = spots.df[spots.df[ROD_IN].notna()]
        keys = stations(spots, self.site.mark_names)[d.index]
        found: dict = {}
        for i in d.index:
            entry = found.setdefault(keys.at[i], {"kinds": [], "rods": [], "dates": []})
            if KIND in d.columns and isinstance(d[KIND].at[i], str):
                entry["kinds"].append(d[KIND].at[i])
            entry["rods"].append(float(d[ROD_IN].at[i]))
            if TIME in d.columns and pd.notna(d[TIME].at[i]):
                entry["dates"].append(f"{d[TIME].at[i]:%Y-%m-%d}")
        out = []
        for key in self.spot_ids():
            entry = found[key]
            rods = entry["rods"]
            bits = [entry["kinds"][0] if entry["kinds"] else "no kind",
                    f"rod {rods[0]:g} in" if len(rods) == 1
                    else f"{len(rods)} readings"]
            if entry["dates"]:
                bits.append(min(entry["dates"]))
            out.append({"point": key, "label": " · ".join(bits)})
        return out

    def set_datum_tie(self, *, point, elev_ft: float, note: str = "",
                      frame: str = "", tied: bool = False
                      ) -> VerticalModel | None:
        """Change which shot the survey hangs from, and at what elevation.

        A solved model is re-solved rather than left in place: the benchmark
        changed, so a surface that still claimed the old datum would be
        wrong. Returns the new model when there was one to re-solve.
        """
        from ..vertical import describe_stations, station_key
        from .datum import apply_tie

        # Any name is a valid station, so a typo would otherwise be stored
        # without complaint and only fail at the next solve.
        shot = self.spot_ids()
        key = station_key(point)
        if key is not None and shot and key not in shot:
            raise ValueError(
                f"'{point}' is not a point id: no rod shot was read on it "
                f"(shot: {describe_stations(shot)}).")
        apply_tie(self.site, point=point, elev_ft=elev_ft, note=note,
                  frame=frame, tied=tied)
        self.siteChanged.emit()
        self.statusMessage.emit(self.site.vertical.describe())
        if self.vertical is not None:
            return self.solve_vertical(self.vertical.mode)
        self.verticalChanged.emit()
        return None

    def set_manual_imagery_offset(self, de_ft: float, dn_ft: float) -> None:
        """A hand-typed shift.

        It has no points behind it, so it is stored with no statistics rather
        than inheriting the ones from a previous solve.
        """
        from ..units import ft_to_m

        self.set_imagery_offset(ImageryOffset(de=ft_to_m(de_ft),
                                              dn=ft_to_m(dn_ft)))
