# Review plan — approved work list

A handoff file for a Claude Code session, most likely the desktop app on
the Windows machine. It holds the approved list of additions (A),
changes (C) and optimizations (O) from the review of September 2026. It also
records the decisions behind them and what is already done, so the work can
continue without the original conversation.

**Start here:** read `CLAUDE.md` and `README.md` first. They are
authoritative. Then work down the **Order of work** table below, one item at
a time.

---

## 1. Ground rules

These were set by the owner and apply to every item.

### Hard constraints

- Keep every test in `tests/test_established_findings.py` passing. Keep the
  CLAUDE.md findings numbers unchanged. Those findings are measured facts, not
  hypotheses to revisit.
- Keep EPSG:32615, the 25 m local-origin grid, and the Revit points format
  (headerless X,Y,Z in decimal feet) unchanged.
- Keep the server bound to 127.0.0.1 with its origin checks intact.
- Never add Google, Bing or Mapbox imagery.
- Never print, log, hardcode or commit real site coordinates,
  `site.local.json`, project or plan files, or survey exports.
- Only make changes that an approved item asks for. No drive-by refactors,
  renames or extra features.

### How to work

- Work on the 'main' branch.
- Implement items one at a time, in the order below. Make one commit per item,
  with the item's ID as the commit-message prefix, for example
  `C8: Normalise the type attribute`.
- After each item, run the non-browser suite. For UI items, also run the
  browser tests.
- Report each item as: `✅ [ID]: files changed; pass/fail/skip vs baseline`.
- Anything new you find goes on a "New findings" list. Do not fix it.

### Stop and ask the owner before

- Adding or upgrading any Python or vendored JS dependency, or introducing a JS
  build step or `node_modules`.
- Deleting or renaming any file.
- Bumping the `.yardproj` / `.yardplan` format version, or changing how
  existing files load. The owner approved A3, C2 and C25 knowing each needs
  this. Still say so plainly when you commit them.
- Editing CLAUDE.md or README.md beyond what an approved item states.
- Any push, pull request or merge. (Update, 2026-09-24: the owner has since
  allowed pushing `main` to `origin` directly. Pull requests and merges still
  need asking.)
- A test that still fails after two fix attempts. Stop and report rather than
  looping.

### Priorities from the owner

- **Workflow and UI come before anything Revit-specific.** The Revit export
  is increasingly likely to go unused. Keep the Revit parts of items minimal
  and do them last.
- **A9 (rover mission export) is deferred, not rejected.** See section 7.

---

## 2. Environment

- **Windows, OneDrive folder.** Never run `uv run` there without
  `UV_PROJECT_ENVIRONMENT=%USERPROFILE%\.venvs\gps-rtk`, or it fails with
  "Access is denied". `devshell.cmd` sets it.
- **Linux or cloud checkout.** `uv run pytest` works as it is.
- **Tests that need real data.** About 50 tests need a real survey export and
  skip without one. This includes all 10 in `test_established_findings.py`.
  They can only run on the machine that has the private export. Run them there
  after any item that touches filters, QC, the surface or the vertical model
  (C6, C24, C25, O1, C1, C2, A1, A3).
- **Browser tests.** They need Chromium through Playwright:
  `uv run playwright install chromium` once. Alternatively, set
  `YARDSURVEY_CHROMIUM` to a Chromium binary. Without either they skip. In the
  cloud review sandbox they ran with
  `YARDSURVEY_CHROMIUM=/opt/pw-browsers/chromium-1194/chrome-linux/chrome`.
- **Network tests.** Any `-m` replaces the `addopts` default (R24), so
  `-m "not browser"` used to select the network tests too. Since C31 they
  are skipped unless the `-m` expression names `network` or
  `YARDSURVEY_NETWORK=1` is set.

### Test counts

| When | Non-browser (`-m "not browser"`) | Browser (`-m browser`) |
|---|---|---|
| Review baseline, at `2bb8665` | 282 passed, 55 skipped, 35 deselected | 35 passed (with the Chromium path set) |
| After C5, C4, C9 | 285 passed, 55 skipped, 35 deselected | not re-run (no UI change yet) |
| Desktop baseline (Windows, `12a34e0`) | 285 passed, 50 skipped, 40 deselected | 35 passed |
| After C8 | 287 passed, 50 skipped, 40 deselected | — |
| After C11 | 288 passed, 51 skipped (+1: `tzset` test on Windows), 40 deselected | — |
| After C12 | 292 passed, 51 skipped, 40 deselected | — |
| After C7 | 335 passed, 51 skipped, 40 deselected | 35 passed |
| After C1 | 337 passed, 51 skipped, 40 deselected | — |
| After C2 | 354 passed, 51 skipped, 40 deselected | 35 passed |
| After C25 | 363 passed, 51 skipped, 40 deselected | 35 passed |
| After C6 | 365 passed, 51 skipped, 40 deselected | 35 passed |
| After O1 | 367 passed, 51 skipped, 40 deselected | 35 passed |
| After O2 | 370 passed, 51 skipped, 40 deselected | 35 passed |
| After O3, C3 | 376 passed, 51 skipped, 40 deselected | 35 passed |
| After A6 | 381 passed, 51 skipped, 40 deselected | 35 passed |
| After A2 | 386 passed, 51 skipped, 40 deselected | 35 passed |
| After A4 | 391 passed, 51 skipped, 40 deselected | 35 passed |
| After A3 | 403 passed, 51 skipped, 40 deselected | 36 passed |
| After A5 | 408 passed, 51 skipped, 42 deselected | 37 passed |
| After A1 | 414 passed, 51 skipped, 42 deselected | 37 passed |
| After A7 | 416 passed, 51 skipped, 43 deselected | 38 passed |
| After C13 | 418 passed, 51 skipped, 44 deselected | 39 passed |
| After C14 | 418 passed, 51 skipped, 45 deselected | 40 passed (twice) |
| After C15 | 419 passed, 51 skipped, 46 deselected | 41 passed |
| After C16 | 420 passed, 51 skipped, 47 deselected | 42 passed |
| After C17 | 420 passed, 51 skipped, 49 deselected | 44 passed |
| After C18 | 420 passed, 51 skipped, 51 deselected | 46 passed |
| After C19 | 421 passed, 51 skipped, 52 deselected | 47 passed |
| After C20 | 422 passed, 51 skipped, 53 deselected | 48 passed |
| After C21 | 424 passed, 51 skipped, 53 deselected | not re-run (no page change) |
| After C22 | 424 passed, 51 skipped, 54 deselected | 49 passed |
| After C23 | 424 passed, 51 skipped, 55 deselected | 50 passed |
| After A8 | 425 passed, 51 skipped, 56 deselected | 51 passed |
| After C29 | 427 passed, 51 skipped, 56 deselected | 51 passed |
| After C10 | 428 passed, 51 skipped, 56 deselected | — |
| After C24 | 429 passed, 51 skipped, 56 deselected | — |
| After C27 | 431 passed, 51 skipped, 56 deselected | — |
| After C28 | 433 passed, 51 skipped, 57 deselected | 52 passed |

Of the 55 skips, 50 need real data. The other 5 are network tests that the
sandbox proxy refused.

On the desktop (Windows, checkout at `C:\tmp\gps-survey-plotter`, not under
OneDrive) the non-browser run is
`uv run pytest -m "not browser and not network"` with
`UV_PROJECT_ENVIRONMENT=%USERPROFILE%\.venvs\gps-rtk`. The 50 skips there are
all real-data tests: `archive/` is not present on that machine either. The
browser tests ran against the installed Chrome, with
`YARDSURVEY_CHROMIUM=C:/Program Files/Google/Chrome/Application/chrome.exe`,
so no Playwright download was needed. Git has no identity configured there;
commits pass `-c user.name=Claude -c user.email=noreply@anthropic.com`.

The venv holds a regular (non-editable) install of `gpsrtk`, so `uv run`
rebuilds and reinstalls it whenever the source changes. When the owner has
`yardsurvey` running, its launcher `.exe` is locked and that reinstall
fails. Run the tests against the working tree instead:
`PYTHONPATH=<repo> uv run --no-sync pytest ...`.

---

## 3. The owner's answers

These answers change the designs in section 5.

1. **Mount changes within a day, and logging past midnight: yes, both
   happen.** So C25 is needed. Its design must never invent an
   "unrecoverable" session (see C25).
2. **SW Maps record IDs.** The owner will adapt their SW Maps habits to this
   software's workflow. They can give each new record a name or ID when they
   start it. Design the station-naming convention (C2, A2, A3, A4) for this
   software first, then write it down for the field.
3. **The rod is read in feet, inches and fractions.** Readings have been
   entered as inches with fractions, because of SW Maps' number field. C7 must
   accept `63 1/4`, `5' 3 1/4"`, `5-3-1/4` and similar.
4. **The antenna height is usually not set in SW Maps.** C12 only warns if it
   is ever set; it never applies the value.
5. **Revit: do whatever makes most sense, at low priority.** Decision: a
   laser shot replaces GNSS bins within 0.5 m, in both the surface and the
   Revit file.
6. **Permanent marks: yes, the owner can set them.** Make the workflow
   explicit in the README, the field sheet and the UI (A3).
7. **Rover: not yet.** A9 is deferred.
8. **C24 (smoothing in metres) was approved**, so exported heightmaps and
   figures will get smoother, to match the preview.

---

## 4. Order of work and status

