# Yard Survey

Georeferenced terrain surfacing from RTK GNSS and rotary laser survey data.
See `CLAUDE.md` for the site conventions, hardware, and established findings
that this code is built to preserve.

## Running

```bash
yardsurvey "archive\Project 1.zip"
```

This starts a small server on this machine and opens it in your browser, at
`http://127.0.0.1:8765/` (or another free port if that one is taken - the
console says which). Omit the path to start empty and use **File ▸ Open
export**. `--no-browser` starts the server without opening a tab, and
`--port` picks another port. **File ▸ Quit** or Ctrl+C in the console stops
it; closing the tab does not.

Typical session: open an export, **Data ▸ Fetch all basemaps**, tune the
filter stack, **Datum ▸ Solve local datum**, then **File ▸ Save project**.

## The browser UI

The window is the one the desktop app had, in a browser tab: the same menus
and shortcuts, the layers, sessions and the filter stack on the left, the plan
view and a 3D tab in the middle with the measured QC underneath, and the
basemaps, the shot plan and the vertical datum on the right. The docks and the
QC pane resize by dragging their edges. The plan view has two toolbar rows:
what to draw on top, and how to show the surface underneath - shaded by
elevation or by slope, with contours and drainage arrows.

A few things work differently because it is a web page:

- **Files are still local paths.** The file dialogs list folders on this
  machine, through the server. A project records its exports by path -
  relative to the project file when they sit under it - and an upload would
  only have handed over a copy. Nothing is uploaded anywhere.
- **Exports download.** **Export ▸ Heightmap raster** and **Export ▸ Revit
  points file** arrive as zips in the browser's download folder: the 16-bit
  heightmap with its mask, world file and preview; the points file with its
  `_ORIGIN.txt` sidecar. **Export ▸ Slope map** and **Export ▸ Contour map**
  arrive as PNGs. **Plan ▸ Print field sheet** opens the sheet in a new tab
  to print, still self-contained for use without a network.
- **Esc** puts a drawing tool down, and **Delete** in the shot-plan table
  deletes the selected rows. In the table, the first click on a row selects
  it and a click on a selected row edits the cell; Shift and Ctrl extend the
  selection.

The server only listens on `127.0.0.1`. A single point from a survey pins a
property to the centimetre, so the coordinates are never served to the
network, and the server also refuses requests that did not come from its own
page: a web page open in another tab cannot drive it to open or overwrite
files.

How it is put together: `gpsrtk.app` holds the application - the state, the
rules for editing a shot plan, the text of every report - as plain Python
that knows nothing about how it is displayed. `gpsrtk.web` exposes it over
HTTP and serves the page. The page has no build step: OpenLayers (plan view),
Plotly (3D) and Tabulator (the readings table) are committed under
`gpsrtk/web/static/vendor`, which says which versions and how to update them.
There is no `node_modules`, for the same OneDrive reason the virtual
environment lives elsewhere.

## Running the tests

    uv run pytest

About 315 tests run anywhere, most of them against a small synthetic export
(`tests/synthetic.py`) written to look like a SW Maps export of a mowed lot,
with a second outing over the same ground on a mount 5 cm higher.
Around 50 more need a real survey export and skip without one, because the
numbers they pin are measured facts about real ground rather than properties
of the code. Point `tests/conftest.py` at an export of your own to run them.

`tests/test_web_browser.py` drives the page in Chromium with Playwright. It
needs a browser Playwright can launch - `uv run playwright install chromium`
once - or `YARDSURVEY_CHROMIUM` pointing at an existing Chromium binary, and
skips with instructions otherwise. `-m browser` runs just those; `-m "not
browser"` leaves them out.

Network tests are deselected by default; `-m network` runs them.

## Configuring a site

Everything is relative to a *site*: a CRS, a local origin snapped to a 25 m
grid, and a vertical datum. There are no site constants in the source. A fresh
session opens on `site.local.json` in the folder it was started from if there
is one, and on a public example otherwise, and a project file carries its own
site regardless.

    from gpsrtk.site import Site
    Site.from_seed("my lot", 41.591087, -93.603278).save("site.local.json")

