"""The HTTP server behind the browser UI.

A thin layer. Every rule about what an action means lives in `gpsrtk.app`;
this module turns actions into HTTP endpoints, the state into JSON, and
rasters and point clouds into compact binary. The browser is a view.

Shape of the conversation:

  * `GET /api/state` returns a snapshot small enough to send after every
    action: layers, the filter stack with its counts, the plan, the QC text,
    and a revision counter per topic. Every action replies with the same
    snapshot, plus a notice to show when there is something to say.
  * Heavy things - the point cloud, the surface, basemaps - are separate
    resources. The browser fetches them again only when the revision that
    covers them has moved.
  * One lock serialises everything that touches the state, as the desktop
    app's single UI thread did. A long action (fetching every basemap) holds
    it; `GET /api/progress` does not take it, so the browser can say what is
    happening while it waits.

It listens on 127.0.0.1 only. Survey coordinates pin a property to the
centimetre, so they are not served to the network - and a server on localhost
is still reachable from any web page open in the same browser, so two more
checks keep other sites out:

  * the Host header must be a loopback name, so a page cannot reach this
    server by pointing a hostname of its own at 127.0.0.1;
  * every action must carry an `X-Yard-Survey` header and, when the browser
    says where it came from, a loopback Origin. A custom header cannot be
    sent cross-site without a CORS preflight, which this server never grants,
    so another site cannot make it load, save or overwrite files.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import tempfile
import threading
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..app import AppState
from ..app import chain_edit as CE
from ..app import plan_edit as PE
from ..app import report, views
from ..app.datum import control_form, tie_form
from ..app.sessions import sessions_payload
from ..io.imagery import NoCoverageError
from ..io.vector import default_vector_providers
from ..project import SUFFIX as PROJECT_SUFFIX
from ..model.pointset import E, N
from ..units import m_to_ft
from ..vertical import height_label
from . import files

STATIC = Path(__file__).parent / "static"
LOOPBACK = ["127.0.0.1", "localhost"]
ACTION_HEADER = "x-yard-survey"
DEFAULT_PORT = 8765
MAX_DOWNLOADS = 20

EXPORT_TYPES = [".zip", ".swmz", ".swm2", ".csv"]
RASTER_TYPES = [".tif", ".tiff", ".png", ".jpg"]


class UserError(Exception):
    """Something to tell the user, as a dialog, rather than a crash."""

    def __init__(self, title: str, text: str, level: str = "error"):
        super().__init__(text)
        self.title, self.text, self.level = title, text, level


def _clean(obj):
    """JSON-safe copy: NaN and infinity become null, numpy scalars plain."""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, bool) or obj is None or isinstance(obj, str):
        return obj
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        return float(obj) if math.isfinite(obj) else None
    if hasattr(obj, "item"):                                  # numpy scalar
        return _clean(obj.item())
    if isinstance(obj, Path):
        return str(obj)
    return obj


def _json(obj, status: int = 200) -> Response:
    return Response(json.dumps(_clean(obj), ensure_ascii=False),
                    status_code=status, media_type="application/json")


class Server:
    """The application state plus what the web layer needs around it."""

    def __init__(self, state: AppState, *, vector_providers=None):
        self.state = state
        self.lock = threading.RLock()
        self.busy: str | None = None
        self.vector_providers = (vector_providers if vector_providers is not None
                                 else default_vector_providers())
        self.downloads: dict[str, tuple[str, bytes, str, bool]] = {}
        self.last_dir: str | None = None
        self._qc: tuple[tuple, str] = ((), "")
        self._sessions: tuple[tuple, dict] = ((), {})
        self._derived_key: tuple = ()
        self._derived: dict = {}
        self._scales = views.legend_scales()

    # --- plumbing --------------------------------------------------------

    @contextmanager
    def acting(self, message: str | None = None):
        """Hold the state for the duration of a request.

        The busy message is restored in `finally`, so an action that fails
        part-way cannot leave the browser showing a spinner forever - the
        failure this replaced was an hourglass cursor that never went away.
        """
        with self.lock:
            previous = self.busy
            if message:
                self.busy = message
            try:
                yield
            finally:
                self.busy = previous

    def progress(self, message: str) -> None:
        self.busy = message

    @contextmanager
    def guard(self, title: str, level: str = "error"):
        """Turn an unexpected exception into a dialog with this title."""
        try:
            yield
        except (UserError, PE.PlanEditError, PE.NeedsConfirmation):
            raise
        except Exception as exc:                            # noqa: BLE001
            raise UserError(title, str(exc) or type(exc).__name__,
                            level) from exc

    def require_data(self) -> None:
        if self.state.result is None:
            raise UserError("No data", "Load an export first.", "info")

    def offer(self, filename: str, data: bytes, media_type: str,
              inline: bool = False) -> dict:
        token = uuid.uuid4().hex
        self.downloads[token] = (filename, data, media_type, inline)
        while len(self.downloads) > MAX_DOWNLOADS:
            self.downloads.pop(next(iter(self.downloads)))
        return {"url": f"/api/downloads/{token}", "filename": filename,
                "inline": inline}

    def remember_dir(self, path: str | Path) -> None:
        p = Path(path)
        self.last_dir = str(p if p.is_dir() else p.parent)

    # --- the snapshot ----------------------------------------------------

    def derived(self, name: str, params: tuple, compute):
        """Something computed from the current surface, kept until it changes.

        Slope, contours and drainage arrows are each asked for by several
        requests (the image, the arrows, the legend) and cost more than the
        snapshot they ride on, so each is worked out once per surface.
        """
        st = self.state
        key = (id(st.surface), st.revisions["result"], st.revisions["site"])
        if self._derived_key != key:
            self._derived_key, self._derived = key, {}
        if (name, params) not in self._derived:
            self._derived[(name, params)] = compute()
        return self._derived[(name, params)]

    def slope_field(self):
        from .. import terrain

        return self.derived("slope", (), lambda: terrain.slope(self.state.surface))

    def sessions(self) -> dict:
        """Per-session QC. Crossovers again, so cached like the QC text."""
        st = self.state
        rev = st.revisions
        key = tuple(rev[t] for t in ("result", "site", "sessions", "vertical",
                                     "layers")) + (id(st.corrected),)
        if self._sessions[0] != key:
            self._sessions = (key, sessions_payload(st))
        return self._sessions[1]

    def qc_text(self) -> str:
        """Crossover QC is a neighbour search over every point, so it is
        recomputed only when the result or the site actually changed."""
        rev = self.state.revisions
        key = (rev["result"], rev["site"], id(self.state.result))
        if self._qc[0] != key:
            slope = self.slope_field() if self.state.surface is not None else None
            self._qc = (key, report.qc_text(self.state, slope))
        return self._qc[1]

    def snapshot(self) -> dict:
        st = self.state
        site = st.site
        off = st.imagery_offset
        surface = None
        if st.surface is not None:
            surface = {"extent": views.local_extent(site, st.surface.extent),
                       "measured": st.surface.measured_fraction}
        home = st.data_extent(margin=8.0)
        suggested = None
        if st.project_path is not None:
            suggested = str(st.project_path)
        elif st.source_path is not None:
            suggested = str(Path(st.source_path).with_suffix(PROJECT_SUFFIX))

        return {
            "rev": dict(st.revisions),
            "title": st.title,
            "status": st.status,
            "has_data": st.result is not None,
            "site": {"name": site.name, "epsg": site.epsg,
                     "origin": [site.origin_e, site.origin_n],
                     "datum": site.vertical.describe(),
                     "tied": site.vertical.tied_to_model},
            "sources": [str(p) for p in st.sources],
            "project_path": str(st.project_path) if st.project_path else None,
            "suggested_project": suggested,
            "layers": [{"name": name, "describe": ps.describe(),
                        "visible": st.visible.get(name, True),
                        "active": name == st.active_layer}
                       for name, ps in st.layers.items()],
            "chain": CE.chain_payload(st.chain),
            "basemaps": views.basemap_payload(st),
            "basemaps_empty": report.EMPTY_BASEMAPS,
            "imagery_offset": {
                "de_ft": m_to_ft(off.de), "dn_ft": m_to_ft(off.dn),
                "zero": off.zero,
                "describe": ("Imagery drawn where it says it is." if off.zero
                             else off.describe())},
            "vectors": [{"name": name, "describe": layer.describe()}
                        for name, layer in st.vectors.items()],
            "vertical": {"solved": st.vertical is not None,
                         "mode": st.vertical.mode if st.vertical else None,
                         "text": report.vertical_text(st)},
            "qc": self.qc_text(),
            "info": report.info_bits(st),
            "points_note": views.points_note(st),
            "sessions": self.sessions(),
            "terrain": (views.terrain_payload(st, self.slope_field())
                        if st.surface is not None else None),
            "scales": self._scales,
            "surface": surface,
            "home": (views.local_extent(site, home) if home is not None
                     else None),
            "plan": PE.plan_payload(st.plan, site),
            "view": st.view,
            "providers": self.providers(),
        }

    def providers(self) -> dict:
        photo, terrain = [], []
        for name, (p, is_terrain) in self.state.all_providers().items():
            (terrain if is_terrain else photo).append(
                {"name": name, "description": getattr(p, "description", "")})
        return {"photo": photo, "terrain": terrain,
                "vector": [{"name": n, "description": p.description}
                           for n, p in self.vector_providers.items()]}

    def reply(self, notice: dict | None = None, **extra) -> Response:
        return _json({"state": self.snapshot(), "notice": notice, **extra})


def notice(title: str, text: str, level: str = "info", **extra) -> dict:
    return {"title": title, "text": text, "level": level, **extra}


# --- the application ------------------------------------------------------------

def create_app(state: AppState | None = None, *, vector_providers=None,
               allowed_hosts: list[str] | None = None) -> FastAPI:
    srv = Server(state or AppState(), vector_providers=vector_providers)
    app = FastAPI(title="Yard Survey", docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.server = srv
    app.add_middleware(TrustedHostMiddleware,
                       allowed_hosts=allowed_hosts or LOOPBACK)

    hosts = set(allowed_hosts or LOOPBACK)

    @app.middleware("http")
    async def same_origin_actions(request: Request, call_next):
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            foreign = origin and urlparse(origin).hostname not in hosts
            if foreign or request.headers.get(ACTION_HEADER) != "1":
                return _json({"error": notice(
                    "Refused", "Actions are only accepted from this "
                    "application's own page.")}, 403)
        return await call_next(request)

    @app.middleware("http")
    async def revalidate(request: Request, call_next):
        # Always revalidate the page and its scripts, so an upgraded install
        # never runs yesterday's JavaScript against today's server.
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(UserError)
    async def _user_error(request, exc: UserError):
        return _json({"error": notice(exc.title, exc.text, exc.level)}, 400)

    @app.exception_handler(PE.PlanEditError)
    async def _plan_error(request, exc: PE.PlanEditError):
        return _json({"error": notice(exc.title, exc.message, "warning")}, 400)

    @app.exception_handler(PE.NeedsConfirmation)
    async def _confirm(request, exc: PE.NeedsConfirmation):
        return _json({"confirm": {"title": exc.title, "text": exc.message}})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    st = srv.state

    # --- reading ---------------------------------------------------------

    @app.get("/api/state")
    def get_state():
        with srv.acting():
            return _json(srv.snapshot())

    @app.get("/api/progress")
    def get_progress():
        # Deliberately lock-free: this is how the browser learns what a long
        # action is doing while that action holds the lock.
        return _json({"busy": srv.busy is not None, "message": srv.busy or "",
                      "rev": dict(st.revisions)})

    @app.get("/api/points")
    def get_points(color_by: str = "elevation", cmap: str = "terrain"):
        with srv.acting():
            data = views.pack_points(st, color_by, cmap)
        return Response(data, media_type="application/octet-stream")

    @app.get("/api/surface.png")
    def get_surface(cmap: str = "terrain", mode: str = "elevation",
                    slope_max: float = 10.0):
        """The surface as an image: hillshaded elevation, or slope."""
        with srv.acting():
            if st.surface is None:
                return Response(status_code=404)
            if mode == "slope":
                data = srv.derived("slope.png", (slope_max,), lambda: views.slope_png(
                    st.surface, slope_max, srv.slope_field()))
            else:
                data = srv.derived("elevation.png", (cmap,),
                                   lambda: views.surface_png(st.surface, cmap))
            return Response(data, media_type="image/png")

    @app.get("/api/contours")
    def get_contours(interval_cm: float = 5.0):
        with srv.acting():
            if st.surface is None:
                return _json(None)
            if not 0.5 <= interval_cm <= 500:
                raise UserError("Contours", "The interval must be between "
                                "0.5 cm and 5 m.", "warning")
            return _json(srv.derived("contours", (interval_cm,), lambda:
                         views.contours_payload(st.surface, st.site,
                                                interval_cm / 100.0)))

    @app.get("/api/drainage")
    def get_drainage(spacing_m: float = 1.5):
        with srv.acting():
            if st.surface is None:
                return _json(None)
            spacing_m = min(max(spacing_m, 0.25), 20.0)
            return _json(srv.derived("drainage", (spacing_m,), lambda:
                         views.drainage_payload(st.surface, st.site, spacing_m,
                                                srv.slope_field())))

    @app.get("/api/surface/grid")
    def get_grid(cmap: str = "terrain"):
        with srv.acting():
            if st.surface is None:
                return _json(None)
            return _json(views.surface_grid(st.surface, st.site, cmap))

    @app.get("/api/basemap.png")
    def get_basemap(name: str):
        with srv.acting():
            bm = st.basemaps.get(name)
            if bm is None:
                return Response(status_code=404)
            return Response(views.basemap_png(bm), media_type="image/png")

    @app.get("/api/features")
    def get_features():
        with srv.acting():
            return _json({"markers": views.spot_markers(st),
                          "vectors": views.vector_lines(st)})

    @app.get("/api/downloads/{token}")
    def get_download(token: str):
        item = srv.downloads.get(token)
        if item is None:
            return Response("That download has expired; export again.",
                            status_code=404, media_type="text/plain")
        filename, data, media_type, inline = item
        disposition = "inline" if inline else "attachment"
        return Response(data, media_type=media_type, headers={
            "Content-Disposition": f'{disposition}; filename="{filename}"'})

    @app.get("/api/files")
    def get_files(dir: str = "", exts: str = ""):
        start = dir or srv.last_dir or os.getcwd()
        try:
            return _json(files.listing(start, [e for e in exts.split(",") if e]))
        except ValueError as exc:
            raise UserError("Browse", str(exc), "warning") from exc

    @app.get("/api/control")
    def get_control():
        with srv.acting():
            return _json(control_form(st.site))

    @app.post("/api/control")
    def set_control(body: dict = Body(...)):
        with srv.acting("Saving control marks…"):
            try:
                model = st.set_control_marks(body.get("marks") or [])
            except (ValueError, KeyError, TypeError) as exc:
                raise UserError("Control marks", str(exc), "warning") from exc
            if model is not None:
                return srv.reply(notice(f"Vertical model ({model.mode})",
                                        report.solve_notice(model),
                                        monospace=True))
            return srv.reply()

    @app.get("/api/datum")
    def get_datum():
        with srv.acting():
            return _json(tie_form(st.site, st.spot_ids(), st.spot_choices()))

    # --- File ------------------------------------------------------------

    @app.post("/api/quit")
    def quit_app():
        # `main` sets `app.state.stop`; a server run any other way (tests, an
        # embedding) has nothing to stop and says so.
        stop = getattr(app.state, "stop", None)
        if stop is not None:
            threading.Timer(0.3, stop).start()
        return _json({"stopping": stop is not None})

    @app.post("/api/export/open")
    def open_export(body: dict = Body(...)):
        path = _path(body)
        with srv.acting("Reading export…"), srv.guard("Could not open"):
            result = st.load(path)
        srv.remember_dir(path)
        parts = []
        if result.reprojected:
            parts.append("These layers were re-projected into the site's CRS "
                         f"(EPSG:{st.site.epsg}):\n\n  "
                         + "\n  ".join(result.reprojected))
        parts += result.notes
        if parts:
            return srv.reply(notice(
                "Coordinate system" if result.reprojected and not result.notes
                else "Export opened", "\n\n".join(parts)))
        return srv.reply()

    @app.post("/api/export/add")
    def add_export(body: dict = Body(...)):
        path = _path(body)
        with srv.acting("Merging export…"):
            if not st.layers:
                with srv.guard("Could not open"):
                    result = st.load(path)
                srv.remember_dir(path)
                if result.notes:
                    return srv.reply(notice("Export opened",
                                            "\n\n".join(result.notes)))
                return srv.reply()
            with srv.guard("Could not add"):
                result = st.add_export(path)
            srv.remember_dir(path)
            st.statusMessage.emit(
                f"Merged {Path(path).name}: {result.added:,} points added")
            return srv.reply(_merge_notice(result, "Export merged"))

    @app.post("/api/sessions/visible")
    def session_visible(body: dict = Body(...)):
        with srv.acting("Updating sessions…"):
            st.set_session_visible(str(body["name"]), bool(body["visible"]))
            return srv.reply()

    @app.post("/api/sessions/shown")
    def sessions_shown(body: dict = Body(...)):
        """Show exactly these sessions: "only this one", or "show all"."""
        with srv.acting("Updating sessions…"):
            names = body.get("names")
            st.set_sessions_shown(st.sessions if names is None else names)
            return srv.reply()

    @app.post("/api/sessions/surface")
    def sessions_surface(body: dict = Body(...)):
        with srv.acting("Rebuilding the surface…"):
            st.set_surface_from_shown(bool(body["on"]))
            return srv.reply()

    @app.post("/api/sessions")
    def sessions():
        with srv.acting("Measuring overlap…"):
            srv.require_data()
            result = st.session_report()
            return srv.reply(_merge_notice(result, "Sessions and overlap",
                                           offer_solve=False))

    @app.post("/api/project/open")
    def open_project(body: dict = Body(...)):
        path = _path(body)
        with srv.acting("Opening project…"):
            with srv.guard("Could not open project"):
                _, warnings = st.load_project(path)
            srv.remember_dir(path)
            if warnings:
                return srv.reply(notice(
                    "Project opened with warnings",
                    "The project loaded, but some parts could not be "
                    "restored:\n\n" + "\n".join(f"  {w}" for w in warnings)
                    + "\n\nThe filter chain and vertical model are "
                      "unaffected.", "warning"))
            return srv.reply()

    @app.post("/api/project/save")
    def save_project(body: dict = Body(default={})):
        with srv.acting("Saving project…"):
            if st.source is None:
                raise UserError("Nothing to save", "Load data first.", "info")
            path = body.get("path") or st.project_path
            if not path:
                raise UserError("Save project",
                                "Choose where to save the project.", "info")
            with srv.guard("Could not save"):
                st.save_project(path, body.get("view"))
            srv.remember_dir(path)
            return srv.reply()

    # --- layers and the filter stack -------------------------------------

    @app.post("/api/layers/visible")
    def set_visible(body: dict = Body(...)):
        with srv.acting():
            st.set_visible(str(body["name"]), bool(body["visible"]))
            return srv.reply()

    @app.post("/api/layers/active")
    def set_active(body: dict = Body(...)):
        with srv.acting("Rebuilding the surface…"):
            with srv.guard("Could not switch layer"):
                st.set_active_layer(str(body["name"]))
            return srv.reply()

    def chain_edit(edit, title: str):
        """Apply an edit to the stack; undo it if the stack will not run.

        A mistyped parameter used to leave the chain holding a value no stage
        can apply. Restoring the previous stack keeps the surface honest and
        the error in front of the user.
        """
        with srv.acting("Filtering…"):
            before = st.chain.to_list()
            try:
                edit(st.chain)
                st.recompute()
            except Exception as exc:                        # noqa: BLE001
                from ..filters import FilterChain

                st.chain = FilterChain.from_list(before)
                st.recompute()
                raise UserError(title, str(exc) or type(exc).__name__,
                                "warning") from exc
            return srv.reply()

    @app.post("/api/chain/add")
    def chain_add(body: dict = Body(...)):
        return chain_edit(lambda c: CE.add_stage(c, str(body["kind"])),
                          "Add stage")

    @app.post("/api/chain/remove")
    def chain_remove(body: dict = Body(...)):
        return chain_edit(lambda c: CE.remove_stage(c, int(body["index"])),
                          "Remove stage")

    @app.post("/api/chain/move")
    def chain_move(body: dict = Body(...)):
        return chain_edit(
            lambda c: CE.move_stage(c, int(body["index"]), int(body["delta"])),
            "Move stage")

    @app.post("/api/chain/edit")
    def chain_stage(body: dict = Body(...)):
        return chain_edit(
            lambda c: CE.edit_stage(c, int(body["index"]),
                                    enabled=body.get("enabled"),
                                    params=body.get("params")),
            "Filter parameter")

    # --- Data: imagery and reference linework ---------------------------

    @app.post("/api/imagery/fetch_all")
    def fetch_all():
        with srv.acting("Fetching basemaps…"):
            srv.require_data()
            with srv.guard("Fetch failed"):
                result = st.fetch_all_imagery(
                    progress=lambda n: srv.progress(f"Fetching {n}…"))
            st.statusMessage.emit(
                f"{len(result.fetched)} basemaps available - toggle them in "
                "the Basemaps panel")
            return srv.reply(notice("Basemaps", result.describe()))

    @app.post("/api/imagery/fetch")
    def fetch_one(body: dict = Body(...)):
        name = str(body["name"])
        with srv.acting(f"Checking {name}…"):
            srv.require_data()
            known = st.all_providers()
            if name not in known:
                raise UserError("Imagery", f"'{name}' is not a known source.")
            provider, terrain = known[name]
            ok, msg = provider.available()
            if not ok:
                raise UserError(
                    "Imagery unavailable",
                    f"{name} is not reachable:\n\n{msg}\n\nPublic GIS services "
                    "go down; try another provider, or load a georeferenced "
                    "file instead.", "warning")
            srv.progress(f"Fetching imagery from {name}…")
            try:
                layer = st.fetch_imagery(provider, terrain=terrain)
            except NoCoverageError as exc:
                raise UserError("No coverage here", str(exc), "warning") from exc
            except Exception as exc:                        # noqa: BLE001
                raise UserError("Imagery failed", str(exc)) from exc
            st.statusMessage.emit(layer.describe())
            return srv.reply()

    @app.post("/api/imagery/load_file")
    def load_imagery_file(body: dict = Body(...)):
        path = _path(body)
        with srv.acting("Warping imagery…"):
            srv.require_data()
            with srv.guard("Could not load imagery"):
                layer = st.load_imagery_file(path)
            srv.remember_dir(path)
            st.statusMessage.emit(layer.describe())
            return srv.reply()

    @app.post("/api/imagery/clear")
    def clear_imagery():
        with srv.acting():
            st.clear_imagery()
            return srv.reply()

    @app.post("/api/basemap")
    def set_basemap(body: dict = Body(...)):
        name = str(body["name"])
        with srv.acting():
            if "visible" in body:
                st.set_basemap_visible(name, bool(body["visible"]))
            if "opacity" in body:
                st.set_basemap_opacity(name, float(body["opacity"]))
            return srv.reply()

    @app.post("/api/imagery/offset")
    def set_offset(body: dict = Body(...)):
        with srv.acting():
            with srv.guard("Alignment", "warning"):
                st.set_manual_imagery_offset(float(body["de_ft"]),
                                             float(body["dn_ft"]))
            return srv.reply()

    @app.post("/api/imagery/offset/solve")
    def solve_offset(body: dict = Body(default={})):
        from ..georef import residual_report

        with srv.acting():
            numbers = [int(n) for n in body.get("numbers") or []] or None
            try:
                offset = st.solve_imagery_offset(numbers)
            except ValueError as exc:
                raise UserError("Align imagery", str(exc), "info") from exc
            rows = residual_report(offset)
            st.statusMessage.emit(offset.describe())
            return srv.reply(notice(
                "Align imagery",
                offset.describe()
                + ("\n\nPoints used, worst residual first:\n  "
                   + "\n  ".join(rows) if len(rows) > 1 else "")
                + "\n\nThe imagery moved; the survey points did not."))

    @app.post("/api/imagery/offset/clear")
    def clear_offset():
        with srv.acting():
            st.clear_imagery_offset()
            return srv.reply()

    @app.post("/api/vectors/fetch")
    def fetch_vectors(body: dict = Body(...)):
        name = str(body["name"])
        with srv.acting(f"Fetching {name}…"):
            srv.require_data()
            provider = srv.vector_providers.get(name)
            if provider is None:
                raise UserError("Reference linework",
                                f"'{name}' is not a known source.")
            with srv.guard("Could not fetch linework"):
                layer = st.fetch_vectors(provider)
            st.statusMessage.emit(report.linework_note(layer))
            return srv.reply()

    @app.post("/api/vectors/clear")
    def clear_vectors():
        with srv.acting():
            st.clear_vectors()
            return srv.reply()

    @app.post("/api/services/check")
    def check_services():
        with srv.acting("Checking services…"):
            imagery = {n: p for n, (p, _) in st.all_providers().items()}
            text = report.services_report(imagery, srv.vector_providers)
            st.statusMessage.emit("Service check complete")
            return srv.reply(notice("Service availability", text,
                                    monospace=True))

    # --- Datum -----------------------------------------------------------

    @app.post("/api/datum")
    def set_datum(body: dict = Body(...)):
        with srv.acting("Tying the datum…"):
            try:
                model = st.set_datum_tie(
                    point=body.get("point"), elev_ft=body.get("elev_ft"),
                    note=body.get("note", ""), frame=body.get("frame", ""),
                    tied=bool(body.get("tied")))
            except ValueError as exc:
                raise UserError("Datum tie", str(exc), "warning") from exc
            if model is not None:
                return srv.reply(notice(f"Vertical model ({model.mode})",
                                        report.solve_notice(model),
                                        monospace=True))
            return srv.reply()

    @app.post("/api/vertical/solve")
    def solve_vertical(body: dict = Body(default={})):
        mode = str(body.get("mode", "local"))
        if mode not in ("local", "navd88", "ellipsoidal"):
            raise UserError("Vertical model", f"'{mode}' is not a mode.")
        with srv.acting(f"Solving vertical model ({mode})…"):
            srv.require_data()
            with srv.guard("Could not solve"):
                model = st.solve_vertical(mode)
            st.statusMessage.emit("Vertical model solved")
            return srv.reply(notice(f"Vertical model ({mode})",
                                    report.solve_notice(model),
                                    monospace=True))

    @app.post("/api/vertical/clear")
    def clear_vertical():
        with srv.acting():
            st.clear_vertical()
            st.statusMessage.emit(
                "Vertical model cleared; elevations are raw ellipsoidal "
                "height.")
            return srv.reply()

    # --- Export ----------------------------------------------------------

    def figure_surface():
        srv.require_data()
        surface = st.figure_surface()
        if surface is None:
            raise UserError("Export failed", "Too few points to grid.", "warning")
        return surface

    @app.post("/api/export/slope_map")
    def export_slope_map(body: dict = Body(default={})):
        from ..io.figures import slope_map

        with srv.acting("Drawing the slope map…"):
            slope_max = float(body.get("slope_max", 10.0))
            spacing = float(body.get("spacing_m", 1.5))
            with srv.guard("Export failed"):
                data = slope_map(figure_surface(), st.site,
                                 note=report.figure_note(st),
                                 slope_max_pct=slope_max, arrow_spacing_m=spacing)
            download = srv.offer("slope_map.png", data, "image/png")
            return srv.reply(download=download)

    @app.post("/api/export/contour_map")
    def export_contour_map(body: dict = Body(default={})):
        from ..io.figures import heightmap

        with srv.acting("Drawing the contour map…"):
            interval_cm = float(body.get("interval_cm", 5.0))
            if not 0.5 <= interval_cm <= 500:
                raise UserError("Contours", "The interval must be between "
                                "0.5 cm and 5 m.", "warning")
            with srv.guard("Export failed"):
                data = heightmap(figure_surface(), st.site,
                                 note=report.figure_note(st),
                                 interval_m=interval_cm / 100.0)
            download = srv.offer(f"contours_{interval_cm:g}cm.png", data, "image/png")
            return srv.reply(download=download)

    @app.post("/api/export/heightmap")
    def export_heightmap():
        from ..io.raster import write_heightmap

        with srv.acting("Building full-resolution surface…"):
            srv.require_data()
            with srv.guard("Export failed"):
                surface = st.export_surface()
                if surface is None:
                    raise UserError("Export failed", "Too few points to grid.",
                                    "warning")
                tag = report.filter_tag(st.chain)
                datum = height_label(st.vertical, st.site, surface.z_column)
                with tempfile.TemporaryDirectory() as tmp:
                    result = write_heightmap(surface, tmp, tag=f"_{tag}",
                                             datum=datum)
                    data = _zip_dir(tmp)
            st.statusMessage.emit(f"Wrote {result['paths']['png16'].name}")
            download = srv.offer(f"heightmap_{tag}.zip", data, "application/zip")
            laser = st.laser_points()
            return srv.reply(notice("Heightmap written",
                                    report.heightmap_notice(
                                        surface, datum,
                                        0 if laser is None else len(laser))),
                             download=download)

    @app.post("/api/export/revit")
    def export_revit():
        from ..filters import BinToCell
        from ..io.revit import append_laser_points, write_points

        with srv.acting("Writing Revit points…"):
            srv.require_data()
            with srv.guard("Export failed"):
                binned = BinToCell(cell=st.site.surface.bin_cell_m).for_site(
                    st.site).apply(st.result)
                laser = st.laser_points()
                dropped = 0
                if laser is not None:
                    binned, dropped = append_laser_points(binned, laser)
                with tempfile.TemporaryDirectory() as tmp:
                    result = write_points(binned, st.site,
                                          Path(tmp) / "revit_points_ft.csv",
                                          vertical=st.vertical)
                    data = _zip_dir(tmp)
                result["laser"] = 0 if laser is None else len(laser)
                result["dropped"] = dropped
            text, raw = report.revit_notice(result, st.site, st.vertical)
            download = srv.offer("revit_points_ft.zip", data, "application/zip")
            return srv.reply(notice("Revit points written", text,
                                    "warning" if raw else "info"),
                             download=download)

    # --- the shot plan ---------------------------------------------------

    def plan_edit(action, message: str | None = None):
        with srv.acting(message):
            result = action()
            st.plan_changed()
            return result

    def projected(body) -> tuple[float, float]:
        return st.site.to_projected(float(body["x"]), float(body["y"]))

    @app.post("/api/plan/point/add")
    def plan_add_point(body: dict = Body(...)):
        with srv.acting():
            point = plan_edit(lambda: PE.add_point(st.plan, *projected(body)))
            return srv.reply(selected=point.number)

    @app.post("/api/plan/point/move")
    def plan_move_point(body: dict = Body(...)):
        with srv.acting():
            number = int(body["number"])
            moved = plan_edit(
                lambda: PE.move_point(st.plan, number, *projected(body)))
            return srv.reply(selected=number, moved=moved)

    @app.post("/api/plan/setup/add")
    def plan_add_setup(body: dict = Body(...)):
        with srv.acting():
            plan_edit(lambda: PE.add_setup(st.plan, *projected(body)))
            return srv.reply()

    @app.post("/api/plan/line/add")
    def plan_add_line(body: dict = Body(...)):
        with srv.acting():
            vertices = [st.site.to_projected(float(x), float(y))
                        for x, y in body.get("vertices", [])]
            line = plan_edit(lambda: PE.add_line(st.plan, vertices))
            return srv.reply(line=line.line_id if line else None)

    @app.post("/api/plan/transects")
    def plan_transects():
        from ..model.pointset import SESSION as S

        with srv.acting("Placing tie transects…"):
            srv.require_data()
            ps = st.filtered if st.filtered is not None else st.source
            d = ps.df
            text = plan_edit(lambda: PE.add_tie_transects(
                st.plan, d[E].to_numpy(), d[N].to_numpy(),
                d[S].astype(str).to_numpy() if S in d.columns else None))
            return srv.reply(notice("Tie transects", text))

    @app.post("/api/plan/cell")
    def plan_cell(body: dict = Body(...)):
        with srv.acting():
            plan_edit(lambda: PE.edit_cell(st.plan, int(body["number"]),
                                           str(body["field"]),
                                           body.get("value"),
                                           confirm=bool(body.get("confirm"))))
            return srv.reply()

    @app.post("/api/plan/coords")
    def plan_coords(body: dict = Body(...)):
        with srv.acting():
            text = plan_edit(lambda: PE.set_measured_position(
                st.plan, st.site, int(body["number"]), str(body["frame"]),
                body.get("x", ""), body.get("y", ""),
                confirm=bool(body.get("confirm"))))
            return srv.reply(result=text)

    @app.post("/api/plan/coords/clear")
    def plan_clear_coords(body: dict = Body(...)):
        with srv.acting():
            text = plan_edit(lambda: PE.clear_measured_position(
                st.plan, int(body["number"])))
            return srv.reply(result=text)

    @app.post("/api/plan/tape")
    def plan_tape(body: dict = Body(...)):
        with srv.acting():
            text = plan_edit(lambda: PE.apply_tape(
                st.plan, int(body["number"]), body.get("tie_a"),
                body.get("dist_a_ft", 0), body.get("tie_b"),
                body.get("dist_b_ft", 0)))
            return srv.reply(result=text)

    @app.post("/api/plan/offset")
    def plan_offset(body: dict = Body(...)):
        with srv.acting():
            text = plan_edit(lambda: PE.apply_station_offset(
                st.plan, int(body["number"]), str(body.get("line", "")),
                body.get("station_ft", 0), body.get("offset_ft", 0)))
            return srv.reply(result=text)

    @app.post("/api/plan/delete")
    def plan_delete(body: dict = Body(...)):
        with srv.acting():
            gone = plan_edit(lambda: PE.delete_points(
                st.plan, body.get("numbers", []),
                confirm=bool(body.get("confirm"))))
            return srv.reply(deleted=gone)

    @app.post("/api/plan/insert")
    def plan_insert(body: dict = Body(...)):
        with srv.acting():
            new = plan_edit(lambda: PE.insert_vertex(st.plan,
                                                     int(body["number"])))
            return srv.reply(selected=new.number)

    @app.post("/api/plan/open")
    def plan_open(body: dict = Body(...)):
        path = _path(body)
        with srv.acting("Opening plan…"):
            try:
                st.open_plan(path)
            except ValueError as exc:
                raise UserError("Could not open plan", str(exc),
                                "warning") from exc
            except Exception as exc:                        # noqa: BLE001
                raise UserError("Could not open plan", str(exc)) from exc
            srv.remember_dir(path)
            fill = st.last_fill
            if fill.filled or fill.conflicts:
                return srv.reply(notice(
                    "Plan opened", fill.describe(),
                    "warning" if fill.conflicts else "info", monospace=True))
            return srv.reply()

    @app.post("/api/plan/save")
    def plan_save(body: dict = Body(...)):
        path = _path(body)
        with srv.acting():
            with srv.guard("Could not save plan"):
                st.save_plan(path)
            srv.remember_dir(path)
            return srv.reply()

    @app.post("/api/plan/fieldsheet")
    def plan_fieldsheet():
        from ..io.fieldsheet import write_field_sheet

        with srv.acting("Rendering field sheet…"):
            if not st.plan.points:
                raise UserError("Field sheet", "Place some shots first.", "info")
            basemap = next(iter(st.visible_basemaps), None)
            with srv.guard("Field sheet"), tempfile.TemporaryDirectory() as tmp:
                out = write_field_sheet(
                    st.plan, Path(tmp) / "field_sheet.html", basemap=basemap,
                    site=st.site, title=f"{st.site.name} — shot plan",
                    marks=st.site.mark_names,
                    imagery_offset=(st.imagery_offset.de,
                                    st.imagery_offset.dn))
                data = out.read_bytes()
            download = srv.offer("field_sheet.html", data,
                                 "text/html; charset=utf-8", inline=True)
            return srv.reply(notice("Field sheet",
                                    report.field_sheet_notice(basemap is not None)),
                             download=download)

    return app


# --- helpers ---------------------------------------------------------------------------

def _path(body: dict) -> str:
    path = str(body.get("path") or "").strip()
    if not path:
        raise UserError("No file", "Choose a file first.", "info")
    return path


def _merge_notice(result, title: str, *, offer_solve: bool = True) -> dict:
    """Say what a merge did, and whether the result can be reconciled.

    A merge that cannot be reconciled is the one outcome worth a warning
    rather than a note: an unlinked session's offset is not recoverable
    later, so the only fix is to walk overlapping ground on the next outing,
    and that has to be known now.
    """
    text = result.describe() or "Nothing loaded."
    actions = []
    if offer_solve and result.reconcilable and len(result.sessions) > 1:
        text += ("\n\nThe sessions overlap, so their vertical offsets can be "
                 "solved. Until that is done the surface steps between them.")
        actions.append({"label": "Solve session offsets",
                        "post": "/api/vertical/solve",
                        "body": {"mode": "ellipsoidal"}})
    return notice(title, text,
                  "info" if result.reconcilable else "warning",
                  actions=actions, monospace=True)


def _zip_dir(folder: str | Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(Path(folder).iterdir()):
            if p.is_file():
                z.write(p, p.name)
    return buf.getvalue()


def _free_port(preferred: int) -> int:
    """The preferred port if it can be had, otherwise any free one.

    The probe sets SO_REUSEADDR where the server's own socket will (asyncio
    does on POSIX), so a port left in TIME_WAIT by the previous run is not
    mistaken for busy - which would move the app to a random port on every
    quick restart. Not on Windows, where the option would let the probe
    take a port another process is actually listening on.
    """
    import socket

    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if os.name != "nt":
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("no free port on 127.0.0.1")


def main(argv: list[str] | None = None) -> int:
    import sys
    import webbrowser

    import uvicorn

    parser = argparse.ArgumentParser(
        prog="yardsurvey",
        description="Terrain surfacing from RTK GNSS and laser survey data. "
                    "Starts a local server and opens it in the browser.")
    parser.add_argument("export", nargs="?", help="survey export to open")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"port on 127.0.0.1 (default {DEFAULT_PORT}; any "
                             "free port if that one is taken)")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open a browser window")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    state = AppState()
    if args.export:
        try:
            state.load(args.export)
        except Exception as exc:                            # noqa: BLE001
            print(f"Could not open {args.export}: {exc}", file=sys.stderr)

    app = create_app(state)
    port = _free_port(args.port)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    app.state.stop = lambda: setattr(server, "should_exit", True)
    url = f"http://127.0.0.1:{port}/"
    print(f"Yard Survey is running at {url}  (Ctrl+C or File ▸ Quit to stop)")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