| # | ID | Title | Area | Status |
|---|---|---|---|---|
| 1 | C5 | Despike ignores missing heights | processing | ✅ `c9a4330` |
| 2 | C4 | A non-survey file leaves the session alone | code health | ✅ `8e43c58` |
| 3 | C9 | Layer names come from the files inside the export | code health | ✅ `02a7c76` |
| 4 | C8 | Normalise the `type` attribute | processing | ✅ `ae4c254` |
| 5 | C11 | Correct daylight-saving times for `.swmz` | processing | ✅ `731a4e4` |
| 6 | C12 | Instrument height guard | processing | ✅ `5ca29f2` |
| 7 | C7 | Rod entry guard: feet, inches and fractions | workflow | ✅ `2fd6e47` |
| 8 | C1 | Every rod shot joins the level network | processing | ✅ `0cc6dc4` |
| 9 | C2 | Station identities: plan shots vs SW Maps records | processing | ✅ `37b38bb` (**meaning changed**) |
| 10 | C25 | Split sessions where the data proves a mount change | processing | ✅ `0817709` (**session names change**) |
| 11 | C6 | One overlap definition for the merge report and the solve | processing | ✅ `06f3c15` |
| 12 | O1 | Session offsets from cell differences | processing | ✅ `42425eb` |
| 13 | O2 | Compute crossovers and slope once | code health | ✅ `1a20e3b` |
| 14 | O3 | Filter-chain cache keyed by a token | code health | ✅ `e03aedc` |
| 15 | C3 | Heights labelled by vertical model | processing | ✅ `6a8f32f` |
| 16 | A6 | Source fingerprints | code health | ✅ `af97773` |
| 17 | A2 | Fill plan shots from SW Maps records by station | workflow | ✅ `ea97af3` |
| 18 | A4 | Per-setup level closure and laser check | workflow | ✅ `7d29d7b` |
| 19 | A3 | Control marks and start/end check shots | workflow | ✅ `1a5cf80` (**format v3**) |
| 20 | A5 | Tie transects | workflow | ✅ `062fd1f` |
| 21 | A1 | Laser terrain shots in the surface (and the Revit file) | processing | ✅ `e1bb548` |
| 22 | A7 | Spot IDs on the map and in the datum dialog | UI | ✅ `50c887e` |
| 23 | C13 | Status strip above the map | UI | ✅ `57efec7`, fix `1a623b9` |
| 24 | C14 | Points drawn under the surface's weight | UI | ✅ `2b7d33e` |
| 25 | C15 | Success reports stop being modal | UI | ✅ `67b579a` |
| 26 | C16 | Consistent units in 2D, 3D and layers | UI | ✅ `4eec96e` |
| 27 | C17 | Legend: ticks, marker key, no overlap | UI | ✅ `b336ab9` |
| 28 | C18 | Contrast, colour tokens, type and spacing scale | UI | ✅ `0a653d7` |
| 29 | C19 | Rod column second, in the table and the field sheet | UI | ✅ `9b905b7` |
| 30 | C20 | Readable filter stack | UI | ✅ `f3a6f34` |
| 31 | C21 | Grouped fetch errors and a short connect timeout | UI | ✅ `8cd2b66` |
| 32 | C22 | File dialog: date column, sorting, keyboard | UI | ✅ `e361792` |
| 33 | C23 | Keyboard menus and labelled fields | UI | ✅ `4cfb6fb` |
| 34 | A8 | Start panel and recent files | UI | ✅ `9c90e3f` |
| 35 | C29 | Solve report wording | UI | ✅ `eae8315` |
| 36 | C10 | World file on the grid | processing | ✅ `2143862` |
| 37 | C24 | Surface smoothing sized in metres | processing | ✅ `116824f` |
| 38 | C26 | .gitignore covers exports | code health | ✅ `757b32b` |
| 39 | C27 | Atomic saves | code health | ✅ `aedcc88` |
| 40 | C28 | Server hardening | code health | ✅ `ef50afd` |
| 41 | C30 | Hide the do-nothing surface-residual stage | code health | ✅ `8b8804d` |
| 42 | C31 | Network tests stay off under any `-m` | code health | ✅ `0970ee9` |
| 43 | C32 | CLAUDE.md: rover is PX4, plus the A9 note | docs | ✅ |
| — | A9 | Rover mission export | workflow | **deferred** (section 7) |

The order follows dependencies, then puts workflow and UI ahead of anything
Revit-related:

- C2 needs C1.
- A2, A3 and A4 need C2.
- O1 comes after C6, because both touch the solver.
- A3 comes after A4, because both use stations.

---

## 5. Items in detail

Each item has what, why (with evidence), how to verify, whether it breaks
anything, and implementation notes. The file:line references are from
`2bb8665`. Experiments E1–E11 were run against the synthetic export, in
the review session's scratchpad.

### Done

**C5 — Despike ignores missing heights** ✅
- **Change:** `filters/threshold.py`. Rows with no height are dropped and are
  left out of the percentiles.
- **Why:** before the fix, one NaN made the percentiles NaN and every row was
  dropped (E10: 1000 → 0).
- **Test:** `tests/test_filters.py::test_despike_survives_a_missing_height`.

**C4 — A non-survey file leaves the session alone** ✅
- **Change:** `app/state.py` `load()` refuses an export with no point layers,
  before changing anything.
- **Why:** before the fix, opening `notes.csv` emptied the layers, the solved
  model and the hidden sessions.
- **Test:** `tests/test_web_api.py::test_a_file_with_no_survey_points_leaves_the_session_alone`.

**C9 — Layer names come from the files inside the export** ✅
- **Change:** `io/swmaps.py`. The project prefix is read from the
  `*_TRACK_POINTS.csv` / `_TRACKS` / `_FEATURE_POINTS` / `_PHOTOS` members.
  Session names still use the zip's own name.
- **Test:** `tests/test_merge.py::test_a_renamed_export_keeps_its_layer_names`.

### Import and processing

**C8 — Normalise the `type` attribute** ✅
- **Done:** `io/base.py` `normalise_kind`, called by both readers. Tests:
  `test_merge.py::test_the_type_attribute_is_normalised`,
  `test_swmz.py::test_the_type_attribute_is_normalised`.
- **What:** both readers (`io/swmaps.py` `normalise`, and
  `io/swmaps_project.py` after the attribute join) strip whitespace from
  `KIND` and lower-case it.
- **Why:** `KindSelect` matches exactly (`filters/select.py:53`), and
  `solve_vertical` keeps only `"lawn"`. So "Lawn" or "lawn " is silently
  dropped from the network and the tie.
- **Verify:** a reader test with "Lawn " comes back as "lawn".
- **Breaking?:** a re-solve may include shots that were dropped before. Stored
  models are unchanged.

**C11 — Correct daylight-saving times for `.swmz`** ✅
- **Done:** `_local_times` looks up the local offset and zone name per whole
  minute of data, from `datetime.fromtimestamp(..., utc).astimezone()`.
  Tests: `test_swmz.py::test_each_time_uses_the_offset_for_its_own_date`
  (runs everywhere, compares with the platform's own conversion; failed
  before the fix on the Central-time desktop) and
  `::test_winter_times_are_standard_time` (sets `TZ`; skips on Windows).
- **What:** `io/swmaps_project.py:_local_times` uses the machine's *current*
  UTC offset for every timestamp. Convert each timestamp with the local rule
  for its own date instead: `datetime.fromtimestamp(sec)` with no argument, or
  a vectorised equivalent. Derive the zone abbreviation from the same
  conversion. No new dependency: `zoneinfo` would need `tzdata` on Windows.
- **Why:** under `TZ=America/Chicago`, a January 18:00 UTC epoch became
  13:00 "CDT". It should be 12:00 CST.
- **Verify:** a test that sets `TZ` via `monkeypatch.setenv` and calls
  `time.tzset()`. Skip the test on Windows, where `tzset` doesn't exist.
- **Breaking?:** re-read winter `.swmz` times move by 1 h. A session's date
  can only change for points within an hour of midnight.

**C12 — Instrument height guard** ✅
- **Done:** `merge.instrument_height_notes`, called from `AppState.load`
  into `MergeReport.notes`. The open-export route now shows a notice when
  there are notes (it only did for re-projection before), as does the
  add-export route when nothing was loaded yet; `load_project` passes the
  notes on as warnings. Tests: `test_merge.py::test_a_set_instrument_height_*`,
  `::test_an_instrument_height_that_changes_mid_session_is_reported`,
  `::test_an_unset_instrument_height_says_nothing`,
  `test_web_api.py::test_opening_an_export_with_instrument_height_says_so`.
- **What:** on load, warn if `ANT_HT` (SW Maps "Instrument Ht") is non-zero
  anywhere, or varies within a session. Say that it is not applied. Carry the
  warning in `MergeReport.notes`, so it shows in the open/merge notice.
- **Why:** the column is read but never used (`model/pointset.py:42`,
  `vertical.apply_vertical`). If it is ever set, heights would mix conventions
  silently. The owner usually forgets to set it, so this only warns.
- **Verify:** a synthetic export with `Instrument Ht` = 1.8 produces the
  warning.
- **Breaking?:** no.