`site.local.json` is not version controlled, and neither is any project or plan
file: a single point from a survey pins a property to the centimetre.

## Importing

Two SW Maps formats are read, and the registry picks between them by content,
not by trusting the extension:

  **`.zip` / `.csv` — the CSV export.** Layers are detected by exclusion, since
  a layer file is named after a user-chosen layer rather than anything fixed.

  **`.swmz` / `.swm2` — SW Maps' own project archive.** A zip holding a SQLite
  database and, when raw logging was on, the receiver's raw stream. Prefer it:
  it carries per-point age of differential, the reference station id and the
  baseline length to it, none of which the CSV export writes, and it holds the
  full logging rate where the export appears to decimate — 35,317 points at
  10 Hz against 8,194 at 2.25 Hz for comparable outings.

The database stores lat/lon only, so that reader projects. Checked against the
CSV export of the same project, which carries both, pyproj reproduces SW Maps'
own X/Y to 0.5 mm — the rounding of the exported three-decimal metres. A file
assembled from one of each kind is therefore in one coordinate frame.

Any raw log inside a `.swmz` is located and reported but not parsed. It is the
input to a PPK path that does not exist yet.

## Merging outings

**File ▸ Add export…** merges another outing into what is already loaded, where
**Open export…** replaces it. Layers of the same name join; the union of their
columns is kept, so older data does not lose its columns and newer data does
not lose its extra ones.

Merging is not concatenation, because two outings do not share a vertical
datum. The antenna was mounted once per outing, the base was selected once, the
rod was measured once, and each of those carries a constant error that differs
between visits. Stack them and the surface steps at the boundary.

What makes the step removable is **overlap** — ground covered on both visits,
where the height difference is the offset because the ground did not move. So
every merge reports the crossover pairs each pair of sessions shares, and names
any session that overlaps nothing. That one is not merely awkward: its offset is
not determined by anything in the data and no later processing recovers it. The
only fix is to walk overlapping ground next time, which is why the merge says so
straight away rather than at export.

Connectivity is transitive — A tied to B and B to C puts C on A's datum without
A and C ever sharing ground — so only a session outside the main group is
called unrecoverable. **Data ▸ Sessions and overlap…** reports the same thing
at any time, and **Datum ▸ Solve session offsets only** removes the step
without touching the benchmark.

A merge discards any solved vertical model, because that model was solved for a
set of sessions that no longer exists.

## Comparing sessions

A merged layer is several outings, and one pooled crossover RMS says how good
they are together and nothing about which of them is the weak one. The
**Sessions** panel lists each outing in the active layer, with its own colour,
and the numbers to compare them by:

- points logged, the share that was RTK fixed, how many the filter stack kept,
  and how long the outing ran;
- **repeatability** - crossovers between the outing's own passes (within
  30 cm, more than 60 s apart). This is the figure to compare outings by: it
  measures the outing against itself, so a step between outings cannot
  inflate it;
- its solved offset, once **Datum ▸ Solve** has run;
- for each pair that shares ground, how far apart they sit - later minus
  earlier - as logged, and again after the solved offsets, which is how to
  see that the correction did its job.

Every session has a checkbox that takes its points off the map, and an
**only** button that shows it alone; **Colour by session** colours the points
to match the swatches. A session's colour belongs to it, so hiding one never
recolours the others.

By default hiding a session only changes what is drawn. Tick **Surface and QC
from shown sessions only** and hidden sessions also leave the surface, the 3D
view, the QC readout and the exports, so one outing's surface can be looked
at, and its QC read, on its own. The QC readout and the map both say when a
subset is in use. The vertical model is still solved from every session:
looking at one outing never moves the datum under the others.

The numbers describing a session do not change when it is hidden. They are
measured on the filtered, corrected points of every session.

## Slope, drainage and contours

The second toolbar row of the plan view shows what the surface says about the
ground:

- **shade: slope** colours the surface by slope, in percent grade, from pale
  (flat) to black at the **max** you choose (10% by default). The legend gives
  the median and 90th-percentile slope.
- **drainage** draws an arrow every 1.5 m pointing downhill. Every arrow is
  the same length: the colour underneath says how steep, the arrow says which
  way the water goes.
