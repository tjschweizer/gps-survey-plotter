// What the menus and buttons do. Each opens whatever dialog it needs, then
// asks the server; the server decides what the action means.

import { store, setPlanMode } from "./store.js";
import { act, getJSON } from "./api.js";
import { showNotice, confirmDialog, fileDialog, datumDialog, controlMarksDialog,
         FILTERS } from "./dialogs.js";
import * as map2d from "./map2d.js";
import * as view3d from "./view3d.js";
import { cancelLine } from "./planmap.js";
import { h } from "./dom.js";

const state = () => store.state;

function requireData() {
  if (state()?.has_data) return true;
  showNotice({ title: "No data", text: "Load an export first." });
  return false;
}

export function currentTab() {
  return document.querySelector('#tabs [aria-selected="true"]')?.dataset.tab === "3d" ? 1 : 0;
}

// The same keys the desktop app saved, so older project files still restore
// the view they were saved with.
export function viewSettings() {
  return { ...map2d.viewSettings(), tab: currentTab(),
           vertical_exaggeration: view3d.exaggeration() };
}

// --- File -----------------------------------------------------------------------

export async function openExport() {
  const path = await fileDialog({ title: "Open survey export", filters: FILTERS.export });
  if (path) await act("/api/export/open", { path }, { busy: "Reading export…" });
}

export async function addExport() {
  if (!state()?.layers.length) return openExport();
  const path = await fileDialog({ title: "Add survey export", filters: FILTERS.export });
  if (path) await act("/api/export/add", { path }, { busy: "Merging export…" });
}

export async function openProject() {
  const path = await fileDialog({ title: "Open project", filters: FILTERS.project });
  if (path) await act("/api/project/open", { path }, { busy: "Opening project…" });
}

export async function saveProject(ask = false) {
  const s = state();
  if (!s?.has_data) {
    await showNotice({ title: "Nothing to save", text: "Load data first." });
    return;
  }
  let path = s.project_path;
  if (ask || !path) {
    path = await fileDialog({ title: "Save project", mode: "save", filters: FILTERS.project,
                              suggested: s.suggested_project, suffix: ".yardproj" });
    if (!path) return;
  }
  await act("/api/project/save", { path, view: viewSettings() }, { busy: "Saving project…" });
}

export async function quit() {
  if (!(await confirmDialog("Quit", "Stop Yard Survey? Anything not saved is lost.",
                            { yes: "Quit", no: "Cancel" }))) return;
  await act("/api/quit", {}, { quiet: true });
  document.body.replaceChildren(h("div", { class: "stopped" },
    "Yard Survey has stopped. You can close this tab."));
}

// --- Data -------------------------------------------------------------------------

export const fetchAllImagery = () =>
  requireData() && act("/api/imagery/fetch_all", {}, { busy: "Fetching basemaps…" });

export const fetchImagery = (name) =>
  requireData() && act("/api/imagery/fetch", { name }, { busy: `Fetching ${name}…` });

export async function loadImageryFile() {
  if (!requireData()) return;
  const path = await fileDialog({ title: "Open georeferenced image", filters: FILTERS.raster });
  if (path) await act("/api/imagery/load_file", { path }, { busy: "Warping imagery…" });
}

export const clearImagery = () => act("/api/imagery/clear");

export const fetchVectors = (name) =>
  requireData() && act("/api/vectors/fetch", { name }, { busy: `Fetching ${name}…` });

export const clearVectors = () => act("/api/vectors/clear");

export const checkServices = () =>
  act("/api/services/check", {}, { busy: "Checking services…" });

export const showSessions = () =>
  requireData() && act("/api/sessions", {}, { busy: "Measuring overlap…" });

// --- Datum -------------------------------------------------------------------------

export async function datumTie() {
  const form = await getJSON("/api/datum");
  const values = await datumDialog(form);
  if (values) await act("/api/datum", values, { busy: "Tying the datum…" });
}

export async function controlMarks() {
  const form = await getJSON("/api/control");
  const marks = await controlMarksDialog(form);
  if (marks) await act("/api/control", { marks }, { busy: "Saving control marks…" });
}

export const solveVertical = (mode) =>
  requireData() && act("/api/vertical/solve", { mode }, { busy: `Solving vertical model (${mode})…` });

export const clearVertical = () => act("/api/vertical/clear");

// --- Plan -----------------------------------------------------------------------------

export function setMode(mode) {
  if (mode === "navigate") cancelLine();
  setPlanMode(mode);
}

export const writeFieldSheet = () =>
  act("/api/plan/fieldsheet", {}, { busy: "Rendering field sheet…" });

export const addTieTransects = () =>
  requireData() && act("/api/plan/transects", {}, { busy: "Placing tie transects…" });

export async function openPlan() {
  const path = await fileDialog({ title: "Open shot plan", filters: FILTERS.plan });
  if (path) await act("/api/plan/open", { path }, { busy: "Opening plan…" });
}

export async function savePlan() {
  const dir = state()?.suggested_project;
  const suggested = dir ? dir.replace(/[^\\/]*$/, "shot_plan.yardplan") : "shot_plan.yardplan";
  const path = await fileDialog({ title: "Save shot plan", mode: "save", filters: FILTERS.plan,
                                  suggested, suffix: ".yardplan" });
  if (path) await act("/api/plan/save", { path });
}

// --- Export and View --------------------------------------------------------------------

export const exportHeightmap = () =>
  requireData() && act("/api/export/heightmap", {}, { busy: "Building full-resolution surface…" });

export const exportRevit = () =>
  requireData() && act("/api/export/revit", {}, { busy: "Writing Revit points…" });

// The printed maps follow what the plan view is showing: the same slope
// scale, the same contour interval.
export const exportSlopeMap = () =>
  requireData() && act("/api/export/slope_map",
    { slope_max: map2d.viewSettings().slope_max, spacing_m: 1.5 },
    { busy: "Drawing the slope map…" });

export const exportContourMap = () =>
  requireData() && act("/api/export/contour_map",
    { interval_cm: map2d.viewSettings().contour_interval_cm },
    { busy: "Drawing the contour map…" });

export const resetView2D = () => map2d.resetView();
export const resetView3D = () => view3d.resetView();