**C7 — Rod entry guard: feet, inches and fractions** ✅
- **Done:** `units.parse_rod`, used by `plan_edit.edit_cell` (which gained
  `confirm=`; `/api/plan/cell` passes it through, and the page's existing
  confirm-and-resend path needed no change) and by both readers through
  `io/base.py` `rod_readings`, which leaves a refused value as NaN. `5-3`
  (no fraction) is read as 5 ft 3 in, consistent with `5-3-1/4`; `63-1/4`
  is inches because its second part is a fraction. Unicode primes and
  vulgar fractions (¼, ½, ...) are accepted. Tests: new
  `tests/test_units.py`; `test_plan_edit.py` rod tests (the "not a number
  of inches" wording became "not a rod reading");
  `test_web_api.py::test_a_rod_reading_under_a_foot_is_confirmed_first`;
  `test_merge.py::test_a_rod_reading_written_as_text_is_read`;
  `test_swmz.py::test_a_rod_reading_in_feet_and_inches_is_read`.
- **What:** one parser, `units.parse_rod(text) -> inches`, used by the plan
  table (`app/plan_edit.py:177`). It must accept:
  - `63`, `63.25`, `63 1/4`, `63-1/4`;
  - `5' 3 1/4"`, `5'3.25"`, `5' 3-1/4`, `5 ft 3 1/4 in`;
  - `5-3-1/4`, meaning feet-inches-fraction as read off a grade rod.

  It must refuse negatives and garbage. Ask for confirmation, through the
  existing `NeedsConfirmation` path, below 12 in ("did you mean feet?") or
  above 200 in. Also use the parser in both readers for `ROD_IN`: today
  `numeric()` turns text such as "63 1/4" into NaN, which silently drops the
  reading.
- **Why:** R7. Any float is accepted as inches today, and fractions are lost.
- **Verify:** parser unit tests; the table edit asks to confirm on "5.26"; a
  reader test with a text rod attribute.
- **Breaking?:** no. Plain numbers mean inches, as before.

### Level network and sessions

**C1 — Every rod shot joins the level network** ✅
- **Done:** `solve_vertical` passes the whole spot set to `level_network`
  (which already skips rows with no reading, and now forward-fills setups
  across every kind) and checks the benchmark against every rod shot.
  `lawn` still feeds the tie and the session-offset evidence; a solve with
  rod shots but no terrain ones now says so in the notes instead of tying
  to nothing. Tests: `test_vertical.py::test_a_benchmark_plan_shot_can_hold_the_datum`
  (the E1b scenario), `::test_a_building_shot_joins_the_network_but_not_the_tie`.
  **Needs a local real-data run:** the real spots include `bldg` shots,
  which now join the network (`test_vertical.py` real-data tests).
- **What:** `vertical.solve_vertical`. `level_network` gets every spot with a
  rod reading, of any kind. Only terrain kinds (`"lawn"`, and the plan's
  terrain purposes, which already map to `"lawn"`) feed the tie reference
  (`spots_with_laser_elevations`) and the session-offset evidence.
- **Why:** R1 / E1b. A plan shot with purpose "benchmark", or a SW Maps `type`
  of "bldg", cannot be the benchmark ("benchmark point 101 is not among the
  rod shots (1-12)"). This blocks CLAUDE.md's open item of tying the datum to
  the garage slab.
- **Verify:** the E1b scenario solves with the benchmark shot held. The
  existing vertical tests still pass.
- **Breaking?:** no. A re-solve can only add points.

**C2 — Station identities: plan shots vs SW Maps records** (changes meaning;
approved)

✅ *Done.* `vertical.station_key` (canonical form: int for a bare integer,
stripped upper-case text otherwise, so `bm1` = `BM1`), `vertical.stations(ps,
marks=())` (attribute → `P\d+`/mark name → SW Maps ID; a row with none of
these becomes its own `#<index>` station), `sort_stations`,
`describe_stations`. `level_network`, `spots_with_laser_elevations` and
`solve_vertical` take `marks=` for A3; nothing passes marks yet.
`Plan.to_frame` adds `station = "P<n>"` (and keeps `point_id`). Both readers
alias `station`; the `.swmz` reader now also fills `feature_name` for
recorded shots, as the CSV export does, so rule 2 works there too.
`apply_tie` stores `station_key(point)`. `AppState.set_datum_tie` now refuses
a station that no rod shot was read on (when there are rod shots at all),
because any name is syntactically valid; the message still reads "is not a
point id", which `test_web_api.py::test_a_bad_benchmark_is_refused` pins.
The station-naming convention is in the README ("Station names") and in the
field sheet's legend. Tests: `test_vertical.py` station tests (E2, the BM1
degree of freedom, a `P3` benchmark, int-vs-name ties),
`test_swmz.py::test_a_shot_named_after_its_plan_number_is_that_station`,
`::test_the_station_attribute_is_read`. The C1 test now holds `P101`.
**Format note:** no file-format bump; `benchmark_point_id` in site/project
JSON may now be a string. A project whose benchmark was a plan number now
means the SW Maps ID of that number (approved).

*What.* Every rod observation gets a **station**, the name of the physical
point it was read on:

- A plan row is `"P<number>"`.
- A SW Maps record is, in order of preference:
  1. its `station` attribute, if present and non-empty;
  2. its feature name, if the name matches `^P\d+$` or a declared control
     mark name (A3), case-insensitive, normalised to upper case;
  3. otherwise, its SW Maps `ID`. Keep this as an int for compatibility.

The level network keys unknowns by station (`EL:<station>`), not by
`point_id`.

`VerticalDatum.benchmark_point_id` accepts an int (a SW Maps ID, as today) or
a string (`"P12"`, `"BM1"`). `apply_tie` stores an int when the text is a
bare integer, otherwise the stripped string.

`LevelNetwork.elevations` keys: an int for bare integer stations, a str
otherwise. Keeping int keys preserves `net.elevations[1]` in the real-data
tests.

`spot_ids()` and the datum dialog list stations. Sort them with ints first,
then strings.

*Why.* R2 / E2. Plan #1 and SW Maps spot 1 were solved as one point, with
residuals of ±31.8 in. Nothing let two SW Maps records be the same physical
point, which is the basis for A3 and A4.

*Verify.*
- The E2 scenario gives zero residuals.
- Two SW Maps records with `station="BM1"` from one setup add one degree of
  freedom.
- A benchmark of `"P3"` works.

*Breaking?* A project whose benchmark was a plan number now means the SW Maps
ID of that number. Bare integers otherwise load unchanged. Approved by the
owner.

*Also:* add the `station` attribute alias in both readers (`"station"` →
canonical `STATION = "station"` in `model/pointset.py`).

**C25 — Split sessions where the data proves a mount change** (session names
change; approved)

✅ *Done.* New module `gpsrtk/sessions_split.py` (`split_sessions`), called
by `io/swmaps.py` `assign_sessions`; the `.swmz` reader now assigns sessions
per layer, as the CSV export's files already were. `SessionInfo.date` reads
the first ten characters. Two decisions beyond the design, both recorded in
the module docstring:
- **Hard split at gaps ≥ 6 h** (`OUTING_GAP_S`). The owner chose this on
  2026-09-24 when asked: under the literal design a later outing in the
  same export that shares no ground with the earlier one would have merged
  silently into its session. Now it is its own session and, if it overlaps
  nothing, the merge report says so. That is true, not invented.
- **Where a proven split lands.** A block with too few cells to test (for
  example a partial pass right after the pause) stays with the session; when
  a later block proves a step, the new session starts at the longest pause
  since the last block proven to be on the old mount. Without this, the
  first few points on the new mount carried the old offset: a whole-step
  spike.

Evidence is RTK-fixed heights only (float would invent steps). Tests: new
`tests/test_sessions_split.py` (mount change after a 4 min pause, a bag
pause, past midnight, unprovable block, 6 h outing, next day's name, float
is not evidence, `SessionInfo.date`, rows with no time). Split costs about
55 ms per layer on the synthetic export. **Needs a local real-data run:**
`test_merge.py::test_the_two_real_formats_merge` expects 3 sessions; report
the new count rather than guessing.

*Owner's answer:* mounts change within a day, and logging runs past midnight.

*Design.* This replaces the reviewed "30 min gap" rule, which could create
false "CANNOT BE RECONCILED" sessions. The new rule is data-proven:

1. **Candidate boundaries** within one export are a time gap ≥ 3 min, or a
   change of track name.
2. **Walk the blocks in time order**, comparing each block against the
   session built so far. Start a new session only if the two share ≥ 25
   0.5 m cells (the `merge.THIN_OVERLAP` threshold), the median cell
   difference is ≥ 3 cm, **and** that difference is ≥ 3× its standard error.
   Otherwise merge the block into the current session. Blocks that don't
   overlap stay merged, because no step can be proven.
3. **Midnight:** a session is no longer cut at midnight. It keeps the date it
   started on.
4. **Names:** the first session of a day keeps today's name, `stem/YYYY-MM-DD`,
   so existing projects mostly load unchanged. Later splits on the same day are
   named `stem/YYYY-MM-DD HH:MM`, from their start time. Update
   `SessionInfo.date` to read the first 10 characters.
5. **Where:** a helper in a new module (for example `gpsrtk/sessions_split.py`,
   numpy/pandas only) called by `assign_sessions`. The spot layer is split the
   same way. Spots have no overlapping ground, so a day's spots stay one
   session.

*Verify.*
- A synthetic day with a mower pass block, a 4-minute gap, then a walked
  block 1 m higher splits into two sessions.
- A synthetic day with a bag-emptying gap and no height change stays one
  session.
- A session crossing midnight stays one.
- The existing synthetic merge tests still pass.

*Breaking?* On days with a proven split, session names change. Offsets and
hidden sessions keyed by the old names no longer match for those days.
Approved. Real-data tests that count sessions (for example
`test_merge.py::test_the_two_real_formats_merge`, which expects 3) may need
updating after a local run. Report that; don't guess.

**C6 — One overlap definition for the merge report and the solve** ✅
- **Done:** `merge.overlap_pairs` is the one definition (moving points within
  `RADIUS_M` 0.30 m; a static shot, `merge.static_rows` = rod reading or
  `type`, within `STATIC_RADIUS_M` 1.0 m; > 60 s apart; each pair once).
  `merge.session_overlap` counts it and `vertical.session_offsets` solves
  from it; `solve_vertical` now defaults to `radius_m=0.30,
  static_radius_m=1.0` (it used 1.0 m for everything). `MergeReport` gained
  `static_shots` and its heading says "within 30 cm, or 1 m of one of N
  static shots". `AppState.session_report` (used by `load` too) describes
  sessions from the logged layer plus terrain shots with a GNSS height, and
  judges overlap on the filtered layer plus those shots, via
  `diagnose(ps, overlap=...)`.
- **Beyond the item, needed for its own check:** with static shots now
  pairing at 1 m and tracks at 30 cm, the synthetic merge solved 3.8 cm
  instead of 5 cm. The first outing's spots are on a 2 m pole but share the
  mower session's name, and each spot-to-outing-2 pair carried the ~1 m
  pole-versus-mower difference (the old 1 m track pairs had diluted it).
  `session_offsets` now gives a session's static shots their own offset
  unknown (`"<session> (static shots)"`, `vertical.STATIC_SUFFIX`) when the
  session also has moving points. It is a nuisance parameter: left out of
  `offsets`, and connectivity and pair counts stay by real session name.
  The synthetic merge now solves +5.00 cm.
- **Tests:** `test_merge.py::test_moving_points_pair_within_30_cm_and_static_shots_within_a_metre`,
  `::test_the_merge_report_and_the_solve_agree_on_a_spot_outing` (E6: a
  spot-only outing 50 cm from the passes; the report and the solve agree
  pair for pair). **Needs a local real-data run.**
- **What:** `merge.diagnose` / `session_overlap` and `vertical.session_offsets`
  use the same pairing: 0.30 m between tracks, 1.0 m where one end is a static
  shot. Detect static shots as rows with a rod reading or a `type`, or pass
  them explicitly from `solve_vertical`. The merge report says which applies.
  `AppState.load` / `session_report` diagnose over tracks plus terrain spots,
  the same set the solver uses.
- **Why:** R6 / E6. The merge report (0.30 m, tracks only) called a spot
  session "CANNOT BE RECONCILED", while the solve (1.0 m, `vertical.py:505`)
  tied it with 305 pairs.
- **Verify:** the E6 scenario gives a consistent report. `test_merge` still
  passes, including `test_merged_sessions_can_be_solved` at 0.05 ± 0.01 m.
- **Breaking?:** stored models are unchanged. A re-solve may move offsets by a
  few millimetres. Real-data tests need a local run.

**O1 — Session offsets from cell differences** ✅
- **Done:** `merge.overlap_observations` (returns an `Overlap`: unknowns
  `a`/`b`, `dz`, real sessions, `kind` "cell"/"shot") replaces C6's
  `overlap_pairs`. Cells are `CELL_M` = 0.5 m, anchored at 0,0; a cell
  covered by two unknowns gives one observation at their medians, if their
  median times are more than `min_seconds` apart. Each static shot gives one
  observation per other unknown within 1 m (median of that unknown's
  points there). The C6 static-shot unknowns carry over (`STATIC_SUFFIX`
  now lives in `merge`). `session_offsets` adds one weight-1 row per
  observation, so the standard errors come from the scatter between cells;
  its `radius_m` is now the cell size (default 0.5, and `solve_vertical`'s
  default is 0.5), which keeps `test_session_offsets_recover_an_injected_bias`
  (`radius_m=0.5`, `min_seconds=0`) meaningful. `session_overlap` counts
  the same observations, and the merge report heading says "0.5 m cells
  both sessions cover, and N static shots compared within 1 m". `THIN_OVERLAP`
  (25) now counts observations. `qc` is untouched. Control-mark ties (step
  3) come with A3.
- **Numbers:** synthetic merge, +5 cm injected: 27,017 raw pairs, +5.00 ±
  0.01 cm before; 3,454 cell observations, +4.98 ± 0.03 cm after; solve
  0.19 s.
- **Tests:** `test_merge.py::test_passes_meet_by_cell_and_static_shots_within_a_metre`,
  `::test_a_shared_cell_is_one_observation_however_many_points`,
  `::test_a_stop_is_one_cell_not_millions_of_pairs`; the C6 and merge tests
  still pass (`test_merged_sessions_can_be_solved` at 0.05 ± 0.01 m).
  **Needs a local real-data run.**
- **What:** `vertical.session_offsets` switches from raw 10 Hz point pairs to
  0.5 m cells, anchored at 0,0, which is the same grid as the site origin:
  1. **Track-to-track:** for each cell occupied by ≥ 2 sessions, take each
     session's median height. That gives one observation per session pair in
     the cell, weight 1.
  2. **Static shots:** each shot is compared with each other session's median
     within 1.0 m, one observation per shot and session.
  3. **Control marks (A3, later):** identical station, a direct tie.

  The standard errors come from the scatter between cells. The merge report
  (C6) counts the same observations. `qc.crossover_stats`, the pinned 5.77 cm
  definition, is **not** touched.
- **Why:** R15 / E8. A 5-minute stop at 10 Hz gave 2.89 M pairs, all from the
  stop, using 112 MB. R16 / E9: the standard error was reported as 0.006 cm,
  against about 0.02 cm from 5-minute block means.
- **Verify:**
  - the synthetic +5 cm offset is recovered within 1 cm;
  - a synthetic stop adds one cell, not millions of pairs;
  - `test_vertical` passes (`test_session_offsets_recover_an_injected_bias`
    uses `radius_m=0.5`, `min_seconds=0`, so keep those parameters
    meaningful);
  - real-data tests pass in a local run.
- **Breaking?:** a re-solve may shift offsets by a few mm. Stored models are
  unchanged.

**O2 — Compute crossovers and slope once** ✅
- **Done:** `AppState.crossover_pairs(ps)` caches `qc.crossover_pairs`
  (30 cm, 60 s) per point-set object, holding the object so an address can
  never be reused, for the last two sets (`result`, `corrected`).
  `report.qc_text(state, slope=None)` summarises `qc.pair_diffs` of those
  pairs; `qc.session_crossovers` gained `pairs=` (renumbered onto the rows
  with a height), used by `sessions_payload` for both columns. The server's
  QC cache passes `slope_field()` to `qc_text`, so `terrain_line` reuses
  it. `qc.crossover_stats` itself is untouched. The solver's rows are
  added by `LeastSquares.add_differences`, a bulk call that also joins each
  pair of unknowns once rather than once per row. (After O1 there are
  thousands of observations, not millions, so this matters less than the
  review expected.)
- **Numbers:** a snapshot now runs one neighbour search, not three. On the
  merged synthetic set (15,330 points) the QC text plus the Sessions panel,
  cold, went from 0.070 s to 0.055 s; the synthetic set is sparse (~3 Hz),
  and at 10 Hz the search dominates more.
- **Tests:** `test_sessions.py::test_the_readouts_share_one_neighbour_search`
  (counts calls), `::test_shared_pairs_give_the_same_numbers`,
  `test_vertical.py::test_differences_added_in_bulk_solve_the_same`.
- **What:** crossover pairs are cached once per `result` object and revision,
  and shared by `report.qc_text` (`report.py:97`) and `app/sessions.py:45-49`.
  Build the solver's observation rows with array operations instead of a
  Python loop over pairs (`vertical.py:283`). The QC terrain line
  (`report.py:264`) reuses `Server.slope_field()`, or a cache on the state.
- **Why:** R17 / E11. QC text took 0.49 s and the sessions panel 0.71 s per
  change at 35k points.
- **Verify:** identical numbers before and after, plus a timing check.
- **Breaking?:** no.

**O3 — Filter-chain cache keyed by a token** ✅
- **Done:** every `PointSet` gets `token` from a module-level
  `itertools.count` in `__post_init__` (so `replace()`, `select()` and
  `with_frame()` all get fresh ones); `FilterChain` keys its prefix cache on
  `_source_token`. Tests: `test_filters.py::test_a_new_source_of_the_same_size_is_filtered_afresh`
  (on this machine the replacement did land at the old address, and the old
  code returned the old export's 7,987 points instead of 0),
  `::test_every_point_set_has_its_own_token`.
- **What:** `FilterChain.run` keys its prefix cache on `id(ps)`
  (`filters/base.py:106`), which Python can reuse after garbage collection.
  Give each PointSet a monotonically increasing token, or let `AppState` pass
  an explicit source token.
- **Verify:** a unit test that replaces the source with an equal-sized new one.
- **Breaking?:** no.

**C3 — Heights labelled by vertical model** ✅
- **Done:** `vertical.height_label(model, site, z_column, short=False)` is
  the one wording: raw → "RAW ELLIPSOIDAL antenna height (no vertical
  model)" / "raw ellipsoidal"; ellipsoidal → "ELLIPSOIDAL antenna height,
  session offsets applied" / "ellipsoidal, offsets applied"; navd88 →
  "NAVD88 orthometric, antenna height"; local → "tied to <frame>" or "local
  datum, ARBITRARY origin" / "local datum". A local model with no datum
  shift (the tie could not be made) is labelled ellipsoidal, since that is
  what its heights are. Used by the QC Heights line, the legend
  (`views.terrain_payload`), the heightmap INFO (new "heights" line;
  `write_heightmap(datum=)`) and notice, and the Revit notice and sidecar
  (`write_points(vertical=)`, `revit_notice(res, site, vertical)`), which
  now warn for antenna heights in any mode. The heightmap tag is
  `report.filter_tag(chain)`: "fixed" when the stack keeps only fix 4,
  otherwise "all". Tests: `test_web_api.py::test_offsets_only_heights_are_not_called_a_local_datum`
  (the E4 scenario), `::test_a_local_solve_is_labelled_local_and_arbitrary`,
  `::test_navd88_heights_say_they_are_of_the_antenna`,
  `::test_the_heightmap_is_named_after_the_points_it_was_made_from`.
- **What:** the label depends on the model's mode, not just on whether `ELEV`
  exists:
  - ellipsoidal → "ellipsoidal antenna height, session offsets applied";
  - navd88 → "NAVD88 orthometric, antenna height";
  - local → tied or arbitrary.

  Apply it to:
  - the QC Heights line (`report.py:115`);
  - the legend label (`views.py:378`);
  - the heightmap INFO file, which gains a datum line;
  - the Revit notice and sidecar (`io/revit.py:88`). This is the minimal Revit
    part.

  The heightmap filename tag comes from the filters (`fixed` / `all`) instead
  of the hard-coded `_fixed` (`server.py:827`). Pass the mode through:
  `Surface` or the state knows the vertical model.
- **Why:** R3 / E4. After "Solve session offsets only" (the merge dialog's own
  button), the Revit file held 857–861 ft antenna heights labelled "LOCAL
  ARBITRARY".
- **Verify:** tests per mode.
- **Breaking?:** only the sidecar and INFO text change.

### Workflow additions

**Station naming in the field.** This convention serves C2, A2, A3 and A4.
Write it into the README and onto the field sheet. The owner has agreed to
follow it in SW Maps:

- A planned shot is recorded as **`P12`**, in the name or `station` field.
- A permanent control mark is recorded by its mark name, for example
  **`BM1`**.
- A turning point or any other re-read point gets any station name. The same
  name means the same physical point.
- Everything else can keep SW Maps' own ID.

**A6 — Source fingerprints** ✅
- **Done:** `project.fingerprint(path)` → `{size, sha256}` (a folder of CSVs
  is hashed file by file in name order). `Project.source_fingerprints`,
  keyed by source path and stored relative like `sources`; an optional key,
  no format bump. `AppState.fingerprints` is filled at every load;
  `load(merge=True)` refuses content identical to a loaded source ("has
  exactly the same contents as X ... a renamed copy") before reading it;
  `to_project` writes the fingerprints; `load_project` warns when a
  source's hash differs from the stored one. Tests in `test_project.py`:
  `test_a_renamed_copy_is_refused_as_already_loaded` (E7),
  `test_fingerprints_are_saved_with_the_project`,
  `test_a_source_changed_since_saving_is_warned_about`,
  `test_an_unchanged_source_is_not_warned_about`,
  `test_a_project_without_fingerprints_still_opens`.
- **What:**
  - `project.py`: new optional key `source_fingerprints` holding
    `{path: {size, sha256}}`. Older builds ignore unknown top-level keys
    (`Project.from_dict` reads known keys only).
  - `AppState`: records a hash at load. `add_export` refuses content that is
    already loaded. `load_project` warns if a source's hash changed.
- **Why:** E7 — a renamed copy merged as new data. R28 — duplicates are
  checked by path only (`state.py:191`). The README promises exact
  reproduction, but a re-exported file would silently receive the stored
  offsets.
- **Verify:** a renamed copy is refused; a modified source gives a warning on
  reopen.
- **Breaking?:** an additive key.

**A2 — Fill plan shots from SW Maps records by station** ✅
- **Done:** `Plan.fill_from_records(records) -> FillReport` (in `plan.py`,
  with `FILL_POSITION_TOLERANCE_M` 0.10 and `FILL_ROD_TOLERANCE_IN` 1e-4).
  Position and fix come only from fix-4 records into shots with no measured
  position (method becomes "rtk"); rod and setup only into empty cells (a
  setup is also filled when the plan's reading already equals the
  record's, so the two dedupe). A rod disagreement is reported only on the
  same setup (or where either has none): a different setup is simply
  another observation. `AppState.plan_records()` gathers records whose
  station is `P<n>` from every layer except the working and plan layers,
  earliest first, with setups resolved as the network does;
  `AppState.fill_plan_from_records()` runs in `load` (notes go in
  `MergeReport.notes`, so the open and merge notices show them) and in
  `open_plan` (`last_fill`; `/api/plan/open` shows a notice, a warning when
  there are conflicts). `AppState.spots` drops plan rows that copy a SW Maps
  reading (station, resolved setup, rod within 1e-4) via
  `_without_copied_readings`. Tests: `test_plan_edit.py::test_a_record_named_after_a_plan_shot_fills_it`,
  `::test_a_copied_reading_is_one_observation`,
  `::test_a_disagreement_is_reported_and_never_written`,
  `::test_a_float_record_gives_no_position`, `::test_opening_a_plan_fills_it_too`.
- **What:** after open, merge or plan open, a SW Maps record whose station is
  `P12` fills plan shot 12:
  - **Position and fix:** only if the record's fix is 4 (fixed) and the plan
    shot has no measured position.
  - **Rod reading and setup:** if the plan cell is empty.

  Disagreements (position > 0.10 m, a different rod reading) are listed in
  the notice and never overwrite. Float fixes are ignored, which matches the
  field sheet's own rule.

  **Double counting:** a plan reading identical to the SW Maps reading it was
  copied from (same station, same setup, rod within 1e-4 in) is counted once
  in the level network. Drop the plan row when combining in `AppState.spots`.

  Files: `plan.py` (matcher), `app/state.py`, `app/report.py`, `server.py`
  notice.
- **Why:** today every GNSS position for a plan shot is retyped from the
  sheet's "GNSS pt / E,N" column (`fieldsheet.py:167` →
  `plan_edit.set_measured_position`).
- **Verify:** synthetic named records fill the plan; a conflict is reported;
  a float record is ignored; the network has no duplicate observation.
- **Breaking?:** no. It uses existing plan fields.

**A4 — Per-setup level closure and laser check** ✅
- **Done:** `vertical.SetupCheck` (shots, repeats as (station, spread in),
  stations shared with other setups, worst |residual| in, `no_check`,
  `over`, `flagged`) and `SETUP_TOLERANCE_IN` = 0.25. `level_network`
  records each observation's (setup, station, rod) and builds
  `LevelNetwork.setups` from the residuals; `LevelNetwork.describe` (the
  Vertical panel) prints "Closure per setup"; `report.solve_notice` adds
  "Laser setups to look at" for flagged ones. `level_network(tolerance_in=)`.
  Field sheet: `_setup_blocks` prints a block per planned setup (two blank
  ones if none) with the four rows the item lists, between the legend and
  the readings table. Tests: `test_vertical.py::test_a_half_inch_closing_error_is_flagged`,
  `::test_a_clean_network_passes`,
  `::test_a_setup_with_nothing_to_check_it_says_so`,
  `::test_the_solve_notice_names_setups_to_look_at`,
  `test_plan.py::test_the_field_sheet_has_a_closure_block_per_setup`.
- **What:**
  - **Report:** after the level network is solved, one entry per setup: the
    number of shots, repeat readings of the same station within the setup
    (open/close on a benchmark gives their difference), misclosures on
    stations shared with other setups, the worst residual in inches, and a
    flag over tolerance (default ¼ in). "No check" is flagged when a setup
    has no repeat and fewer than 2 shared stations. It goes in
    `LevelNetwork.describe`, the solve notice and the Vertical panel.
  - **Field sheet** (`io/fieldsheet.py`): a block per planned setup, or two
    blank blocks if none are planned. Its rows are "Backsight on ____
    (station) at open", "Backsight on ____ at close", "Calibration
    (two-peg) check: date / result", and "Observer".
- **Why:** the real network has 0 degrees of freedom
  (`tests/test_vertical.py:77`), so a mis-read rod is invisible. Closing a
  setup on the benchmark, and a periodic two-peg or four-axis check, are
  general levelling practice.
- **Verify:** a synthetic 0.5 in closing error is flagged; a clean network
  passes.
- **Breaking?:** no. Repeat readings are SW Maps records sharing a station
  (C2).

**A3 — Control marks and start/end check shots** (project format v3;
approved)

✅ *Done.* `site.ControlMark` (name upper-cased, optional `elev_ft`, note;
the held elevation is stored and shown, nothing uses it yet) and
`Site.control` / `Site.mark_names`; `Site.from_dict` accepts no `control`
key. `project.VERSION` = 3. `AppState.check_shots()` finds records whose
station (with marks) is a mark and gives each the nearest track session of
the same export (session-name prefix) within `CHECK_WINDOW_S` = 60 min.
`merge.overlap_observations(checks=)` adds one "mark" observation per mark
and pair of sessions (median heights); `diagnose(checks=)` counts them,
reports `check_shots`, and never calls a mark-tied pair "thin".
`solve_vertical`/`session_offsets` take `marks=` and `checks=`;
`AppState.solve_vertical`, `spot_ids` and `session_report` pass the site's.
`vertical.check_residuals` (the mark height comes from the reference
session, else the earliest session that shot it, so a shifted mount shows
its whole disagreement) and `describe_checks` ("checks: BM1 start +0.4 cm,
end -0.8 cm, drift ..." / "no check shots", flag over
`CHECK_TOLERANCE_M` = 3 cm); in `SessionOffsets.checks` and its describe,
and in the Sessions panel (`sessions_payload` rows `checks`,
`checks_flagged`; shown only when marks exist). `AppState.set_control_marks`
refuses duplicates and names that are already stations (digits, `P<n>`)
and re-solves a solved model. UI: `GET/POST /api/control`, "Datum ▸
Control marks…" (`controlMarksDialog`, `actions.controlMarks`). Field
sheet: a "Control marks and check shots" box above the map
(`write_field_sheet(marks=)`). README: new section "Control marks and check
shots". Tests: new `tests/test_control.py` (the Elsewhere outing tied and
its 4 cm recovered, unreconcilable without checks, a shifted mount flagged,
"no check shots", a shot too far in time, a rod reading on BM1 holding the
datum and closing the setup, site round trip, a v2 project without
`control`, format 3, name validation, the API, the field-sheet box);
`test_project.py::test_resaving_stamps_the_current_format` now expects 3;
`test_web_browser.py::test_control_marks_are_added_from_the_datum_menu`.

*What.*

- **Site.** `Site` gains `control: list[ControlMark]`, each with a name, an
  optional held local elevation in ft, and a note. Bump `project.VERSION` to
  3; older builds then refuse with their existing "newer version" message.
  `Site.from_dict` must accept files with no `control` key.
- **UI.** A "Datum ▸ Control marks…" dialog adds, edits and removes marks.
  Keep it small.
- **Check shots.** A SW Maps record whose station is a mark name is a check
  shot. It is assigned to the track session nearest in time within the same
  export, within 60 min.
- **Ties.** Two sessions with check shots on the same mark give a direct
  observation, `off_b − off_a = z_b − z_a`. This works because the check
  shots are taken on the same fixed-height pole.
- **Report.** Per session: the start and end check residuals and the drift.
  Flag anything over 3 cm. Show a line in the Sessions panel, for example
  "checks: BM1 start +0.4 cm, end −0.8 cm" or "no check shots".
- **Merge report.** A session linked only through a mark is reconcilable.
- **Laser.** A rod reading on a mark enters the network as that station, so
  the benchmark can be `BM1`.

*Workflow to document* (owner's answer 6). A README section "Control marks
and check shots" and a field-sheet header box:

- Set 2–3 permanent marks, such as mag nails or rebar, outside the mowed area.
- Shoot the mark with the fixed-height pole at the **start and end of every
  outing**, as record `BM1`.
- Read the rod on `BM1` from **every laser setup**, at open and close.
- Tie one mark to the Revit model's garage slab or door threshold, once.

*Why.* The NGS *User Guidelines for Single Base Real Time GNSS Positioning*
call for occupying a known point at the start and end of each session. Today
only crossovers tie sessions (`vertical.py:283-297`). The README's "or shoot a
permanent benchmark" has no code path behind it.

*Verify.*
- The synthetic "Elsewhere" outing, plus two check shots, becomes
  reconcilable with its offset recovered within 1 cm.
- A shifted mount is flagged.
- A project without `control` still loads.

*Breaking?* Project format v3. Approved.

**A5 — Tie transects** ✅
- **Done:** `plan.py` purpose group "guide" = ("tie transect",),
  `GUIDE_PURPOSES`, `is_guide`; guide points are left out of `to_frame`
  (never in the network) and `coverage()`. `plan_edit.tie_transect_runs`
  scores rows/columns of 0.5 m cells by the number of sessions covering
  each cell, clips each to its longest run (gaps ≤ 2 m, ≥ 5 m), and picks
  the two best per direction at least max(5 m, a third of the lot) apart;
  `add_tie_transects` replaces any existing transect lines (`tie-EW-1`...
  `tie-NS-2`, kind "transect", two vertices each). `POST
  /api/plan/transects` uses the filtered points of every session. UI:
  Plan ▸ Add tie transects; the table filters out guide points; the map
  draws transects in purple, dotted, labelled "walk first"; the table's
  purpose list leaves "tie transect" out. Field sheet: transects drawn and
  labelled "walk or mow first", in the legend, with no marker and no row.
  Note: with every point deleted, `Plan.next_number` restarts at 1, so a
  re-run can reuse the removed guide points' numbers (harmless, since guide
  points never have rows). Tests: `test_plan_edit.py::test_tie_transects_run_over_covered_ground`,
  `::test_tie_transects_are_guides_not_shots`,
  `::test_adding_tie_transects_again_replaces_them`,
  `::test_an_outing_that_walks_only_the_transects_is_reconcilable`,
  `::test_the_field_sheet_draws_transects_without_rows`,
  `test_web_browser.py::test_tie_transects_are_drawn_but_not_listed`.
- **What:** Plan ▸ "Add tie transects". Add 2 E-W and 2 N-S lines placed
  through the best-covered ground of the loaded sessions (occupied 0.5 m
  cells), each clipped to its longest covered run (gaps ≤ 2 m allowed, length
  ≥ 5 m). They are plan lines of kind `transect` whose vertices use a new
  purpose, "tie transect", in a new group "guide". Guide points:
  - are drawn on the map and the field sheet ("walk or mow these first");
  - are **not** listed as rod rows;
  - never enter the level network.

  Files: `plan.py` (purpose group), `app/plan_edit.py` (generator),
  `server.py` route, `menus.js`, `fieldsheet.py`.
- **Why:** overlap between outings is luck today, and an outing without it
  cannot be recovered (`merge.py:131`).
- **Verify:** the lines fall inside coverage; a synthetic outing that walks
  only the transects is reconcilable.
- **Breaking?:** no.

**A1 — Laser terrain shots in the surface (and the Revit file)** ✅
- **Done:** `build_surface(fixed=None)` (off by default; established-findings
  calls unchanged) takes a frame of `e`, `n`, `z`: bins within
  `FIXED_RADIUS_M` 0.5 m are replaced by the points, and after smoothing
  `_honour` adds compact bumps (radius `FIXED_BUMP_M` 1 m, worked out over
  local windows) with amplitudes solved against the grid's own bilinear
  sampling, so `Surface.sample(e, n)` (new, with `Surface.grid_axes`)
  returns each shot's elevation exactly (~1e-12 mm on the synthetic lot).
  `AppState.laser_points()` gives one point per terrain station, only for a
  local model with a datum shift; it re-solves the level network from the
  rod readings when the model came from a project (the network is not
  stored). Preview, figure and export surfaces use it.
  `io/revit.append_laser_points` appends the shots (kind "laser") and drops
  bins within 0.5 m; the Revit and heightmap notices say how many; the solve
  notice says laser shots are not used in ellipsoidal/NAVD88 mode. README:
  a paragraph in "The vertical model". Tests: `test_surface_and_export.py`
  laser tests (exact at every shot, default off, row arithmetic, none in
  other modes, survives a reopen) and
  `test_web_api.py::test_the_revit_export_says_how_many_laser_shots_it_added`.
  **Needs a local real-data run** (findings use the default `fixed=None`).
- **What:** after a **local** solve, terrain rod shots (laser elevation from
  the level network) become fixed points:
  - In `build_surface` (new optional argument, **off by default**, so
    established-findings calls are unchanged), each replaces the GNSS bins
    within 0.5 m.
  - In the Revit export, they are appended, and track bins within 0.5 m are
    dropped. This is the minimal Revit part.

  In ellipsoidal or NAVD88 mode no laser points are used, and a notice says
  why. Files: `app/state.py`, `surface.py`, `web/server.py`, `app/report.py`,
  plus a README paragraph.
- **Why:** laser elevations are only used for the tie (`vertical.py:579`).
  The surface and export read only the track result (`server.py:843`), so
  planned swales, crowns and breaklines never reach a deliverable.
- **Verify:** the surface at each shot equals the laser elevation within
  1 mm; the Revit row count adds up; no laser rows in other modes.
- **Breaking?:** the Revit file gains rows. Formats are unchanged.

### UI

Evidence for these is in the review screenshots: synthetic export,
1500×900 and 1280×720.

**A7 — Spot IDs on the map and in the datum dialog** ✅
- **Done:** `views.spot_markers` returns objects (`x`, `y`, `station`,
  `kind`, `rod`, `date`), merging a FEATURE_POINTS copy with the layer copy
  that has the attributes. `map2d.js` labels squares with the station below
  0.12 m/px and adds kind, rod and date below 0.04 m/px (decluttered).
  `AppState.spot_choices()` → `tie_form(..., choices)` gives each station a
  label ("lawn · rod 45.5 in · 2026-08-27", or "2 readings"); the datum
  dialog's datalist shows them (`form.points` kept). Tests:
  `test_web_api.py::test_spot_markers_say_what_they_are`,
  `::test_the_datum_picker_describes_each_station`,
  `test_web_browser.py::test_spot_labels_appear_when_zoomed_in`.
- **What:** spot markers carry their station/ID, kind, rod reading and date
  (`views.spot_markers` returns objects). `map2d.js` shows zoom-dependent
  labels. The datum picker (`dialogs.js`) lists each station with its kind,
  rod reading and date.
- **Why:** the dialog asks for an ID, but the map's red squares carry none.
- **Verify:** browser test.
- **Breaking?:** no.

**C13 — Status strip above the map** ✅
- **Done:** `report.status_strip(state)` → segments `{text, level}`: the
  datum (from `height_label`; raw/ellipsoidal "warn", tied "ok"), the model
  mode, the sessions and whether every one has a solved offset, % measured
  and `filter_summary`. In the snapshot as `strip`; `#status-strip` sits
  between the terrain toolbar and the map (`#view-plan` gained a grid row),
  rendered by `main.js`. The basemap Alignment group is a `<details>`
  folded until the first basemap arrives (opened then, folded again if they
  are cleared). The strip is always one line high (clipped, never wrapped
  or hidden): a first version that hid itself when empty and wrapped made
  two map-interaction browser tests flaky, because each change of its
  height resized the map a beat before OpenLayers knew. Tests: `test_web_api.py::test_the_status_strip_says_what_the_heights_are`,
  `::test_the_strip_says_when_session_offsets_are_unsolved`,
  `test_web_browser.py::test_the_status_strip_sits_above_the_map`.
- **What:** one line above the map showing the datum (tied, local,
  ellipsoidal or raw), the model mode, the sessions and whether their offsets
  are solved, % measured, and the filter summary. The basemap "Alignment" box
  collapses until a basemap exists. Files: `index.html`, `app.css`,
  `main.js`, `report.py` (strip text).
- **Why:** the datum state is in the last right-hand panel, below the fold
  at both sizes.
- **Verify:** browser test.
- **Breaking?:** no.

**C14 — Points drawn under the surface's weight** ✅
- **Done:** `map2d.js`: with the surface shown, points draw at radius 0.75
  (1.5 px) on a layer at 35% opacity; alone, radius 1.4 and full opacity
  (the style cache is keyed by radius too). Switching the shade to slope
  turns points off unless the user turned them on there before: the choice
  is remembered per shade mode in localStorage (`points-<mode>`). Test:
  `test_web_browser.py::test_points_give_way_to_the_surface`.
- **What:** with the surface on, points draw at about 1.5 px and 35% opacity.
  In slope mode they are off by default, and the choice is remembered.
  File: `map2d.js`.
- **Why:** about 50k dots hide the surface. Slope mode stacks points, arrows
  and squares.
- **Verify:** browser test.
- **Breaking?:** no.

**C15 — Success reports stop being modal** ✅
- **Done:** `dialogs.showToast` (bottom-right `#toasts`, at most 3; title,
  first line, a "Details" toggle for the full text, actions as buttons or
  links, a dismiss button); `api.act` sends `info` notices there and keeps
  `warning`/`error` modal. So nothing that needs acting on becomes a
  toast: `report.solve_level(model)` makes a solve "warning" when a session
  is unresolved, a setup is flagged or a check shot is over tolerance (all
  three solve routes use it), and `MergeReport.attention` (set by A2 fill
  conflicts) makes the open and merge notices warnings. Tests:
  `test_web_browser.py::test_success_is_a_toast_and_trouble_is_a_dialog`,
  the A5 browser test now reads its toast,
  `test_web_api.py::test_a_solve_with_something_to_act_on_is_a_warning`.
- **What:** fetch, solve and export results go to a dismissible side panel
  or toast with details. Warnings and errors stay modal. Files: `api.js`,
  `dialogs.js`, `app.css`.
- **Why:** the typical session is 15 actions with 4 modal dialogs, 2 of
  which are only success reports.
- **Verify:** browser tests. Update any tests that click through those
  dialogs.
- **Breaking?:** no.

**C16 — Consistent units** ✅
- **Done:** `views.surface_grid(..., datum=)` sends x, y and z in feet from
  the local origin (all three, so the exaggeration stays true) plus `units`
  and `datum` (short `height_label`); `view3d.js` titles the axes and the
  colour bar "east (ft)", "elevation (ft, <datum>)" and hovers in ft. The
  Layers panel uses the new `views.layer_summary` (heights in ft);
  `PointSet.describe` is unchanged. QC residuals stay in cm. Tests:
  `test_web_api.py::test_the_3d_grid_and_the_layers_are_in_feet`,
  `test_web_browser.py::test_the_3d_view_is_in_feet_on_a_named_datum`; the
  real-data `test_the_3d_grid_pairs_rows_with_northings` now expects feet.
- **What:** the 3D axes and colour bar show ft, with the datum named. Layer
  summaries show ft. QC keeps cm for residuals. Files: `view3d.js`,
  `views.surface_grid`, and the layer payload in `server.py`. Don't change
  `PointSet.describe`, which tests and reports use.
- **Why:** the 2D legend is in ft, but 3D is in m with no datum.
- **Verify:** payload test.
- **Breaking?:** no.

**C17 — Legend** ✅
- **Done:** `map2d.js` legend: a head (title with units and datum, and a
  fold toggle remembered as `legend-open`), five tick labels under the bar
  (slope: 0 ... "≥ max"), the existing notes, and a key of the marker
  shapes actually on the map (red square = SW Maps shot, ring by purpose
  group = plan shot, orange triangle = laser setup). It starts folded where
  the map is under 700 px wide (1280 px windows), and `resetView` frames
  the survey with a right margin the width of the legend, so it no longer
  sits on the data. It now takes pointer events (for the toggle). Tests:
  `test_web_browser.py::test_the_legend_has_ticks_a_key_and_folds`,
  `::test_at_1280_the_legend_keeps_off_the_survey`.
- **What:** 5 tick labels, units and datum, a key for marker shapes (squares
  are SW Maps shots, circles are plan shots by group, the triangle is a laser
  setup). It is collapsible and moves off the data at narrow widths. Files:
  `map2d.js`, `app.css`.
- **Why:** end labels only, and it covers the survey at 1280 px.
- **Verify:** browser test at 1280×720.
- **Breaking?:** no.

**C18 — Contrast, colour tokens, type and spacing scale** ✅
- **Done:** `--muted` #5f6771 (4.93:1 on `--bg`, ≥4.9 on white and tinted
  rows); `--border-strong` #878f98 (3.27:1 on white, 3.08 on buttons); the
  drainage colour `--arrow` #0e7c8c (4.91:1), which `map2d.js` now reads
  from the stylesheet for the map arrows too. A switched-off filter stage
  is muted and italic instead of opacity 0.45; a hidden session gets a
  dashed outline and the page tint instead of opacity 0.55. All ten listed
  colours are `:root` tokens (plus the legend's pens); every `font-size:
  11px` is 12 px, and the canvas labels in `map2d.js`/`planmap.js` too;
  padding, margin and gap are on a 4 px scale (2 px as a half step; tokens
  `--s1`..`--s4`). Dialogs name their severity in a word ("Warning",
  "Error", "Note", "Confirm"; confirmations now use a `confirm` level).
  Tests: `test_web_browser.py::test_text_and_borders_meet_wcag_aa` (computes
  the ratios in the page and also requires no fading by opacity - it fails
  on the old stylesheet with "faded to 0.45"),
  `::test_dialogs_say_their_severity_in_words`.
- **What:** raise the pairs that fail WCAG AA (4.5:1 text, 3:1 component
  borders) in `app.css`, plus a small change in `dialogs.js`:

  | Element | Now | Needs |
  |---|---|---|
  | Filter-stage-off text | 2.79:1 | 4.5:1 |
  | Hidden-session numbers | 2.16:1 | 4.5:1 |
  | Arrow glyph `#1fb4c8` on white | 2.49:1 | 4.5:1 |
  | Muted text on `--bg` | 4.26:1 | 4.5:1 |
  | Input borders `#b9bfc6` | 1.85:1 | 3:1 |

  Also:
  - Dialog severity gets a word as well as the dot colour.
  - Every colour becomes a `:root` token. There are 10 hard-coded today:
    `#f7f8fa`, `#e3e7ec`, `#194f8c`, `#bcd4f4`, `#a9c6ee`, `#fafbfc`,
    `#d99a00`, `#b88a1b`, `#1fb4c8`, `#f7f7f7`.
  - Spacing moves to a 4 px scale.
  - The minimum text size becomes 12 px.
- **Verify:** a contrast check in the browser tests.
- **Breaking?:** no.

**C19 — Rod column second, in the table and the field sheet** ✅
- **Done:** the table is `# · Rod · Purpose · Setup · Fixed · Method · Line
  · Notes`, with `#` and Rod both frozen (so Rod never scrolls out of the
  dock) and slightly narrower columns; `plan_edit.FIELDS` follows. The
  field sheet is `# · Rod (in) · Purpose · Setup · Fixed · Plan E,N · GNSS
  pt / E,N · Tie A · Tie B · Line · Notes`: the table's order, with the
  position boxes where Method sits. Tests:
  `test_web_browser.py::test_rod_is_the_second_column_and_stays_in_view`,
  `test_plan.py::test_the_field_sheet_puts_the_rod_reading_second`.
- **What:** order the columns `# · Rod · Purpose · Setup · Fixed · Method ·
  Line · Notes`, compact enough that Rod never scrolls out of a 390 px dock.
  Change the printed field sheet to match. Files: `panels/plan.js`,
  `io/fieldsheet.py`.
- **Why:** only 5 of 8 columns are visible, and "Rod (in" is clipped. Rod is
  the only required field.
- **Verify:** browser test.
- **Breaking?:** the printed sheet layout changes.

**C20 — Readable filter stack** ✅
- **Done:** `chain_edit.PLAIN_NAMES` names every registered stage;
  `chain_payload["kinds"]` is now a list of `{kind, name}` sorted by name,
  and the Add list shows the names (a stage's card has its name as a
  tooltip). The fix stage's `values` param carries `choices` (fixed = 4,
  float = 5) and the page draws two checkboxes, sending the whole list back
  (other codes ride along); `edit_stage` stores a list as a tuple of ints.
  The saved chain JSON is unchanged (`"values": [4, 5]`). Tests:
  `test_web_api.py::test_the_filter_stack_reads_in_words`,
  `test_web_browser.py::test_the_fix_filter_is_two_checkboxes`.