- **contours** draws contour lines every 2, 5, 10, 25 or 50 cm, labelled in
  centimetres above the lowest measured point, so the labels read as fall
  even before the datum is tied to anything.

**Export ▸ Slope map** and **Export ▸ Contour map** turn the same views into
finished figures: a title naming the data they were drawn from (the filter
choices, and the sessions when only some are used), axes in metres from the
local origin, a north arrow and a colour bar. The slope map uses the plan
view's slope scale; the contour map is shaded relief coloured by height above
the low point, with contours at the plan view's interval.

None of these draw ground that was not measured: slope, arrows and contours
stop at the edge of the survey, and the figures outline it. Slope is taken
after smoothing over 0.5 m - the bin size, below which there is no measured
detail - so it comes out the same on screen and in a figure gridded at 8 cm.
The QC readout's **Terrain** line gives the median, 90th-percentile and
maximum slope, and the area actually mapped.

## Projects

`File ▸ Save project` writes a `.yardproj` recording the site, every export
that was loaded, the filter chain, the vertical model, which basemaps are on and at
what opacity, the shot plan, the imagery alignment offset, and the view
settings - including which sessions are hidden and whether they are left out
of the surface, since that changes what the surface is. Reopening reproduces the **exact** same
elevations, because the solved vertical terms are stored and re-applied rather
than re-solved - a fresh solve against slightly different inputs would drift
silently.

Nothing derived is stored. Surfaces and rasters are rebuilt on load, so a stale
file can never disagree with the parameters that claim to have produced it.
Imagery comes back from the on-disk cache, so reopening does not depend on a
public service being up. Source paths under the project folder are stored
relative, so the folder can be moved or copied.

The shot plan is part of the project, not a separate document. Format version 2
added it; version 1 files predate that and open fine, and are written back out
as version 2. `Plan ▸ Save plan` still writes a standalone `.yardplan` for
carrying a plan to another project.

## Aligning imagery

County and state orthos are georeferenced to a foot or two - fine for finding
the house, not for clicking a driveway edge. Over a lot this size the residual
error is essentially a single translation, so **Basemaps ▸ Alignment** shifts
every raster together.

`Solve from points` measures the shift from the plan itself: a point whose mark
was clicked on a feature visible in the photo, and which has since been shot
with RTK, gives one reading of `measured - photographed`. The mean is the
shift; the scatter about it is the quality statement, and it is reported rather
than swallowed. Selecting rows in the shot plan overrides the automatic choice
of points.

The shift moves the **photo**. The measurements never move: they are the
better-known thing, and correcting them to agree with an aerial would be
backwards.

## Basemaps

**Data ▸ Fetch all basemaps** tries every source once and reports which ones
actually cover the lot. They then stack in the Basemaps panel, each with a
checkbox and its own opacity - a LiDAR hillshade at 40% over the leaf-off
aerial is the most useful view for spotting break lines. Photographs sit
underneath LiDAR products in the stack.

## Why there is a launcher

**Do not run `uv run` directly from this folder.** It will fail with
`Access is denied`.

This project lives in a OneDrive-synced tree. The virtual environment must stay
*outside* it: a venv is thousands of small files, and OneDrive Files On-Demand
dehydrates them into cloud placeholders. uv then cannot rewrite its own
packages and dies with `failed to remove directory ... Access is denied`.

uv only accepts the environment location as the `UV_PROJECT_ENVIRONMENT`
environment variable. There is no `uv.toml` key for it, and uv's `.env` support
loads variables for the command being run rather than for uv's own
configuration - both verified, neither works. So `yardsurvey.cmd` sets it and
then calls uv.

For running things by hand - `pytest`, `python -m gpsrtk`, `uv add` - open a
shell that already has the variable set:

```bash
devshell
```

Or set it yourself for the session:

```
set UV_PROJECT_ENVIRONMENT=%USERPROFILE%\.venvs\gps-rtk          rem cmd
$env:UV_PROJECT_ENVIRONMENT = "$env:USERPROFILE\.venvs\gps-rtk"  # PowerShell
```

There is no system Python on this machine; `uv` provisions CPython 3.12 itself.
The first run of `yardsurvey` creates the environment if it is missing.

