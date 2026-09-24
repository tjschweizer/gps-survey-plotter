# Yard Survey — Terrain Mapping

Producing a georeferenced terrain surface of a residential lot in central
Iowa, ultimately for import as a Revit toposolid. Data comes from RTK GNSS
plus a rotary laser level.

The site's own coordinates are not in this repository. They live in
`site.local.json` and in the project files, both of which are ignored by
git: one point from a survey pins a property to the centimetre.

## Hardware

- **Receiver**: SparkFun Quadband GNSS RTK Breakout, Quectel LG290P (L1/L2/L5/E6,
  GPS+GLONASS+Galileo+BeiDou+QZSS+NavIC). USB via CH342 (CDC-ACM). Default baud
  460800. Configured with Quectel `$PQTM*` sentences, not u-center.
- **Antenna**: u-blox ANN-MB2, active, 5 m RG-174, SMA. Sits on a steel ground
  plate. Passbands 1166–1285 and 1535–1602 MHz — note the upper edge clips part
  of GLONASS L1 (1598–1605 MHz), so upper GLONASS channels are attenuated.
- **Field app**: SW Maps on Android, USB OTG, built-in NTRIP client.
- **Laser**: rotary laser level + rod with detector.
- **Legacy, superseded**: CubePilot Here+ base and Here3+ (both NEO-M8P-2,
  L1-only). Could not hold an RTK fix reliably. Do not reintroduce.

## Corrections

Iowa RTK Network (public, statewide). Runs **Leica GNSS Spider**. The host
and credentials are in the local config, not here.

- Use **`MSM_NEAR`**. Serves 1074/1084/1094/1124 (MSM4) at 1 Hz, plus 1006 and 1230.
- `MSM_IMAX` also works but adds ephemeris messages (1019/1020/1045/1046) the
  receiver doesn't need, and 1006 arrives at a longer interval.
- **Never use the `RTCM3_*` mount points.** They serve legacy 1004/1012 which
  modern receivers do not decode. This wasted significant debugging time.
- `CMRP_*` are Trimble CMR+ — not decodable, and the only paid mounts.
- `RTCM3_IAAM` / `RTCM3_IAAMx` return 401 with a standard account.
- NMEA GGA must be sent upstream (`nmea=1` on all network mounts).
- NTRIP clients must send a `User-Agent` beginning with "NTRIP" or Spider
  won't stream. Plain curl fails on this.
- There is **no VRS mount point** on this network.

## Coordinate and datum conventions

**These must not change between sessions or surfaces will not align.**

- CRS: **UTM zone 15N, EPSG:32615**.
- Revit local origin: snapped to a 25 m grid near the site, from the site
  config. Revit degrades past ~33 km from its internal origin, and raw UTM
  northings here are ~4600 km out, so the offset is not optional.
- Revit points files are **headerless CSV, X,Y,Z, all in decimal feet**.
- Vertical datum is local and arbitrary. Historically spot ID 1 = 100.000 ft.
  **Not yet tied to the Revit model's 0'-0"** — needs one shot on a known
  building feature (garage slab or door threshold).
- Building bearing, dimensions and corner coordinates live in the site
  config, not here. The first set of corner shots was taken against the
  soffit and closed 1.1 ft out on the diagonals; a later set against the
  foundation closed to 0.44 ft.
- Grid vs ground: combined scale factor near 0.9996 in this zone. Plat distances
  are ground; UTM is grid. Consider the Iowa Regional Coordinate System if this
  starts mattering.

## Data format

SW Maps exports a zip containing `*_TRACK_POINTS.csv`, `*_spots.csv`,
`*_TRACKS.csv`, `*_FEATURE_POINTS.csv`, `*_PHOTOS.csv`.

Key columns: `X`/`Y` (UTM), `Elevation` (ellipsoidal metres — `Ortho Height` is
always 0, no geoid model loaded), `Fix ID` (**4 = RTK fixed, 5 = float**),
`Horizontal Accuracy`, `Vertical Accuracy`, `Speed` (m/s), `Bearing`, `PDOP`.

Spot layer custom attributes: `height number` (laser rod reading, **inches**),
`type`, `base position` (instrument setup id).

## Established findings — do not re-derive

- **Crossover accuracy, mower-mounted, fixed only: 5.77 cm RMS / 3.20 cm median**
  (pairs within 30 cm, >60 s apart).
- **Speed filtering works.** `Speed > 0.9 m/s` (2 mph) → 4.13 cm RMS, keeps 62%
  of points and loses <1% of coverage. `> 1.1 m/s` → 3.32 cm but only 40% kept.
  Confounded: slow points cluster at turns near trees, so the filter partly
  selects for open sky.
- **Turn-rate filtering barely helps** (5.49 vs 5.77 cm) and adds little on top
  of speed.
- **Float error is ~50/50 bias vs noise**: 27.6 cm episode-to-episode bias,
  29.2 cm within-episode noise. Bimodal — episodes with std 13–15 cm are
  recoverable by removing a constant offset; episodes with std 50–65 cm are not.
- **Reported accuracy is optimistic for float by ~3×** (correlation with actual
  error only 0.44). Inflate float σ before any inverse-variance weighting.
- **Walked-vs-planted antenna height offset was 26.36 in**, with only 1.36 in
  scatter. Constant offsets like this must be identified before merging datasets
  or they inject spikes.
- **No heading-dependent (lever-arm) bias** in mower data — antenna is
  adequately over the axle.
- Canopy on the **west and northwest** side of the lot drives ~48% float. That
  region needs re-mowing at a different time of day, or laser shots.

## Processing conventions

- Filter to `Fix ID == 4` for surfaces unless explicitly testing float handling.
- **Bin to 0.5 m cells (median) before interpolating.** Points are ~0.3 m apart
  along a pass and ~1 m between passes; raw triangulation produces sliver
  triangles that hillshade into false corduroy.
- **Mask anything >1.6 m from a measured sample.** Do not interpolate across the
  house footprint or tree wells and present it as terrain.
- Report the measured/interpolated pixel ratio with any raster. Current best is
  ~42% measured.
- Prefer measured QC (crossover residuals, independent laser comparison) over
  the receiver's own accuracy estimates.

## Existing scripts

Superseded by the `gpsrtk` package and kept locally in `archive/`, which is
not version controlled because those scripts carry the site constants at
module top.

- `archive/process_survey.py` — laser spots → elevations, contours, slope
- `archive/process_full.py` — tracks + spots merged, crossover QC, Revit export
- `archive/heightmap.py` — 16-bit heightmap + hillshade + world file
  (`--all` includes float, `--minspeed <m/s>` applies the speed filter)
- `archive/iso.py` — isometric renders (`--fixed`)
- `archive/to_revit.py` — local origin offset and unit conversion

## Open work

- UI for processing and acquisition planning (this project).
- Tie vertical datum to the Revit model.
- Re-shoot house corners; current diagonals disagree by 1.1 ft.
- Fill west/northwest canopy gap.
- Break lines not yet collected: swale bottoms, driveway crown, foundation
  perimeter at 1 ft and 10 ft, curb flowline.
- Investigate PPK: LG290P can emit its own observations as RTCM3 MSM. RTKLIB
  combined forward/backward with RTS smoothing should convert some float to fixed.
- Possible ArduPilot rover to automate acquisition on repeatable transects.