- **What:** plain names in the Add list. The fix filter becomes two
  checkboxes, "fixed" and "float", instead of the text `[4]`. The saved chain
  JSON is unchanged. Files: `panels/chain.js`, `app/chain_edit.py`.
- **Verify:** browser test.
- **Breaking?:** no.

**C21 — Grouped fetch errors and a short connect timeout** ✅
- **Done:** `imagery.TIMEOUT = (10.0, 60.0)` and `vector.TIMEOUT = (10.0,
  45.0)` (connect, read). `fetch_all_imagery` keeps the whole message;
  `FetchReport.failure_groups()` groups failures whose messages match once
  URLs, hosts and object addresses are taken out, and `describe()` prints
  one line per group (the first message, clipped only past 600 characters
  and at a word) with the providers named under it. In the toast (C15) the
  summary line is the counts and the groups are behind Details. Tests:
  `test_project.py::test_identical_failures_are_grouped`,
  `::test_a_black_holed_service_fails_in_seconds_not_minutes`.
- **What:** identical failures are grouped into one line, with details on
  expand. Use `timeout=(10, 60)` (connect, read). Files: `io/imagery.py`
  (and `io/vector.py`), `app/state.py` `FetchReport.describe`.
- **Why:** 10 near-identical raw proxy errors cut mid-word. A black-holed
  network could stall the UI for up to 10 minutes, since each provider waits
  60 s under the global lock.