## Tests

From `devshell`, or with the variable set:

```bash
uv run pytest
```

Network tests are opt-in, because a public GIS service being down is a fact
about the world rather than a defect here:

```bash
uv run pytest -m network
```

`tests/test_established_findings.py` pins the numbers recorded in CLAUDE.md
(5.77 cm crossover RMS, ~42% measured pixels, 48% float). Those are acceptance
criteria: if they move, that is a regression unless CLAUDE.md is updated too.

## Layout

    gpsrtk/
      units.py      international vs US survey foot, kept distinct
      site.py       CRS, local origin, vertical datum, surfacing defaults
      surface.py    bin -> grid -> distance mask -> smooth
      vertical.py   geoid, laser level network, session offsets, datum tie
      qc.py         crossover residuals, pooled and per session
      terrain.py    slope, downhill direction, contours, mapped area
      model/
        pointset.py canonical PointSet
        adjust.py   least-squares engine shared by the network and the offsets
      io/           readers, imagery/vector providers, exporters,
                    and the printed slope and contour maps (figures.py)
      filters/      serialisable filter chain
      app/          the application, with no GUI
        state.py      what is loaded, filtered, solved and fetched
        plan_edit.py  the rules behind the shot-plan table and map
        sessions.py   per-session quality, for the Sessions panel
        report.py     every readout and notice, as text
        views.py      what the plan and 3D views draw
      web/          the browser UI
        server.py     local HTTP server over gpsrtk.app
        files.py      folder listings for the file dialogs
        static/       the page: index.html, css/, js/, vendor/

## The vertical model

