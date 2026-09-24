# Yard Survey

Georeferenced terrain surfacing from RTK GNSS and rotary laser survey data.
See `CLAUDE.md` for the site conventions, hardware, and established findings
that this code is built to preserve.

## Running

```bash
yardsurvey "archive\Project 1.zip"
```

Omit the path to start empty and use File ▸ Open export.

Typical session: open an export, **Data ▸ Fetch all basemaps**, tune the
filter stack, **Datum ▸ Solve local datum**, then **File ▸ Save project**.

## Running the tests

    uv run python -m pytest

Around 100 tests run anywhere. The rest need a survey export to work from and
skip without one, because the numbers they pin are measured facts about real
ground rather than properties of the code. Point `tests/conftest.py` at an
export of your own to run them.

Network tests are deselected by default; `-m network` runs them.

## Configuring a site

Everything is relative to a *site*: a CRS, a local origin snapped to a 25 m
grid, and a vertical datum. There are no site constants in the source. A fresh
window opens on `site.local.json` if there is one and on a public example
otherwise, and a project file carries its own site regardless.

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

## Projects

`File ▸ Save project` writes a `.yardproj` recording the site, every export
that was loaded, the filter chain, the vertical model, which basemaps are on and at
what opacity, the shot plan, the imagery alignment offset, and the view
settings. Reopening reproduces the **exact** same
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
      qc.py         crossover residuals
      model/
        pointset.py canonical PointSet
        adjust.py   least-squares engine shared by the network and the offsets
      io/           readers, imagery/vector providers, exporters
      filters/      serialisable filter chain
      ui/           PySide6 app

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

Two things the panel will tell you that are worth acting on:

- **A session with no overlapping ground cannot be tied.** Nothing recovers it
  afterward. Every outing should re-cover some previously surveyed ground, or
  shoot a permanent benchmark.
- **Redundancy in the level network is (shared points − 1).** A second setup
  adds its own height of instrument as an unknown, so a single shared point
  only determines it and buys no checking. Shoot at least two.

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