- **Verify:** a stub-provider test.
- **Breaking?:** no.

**C22 — File dialog** ✅
- **Done:** `dialogs.fileDialog`: a header row (Name, Modified, Size) whose
  Name and Modified buttons sort (by name, or newest first; folders always
  lead), a Modified column from `mtime` (local "YYYY-MM-DD HH:MM"), and a
  keyboard on the list: Up/Down/PageUp/PageDown move the active row (a file
  becomes the chosen name), Home/End, Enter opens a folder or accepts a
  file, Backspace goes up; `aria-activedescendant` and `aria-selected` are
  kept. Test: `test_web_browser.py::test_the_file_dialog_sorts_by_date_and_answers_the_keyboard`.
- **What:** a Modified column (`mtime` is already sent, `files.py:65`),
  sorting by name or date, and arrow keys plus Enter in the list.
  File: `dialogs.js`.
- **Verify:** browser test.
- **Breaking?:** no.

**C23 — Keyboard menus and labelled fields** ✅
- **Done:** `menus.js` handles keys on the menu bar (`onMenuKey`):
  Down/Enter/Space open a menu at its first item; Up/Down/Home/End move;
  Right opens a submenu or moves to the next menu; Left closes a submenu or
  moves to the previous one; Escape backs out one level and returns focus;
  a button item still activates itself on Enter. The datum dialog's labels
  have `for`/`id` (`datum-point`, `datum-elev`, `datum-note`,
  `datum-frame`, `datum-tied`). Test:
  `test_web_browser.py::test_the_menus_work_from_the_keyboard`.