Raw `Elevation` is the ellipsoidal height of the **antenna** — about 860 ft
here, not the site's 100 ft datum and not height above ground. `Datum ▸ Solve
local datum` runs three steps:

1. **Laser level network.** Rod readings against a laser plane give true ground
   elevation, independent of GNSS vertical. Solved by least squares, so
   multiple instrument setups tie together properly.
2. **Session offsets.** Each outing has its own constant vertical bias — mount,
   antenna reference point vs phase centre, base station reselection, tilt.
   Solved from crossover overlap between sessions.
3. **Tie to the laser.** One constant puts the session-corrected GNSS onto the
   laser's ground surface, absorbing antenna height and the arbitrary datum
   together.

After a local solve, the laser terrain shots also go into the surface
itself: a rod read to an eighth of an inch is a better height than the GNSS
around it, and planned swales, crowns and break lines would otherwise reach no
deliverable. Each terrain shot (`lawn`, or a plan shot with a terrain purpose)
replaces the GNSS bins within 0.5 m, and the surface - smoothed or not -
passes through it exactly. The Revit points file gets the same treatment: the
shots are appended at their level-network elevations and the GNSS bins within
0.5 m of them are dropped. In ellipsoidal or NAVD88 mode the GNSS heights are
on another datum, so no laser shots are used, and the solve says so.

Two things the panel will tell you that are worth acting on:

- **A session with no overlapping ground cannot be tied.** Nothing recovers it
  afterward. Every outing should re-cover some previously surveyed ground, or
  shoot a permanent benchmark.
- **Redundancy in the level network is (shared points − 1).** A second setup
  adds its own height of instrument as an unknown, so a single shared point
  only determines it and buys no checking. Shoot at least two.

## Station names

The level network solves one elevation per **station**: the physical point a
rod was read on. Two readings are the same point exactly when they share a
station, which is how a benchmark read at the open and close of a setup, or
from two setups, checks itself. Name records in SW Maps by this convention,
in the record's name or in a `station` attribute:

- a **planned shot** is `P` and its number: **`P12`**;
- a **permanent mark** is its mark name, for example **`BM1`**;
- a **turning point** or any other re-read point gets any name - the same
  name means the same physical point;
- everything else can keep SW Maps' own ID.

A plan row is always station `P<number>`. A SW Maps record is its `station`
attribute when that is set; otherwise its name, when the name is `P12` or a
control mark; otherwise its SW Maps ID. Names are case-insensitive. So plan
shot 1 (`P1`) and SW Maps record 1 are different points, and the benchmark in
**Datum ▸ Datum tie…** can be a SW Maps ID (`12`), a planned shot (`P12`) or a
mark (`BM1`). The field sheet prints the same convention.

## Control marks and check shots

Overlap is the only other thing that ties one outing's heights to another's,
and an outing without it cannot be recovered. A permanent mark removes that
dependence. Declare the marks in **Datum ▸ Control marks…** (a name, and
optionally the elevation it is held at and a note), then:

- set 2–3 permanent marks, such as mag nails or rebar, **outside the mowed
  area**;
- shoot a mark with the **fixed-height pole** at the **start and end of every
  outing**, recorded in SW Maps under the mark's name (`BM1`);
- read the rod on `BM1` from **every laser setup**, at the open and the close;
- tie one mark to the Revit model's garage slab or door threshold, once.

A SW Maps record whose station is a mark name is a **check shot**. It belongs
to the track session nearest in time in the same export, within an hour. Two
sessions that shot the same mark are tied directly - the pole is the same
height every time, so the difference in their heights is the difference
between the sessions - which makes a session linked only through a mark
reconcilable in the merge report. The Sessions panel gives each session's
start and end check residuals and the drift between them, after its solved
offset ("checks: BM1 start +0.4 cm, end −0.8 cm"), and flags anything over
3 cm: a mount that shifted, or an overlap that says something the marks do
not. A rod reading on a mark enters the level network as that station, so
the benchmark in **Datum ▸ Datum tie…** can be `BM1`. The field sheet prints
this routine in a box at the top.

Marks are part of the site, so they travel in the project file. That made
it format version 3; an older build refuses a version 3 project with its
"newer version" message rather than silently dropping the marks.

## Imagery and reference linework

Providers are health-checked before use, because public GIS services go down.
Google, Bing and Mapbox are deliberately absent — their terms restrict caching
and derivative use, which is what this tool does with imagery.

The Iowa Geographic Map Server (ISU) carries far better imagery for this site
than the national mosaics. Its working root is
`ortho.gis.iastate.edu/arcgis/rest/services/ortho`; the endpoint list published
on its own tools page is stale and still advertises retired
`/arcgisserver/.../WMSServer` paths, which 404. Measured native resolution over
this lot:

| Source | GSD | Notes |
|---|---|---|
| Iowa ortho 2016–2018 | ~22 cm | **leaf-off** — default |
| Iowa NAIP 2023 | 30 cm | leaf-on |
| Iowa NAIP 2021 / 2025 | 60 cm | leaf-on |
| USGS NAIPPlus | 60 cm | nationwide fallback |
| Iowa ortho 2023–2025 | 1 in where flown | blank here — not flown yet |

Leaf-off matters more than resolution: the west and northwest canopy drives
~48% float, and a spring flight shows the ground under those trees.

The server also carries 2020 LiDAR (DEM, hillshade, DSM, intensity, slope, all
1 m) under **Data ▸ Fetch LiDAR raster**. The DEM is not accurate enough to
improve the surface, but differencing against it is the only *external* check
available — crossover statistics are internal to the data and cannot catch a
whole-surface datum blunder.

A service can return HTTP 200 with a valid but empty tile when an area has not
been flown. That is detected by counting distinct pixel values, not variance: a
1 m DEM over 1.2 m of relief is legitimately near-uniform and must not be
mistaken for no coverage.

Parcel polygons are cartographic (±1–3 ft), drawn dashed and labelled
REFERENCE ONLY. An authoritative boundary needs a plat traverse from a monument
shot with RTK; this tool does not pretend to provide one.

## Known gaps

- House corner shots are superseded — they were taken by eyeballing soffit
  corners. Re-shoot against the foundation before using them to register the
  Revit model.
- The local datum is still tied to spot ID 1 at an arbitrary 100.000 ft, not to
  the Revit model's 0'-0". That needs one shot on a known building feature.
- No break lines yet: swale bottoms, driveway crown, foundation perimeter,
  curb flowline.
- PPK not investigated. The LG290P can emit its own observations as RTCM3 MSM,
  and RTKLIB forward/backward with RTS smoothing should convert some of the 48%
  float into fixed.