- **What:** Arrow, Enter and Esc work in menus and submenus, which today open
  on hover only (`menus.js:111`). Dialog labels are linked to their inputs
  with `for`/`id` (`dialogs.js:229`).
- **Verify:** browser test.
- **Breaking?:** no.

**A8 — Start panel and recent files** ✅
- **Done:** `web/files.py` `remember_recent` / `recent_files` keep up to 8
  exports and projects, newest first, in `<cache>/recent.json` (the cache
  folder is git-ignored); files gone from disk are not listed, and a failed
  write never fails the action. The server remembers every open export, add
  export, open project and save project, and the snapshot carries `recent`
  only while nothing is loaded. `#start` in the plan view (`main.js`
  `renderStart`): Open export…, Open project…, and the recent files (kind,
  name, folder), each opening with one click; hidden once data is loaded.
  Tests: `test_web_api.py::test_opened_files_are_remembered_for_the_start_panel`,
  `test_web_browser.py::test_an_empty_page_offers_a_way_in`.
- **What:** with nothing loaded, the map area offers Open export, Open
  project, and up to 8 recent files. The list is stored in the git-ignored
  cache folder. Files: `server.py`, `main.js`, `app.css`.
- **Why:** the empty state has no obvious first action, and opening a file
  means navigating folders. With C15, the typical session drops from 15
  actions to about 10.
- **Verify:** browser test.
- **Breaking?:** no.

**C29 — Solve report wording** ✅
- **Done:** `report.solve_notice` no longer starts its warnings from
  `model.notes` (which `describe()` prints); `VerticalModel.describe` gives
  the tie offset as "+231.507 m (+759.537 ft)", keeping scatter and max
  deviation in cm. Tests: `test_vertical.py::test_the_solve_report_says_each_note_once`,
  `::test_the_tie_offset_is_in_metres_and_feet`.
- **What:** notes are printed once (`report.py:69` repeats `model.notes`,
  which `describe()` already prints). The tie offset is shown in m and ft,
  not "+23150.7 cm" (`vertical.py:488`).
- **Verify:** test.
- **Breaking?:** no.

### Surface, hygiene and docs

**C10 — World file on the grid** ✅
- **Done:** `Surface.world_file` writes the node step, width / (n - 1)
  (and height / (n - 1)), with the first pixel centre at (`xmin`, `ymax`).
  `Surface.px`, the mask and the measured fraction are untouched. The
  real-data `test_world_file_points_at_the_north_west_pixel_centre` now
  checks against `terrain.grid_axes`; new synthetic
  `test_surface_and_export.py::test_the_world_file_places_every_pixel_on_its_grid_node`
  (fails before the fix). **Needs a local real-data run** for the updated
  pinned test.
- **What:** grid nodes sit at `linspace(min, max, n)`, but the world file
  assumes a pixel size of width/n plus half a pixel (`surface.py:102, 169,
  172`). Write the world file from the node spacing, width/(n−1), with the
  first centre at `xmin` / `ymax`. Keep `Surface.px` and the mask calculation
  as they are, so the 42% measured finding cannot move. Update
  `test_world_file_points_at_the_north_west_pixel_centre`, which is not a
  findings test, to check against `terrain.grid_axes`.
- **Why:** E3 — errors of ±1.97 cm at the edges (1024 px over 40 m).
- **Breaking?:** exported rasters shift by up to half a pixel. That is the
  correction.

**C24 — Surface smoothing sized in metres** ✅
- **Done:** `surface.PREVIEW_SIZE` (320, now imported by `app/state.py`
  instead of defined there). `build_surface` scales the site's
  `median_size` (rounded, kept odd) and `gaussian_sigma` by
  (width / PREVIEW_SIZE) / px, so the preview is unchanged and every other
  raster smooths the same ground. On the synthetic lot, export vs preview
  at 400 random points: median 1.67 mm / p90 4.37 mm before, 0.08 / 0.21 mm
  after; figure vs preview 0.91 / 2.26 before, 0.06 / 0.19 after. Export
  build 1.3 s. The mask (and measured fraction) is computed before
  smoothing, so it cannot move. Test:
  `test_surface_and_export.py::test_smoothing_covers_the_same_ground_at_every_size`.
  **Needs a local real-data run** to confirm the findings.
- **What:** treat the site's `median_size` and `gaussian_sigma` as sizes at
  the **preview's** pixel size, and scale them to each raster's pixel size.
  The preview is unchanged; the export and figures now match it. No
  site-file change. File: `surface.py`.
- **Why:** R13 — σ is about 25 cm in the preview, 8 cm in the export and
  16 cm in figures.
- **Verify:** heights at the same ground points agree within about 1 mm
  across sizes. The measured fraction is untouched, since it depends only on
  the mask; confirm with a local findings run.
- **Breaking?:** exported heightmaps become smoother. Approved.

**C26 — .gitignore covers exports** ✅
- **Done:** added `*.pgw`, `heightmap_*`, `revit_points*.csv`,
  `slope_map*.png`, `contours_*.png`. `git check-ignore` confirms each of
  the export names the server offers (including in a subfolder), and `git
  ls-files -ci --exclude-standard` lists no tracked file caught by them.
- **What:** add `*.pgw`, `heightmap_*`, `revit_points*.csv`, `slope_map*.png`
  and `contours_*.png`.
- **Why:** the world file holds absolute UTM coordinates (`raster.py:57`),
  and these exports aren't ignored if saved into the repo.
- **Verify:** `git check-ignore`.
- **Breaking?:** no.

**C27 — Atomic saves** ✅
- **Done:** new `gpsrtk/fileio.py` `write_text_atomic(path, text)`: a
  temporary file beside the target, `os.replace`, one retry after
  `RETRY_AFTER_S` (0.5 s) on `PermissionError`, and the temporary file
  removed if anything fails. Used by `Project.save`, `Plan.save` and
  `Site.save`. Tests: `test_project.py::test_a_save_that_fails_leaves_the_old_file_whole`,
  `::test_a_save_retries_once_past_a_sync_lock`.
- **What:** write to a temporary file in the same folder, then `os.replace`,
  with one retry on `PermissionError` (OneDrive locks). Applies to
  `project.py:119`, `plan.py:433` and `site.save`.
- **Verify:** test.
- **Breaking?:** no.

**C28 — Server hardening** ✅
- **Done:** actions compare the whole Origin (scheme, host name, port, with
  default ports filled in) against the request's own scheme and Host, and
  the host name must still be loopback. Every response gets
  `X-Content-Type-Options: nosniff`; the page gets `PAGE_CSP` (`default-src
  'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src
  'self' data: blob:; worker-src 'self' blob:; connect-src 'self';
  object-src 'none'; base-uri 'none'; frame-ancestors 'none'`); downloaded
  HTML (the field sheet) gets `default-src 'none'; style-src
  'unsafe-inline'; img-src data:`. Plotly's 3D bundle turned out not to
  need `'unsafe-eval'`: it renders with no violation logged. Tests:
  `test_web_api.py::test_another_port_on_this_machine_is_a_foreign_origin`,
  `::test_the_page_and_its_downloads_carry_security_headers`
  (`test_actions_from_its_own_page_are_accepted` now sends the origin the
  test client really is, `http://127.0.0.1`),
  `test_web_browser.py::test_the_page_runs_clean_under_its_content_security_policy`.
- **What:** compare the full Origin (scheme, host and port) against the
  request's host, not just the hostname (`server.py:309`). Add:
  - a `Content-Security-Policy` on the page (`script-src 'self'`). Plotly's
    WebGL may need `'unsafe-eval'`, so check with the browser tests;
  - `default-src 'none'; style-src 'unsafe-inline'; img-src data:` on
    downloaded HTML such as the field sheet (`server.py:989`);
  - `X-Content-Type-Options: nosniff`.
- **Verify:** API tests (a foreign port is refused; the headers are present)
  and the browser tests.
- **Breaking?:** no.

**C30 — Hide the do-nothing surface-residual stage** ✅
- **Done:** `chain_edit.HIDDEN_KINDS = {"surface_residual"}` keeps it out of
  `chain_payload["kinds"]` only; it stays registered, so saved chains using
  it load and run. Test:
  `test_web_api.py::test_the_do_nothing_residual_stage_is_not_offered`.
- **What:** remove `surface_residual` from the Add list only
  (`chain_edit.chain_payload` `kinds`). Saved chains that use it still load.
- **Why:** nothing ever calls `set_reference`, so the stage does nothing
  (`threshold.py:129-139`).
- **Breaking?:** no.

**C31 — Network tests stay off under any `-m`** ✅
- **Done:** `tests/conftest.py` `pytest_collection_modifyitems` skips every
  test with the `network` marker unless the `-m` expression contains
  "network" or `YARDSURVEY_NETWORK=1`. `-m "not browser"` now reports the 5
  network tests as skipped; the default still deselects them; `-m network`
  selects them. Test (runs pytest in a subprocess):
  `test_imagery.py::test_network_tests_stay_off_under_any_other_mark_expression`.
- **What:** a `tests/conftest.py` `pytest_collection_modifyitems` hook skips
  `network` tests unless the `-m` expression names `network`, or
  `YARDSURVEY_NETWORK=1` is set.
- **Why:** R24.
- **Breaking?:** no.

**C32 — CLAUDE.md: rover is PX4, plus the A9 note** ✅
- **Done:** "Open work" now describes the PX4 rover (Cube Orange, Raspberry
  Pi 5, LG290P with ANN-MB2, RTK through QGroundControl's NTRIP client) in
  place of "Possible ArduPilot rover…", and carries the deferred A9 note
  (what it would do, what it waits on, the ulog dependency question, and
  `*.plan` in .gitignore). Doc only.
- **What:** in "Open work", replace "Possible ArduPilot rover…" with the PX4
  build: Cube Orange, Raspberry Pi 5, LG290P with ANN-MB2, RTK corrections
  through QGroundControl's NTRIP client. Add the deferred A9 note (section 7).
- **Breaking?:** no. Doc only.

---

## 6. Review findings (reference)

Short versions of the review's findings, for context.

| ID | Severity | Where | Finding | Item |
|---|---|---|---|---|
| R1 | bug | vertical.py:534, plan.py:380 | Only "lawn" rod shots enter the level network | C1 |
| R2 | bug | vertical.py:183, plan.py:375 | Plan numbers and SW Maps IDs collide | C2 |
| R3 | bug | report.py:115, views.py:378, revit.py:88 | Ellipsoidal/NAVD88 heights labelled "local datum" | C3 |
| R4 | bug | state.py:215 | A non-survey file emptied the session | C4 ✅ |
| R5 | bug | threshold.py:113 | One NaN height dropped every point | C5 ✅ |
| R6 | bug | merge.py:37 vs vertical.py:505 | Merge report and solve disagree on links | C6 |
| R7 | risk | plan_edit.py:177 | Any number accepted as inches | C7 |
| R8 | risk | select.py:53 | `type` matched exactly | C8 |
| R9 | risk | swmaps.py:148 | Layer names tied to the zip's name | C9 ✅ |
| R10 | bug | surface.py:102/172 | World file off by half a pixel | C10 |
| R11 | bug | swmaps_project.py:254 | Daylight-saving time wrong for `.swmz` | C11 |
| R12 | risk | pointset.py:42 | Instrument Ht ignored | C12 |
| R13 | risk | surface.py:160 | Smoothing sized in pixels | C24 |
| R14 | risk | swmaps.py:93 | Sessions split by date only | C25 |
| R15 | risk | qc.py:37 | Pair count explodes when stationary at 10 Hz | O1 |
| R16 | risk | adjust.py:220 | Over-confident standard errors | O1 |
| R17 | cleanup | report.py:97, sessions.py:45 | Crossovers found 3× per change | O2 |
| R18 | risk | filters/base.py:106 | Cache keyed on `id()` | O3 |
| R19 | risk | .gitignore | Exports with coordinates not ignored | C26 |
| R20 | risk | project.py:119 | Writes not atomic | C27 |
| R21 | risk | server.py:309/989 | Origin checked without port; no CSP | C28 |
| R22 | cleanup | report.py:69 | Solve report duplicated notes; odd units | C29 |
| R23 | cleanup | threshold.py:129 | Surface-residual stage does nothing | C30 |
| R24 | risk | pyproject.toml:40 | `-m` re-enables network tests | C31 |
| R25 | risk | imagery.py:35 | 60 s × 10 providers under the lock | C21 |
| R26 | cleanup | plan.py:53 | `SIGMA_M` comment claims unused behaviour | none (note only) |
| R27 | cleanup | server.py:827 | Heightmap always tagged `_fixed` | C3 |
| R28 | risk | state.py:191 | Duplicates detected by path only | A6 |

---

## 7. Deferred: A9, rover mission export

The owner wants this later; do not implement it now.

- **What:** Export ▸ "Rover mission". Plan lines and transects become a
  QGroundControl `.plan` file (JSON, WGS84 waypoints, cruise speed) for the
  PX4 tracked rover. Add `*.plan` to .gitignore, because the file carries
  lat/lon.
- **Where:** a new `io/mission.py`, a server route, and a menu item.
- **Pairs with:** A5 (tie transects).
- **Open question:** what the rover will log (PX4 ulog, NMEA from the Pi, or
  SW Maps on a phone). The answer decides whether an import path comes later.
  Reading ulog would need a new dependency, so ask first.

---

## 8. New findings (not fixed)

Things noticed while implementing, recorded rather than fixed, per the
ground rules.

- (none yet)
