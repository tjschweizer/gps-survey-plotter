// Shot plan panel: the table you type the paper sheet into.
//
// Columns mirror the printed sheet, in the same order, so transcribing is a
// straight read across rather than a hunt. The rod reading is the only field
// that must be filled in; everything else has a working default.
//
// Position method gets its own column because it decides how much the point
// can be trusted horizontally, and the methods that need extra numbers -
// taped distances, station and offset, a typed-in coordinate - reveal an
// editor underneath rather than trying to cram them into cells.
//
// The rules about what an edit means live on the server; a refused edit comes
// back as a dialog and the cell goes back to what it was.

import { h, clear } from "../dom.js";
import { store, subscribe, changed, planMode, setPlanMode, onPlanMode,
         selection, setSelection, onSelection, requests } from "../store.js";
import { act } from "../api.js";
import { writeFieldSheet } from "../actions.js";

const body = document.querySelector("#panel-plan .body");
const plan = () => store.state?.plan;

// --- tools --------------------------------------------------------------------

// Exclusive, so exactly one tool is active and the active one is obvious.
// Clicking the tool you are already in drops back to Navigate, which is the
// only way to put a tool down without hunting for it.
const TOOLS = [
  ["+ Point", "add_point", "Click on the map to place a shot"],
  ["+ Line", "add_line", "Click vertices; double-click to finish"],
  ["+ Laser", "add_setup", "Where the laser will stand"],
  ["Navigate", "navigate", "Pan, zoom and drag markers. Esc returns here."],
];
const toolButtons = new Map();
const tools = h("div", { class: "tools", role: "toolbar", "aria-label": "Plan tools" },
  TOOLS.map(([label, mode, tip]) => {
    const b = h("button", {
      title: tip, "aria-pressed": String(mode === "navigate"), dataset: { mode },
      onclick: () => setPlanMode(mode !== "navigate" && planMode() === mode ? "navigate" : mode),
    }, label);
    toolButtons.set(mode, b);
    return b;
  }));

onPlanMode((mode) => {
  for (const [m, b] of toolButtons) b.setAttribute("aria-pressed", String(m === mode));
  const hint = plan()?.hints?.[mode];
  if (hint) document.getElementById("status").textContent = hint;
});

// --- the table ----------------------------------------------------------------

// A click on a cell of an already-selected row edits it; the first click on a
// row only selects. Without that the cells look permanently read-only.
const editable = (cell) => cell.getRow().isSelected();

const tableEl = h("div", { id: "plan-table", tabindex: "0" });
let built = false;
let restoring = false;
let queued = null;

const table = new Tabulator(tableEl, {
  data: [],
  index: "number",
  height: "270px",
  // Workable widths for every column, with Notes absorbing the slack; sizing
  // to contents left each column as narrow as its emptiest cell.
  layout: "fitDataStretch",
  selectableRows: true,
  selectableRowsRangeMode: "click",   // click one, Ctrl adds, Shift takes a range
  editTriggerEvent: "click",
  placeholder: "No shots planned. Use + Point or + Line, then click on the map.",
  columnDefaults: { headerSort: false, resizable: true, minWidth: 34 },
  columns: [
    // Frozen: the number is what links a row to its marker and to the sheet,
    // so it stays in view however far the table is scrolled sideways.
    { title: "#", field: "number", width: 42, resizable: false, frozen: true },
    { title: "Purpose", field: "purpose", width: 118, editable, editor: "list",
      editorParams: { valuesLookup: () => plan()?.purposes ?? [], autocomplete: true, listOnEmpty: true } },
    { title: "Line", field: "line", width: 80, editable, editor: "list",
      editorParams: { valuesLookup: () => ["", ...(plan()?.line_ids ?? [])] } },
    { title: "Setup", field: "setup", width: 58, editable, editor: "list",
      editorParams: { valuesLookup: () => plan()?.setup_choices ?? [""],
                      autocomplete: true, freetext: true, allowEmpty: true, listOnEmpty: true } },
    { title: "Rod (in)", field: "rod", width: 70, hozAlign: "right", editable, editor: "input" },
    { title: "Fixed", field: "fix", width: 52, editable, editor: "list",
      editorParams: { values: ["", "yes"] } },
    { title: "Method", field: "method", width: 104, editable, editor: "list",
      editorParams: { valuesLookup: () => plan()?.methods ?? [] } },
    { title: "Notes", field: "note", minWidth: 110, editable, editor: "input" },
  ],
  // Shot rows go green; the purpose group tints the rest, so control and
  // feature shots are distinguishable at a glance from the terrain they must
  // not be mixed into.
  rowFormatter: (row) => {
    const d = row.getData();
    const el = row.getElement();
    el.classList.toggle("done", d.has_reading);
    el.classList.toggle("feature", !d.has_reading && d.group === "feature");
    el.classList.toggle("control", !d.has_reading && d.group === "control");
    el.title = d.tooltip;
  },
});

table.on("tableBuilt", () => {
  built = true;
  if (queued) load(queued);
});

table.on("cellEdited", async (cell) => {
  const number = cell.getRow().getData().number;
  const reply = await act("/api/plan/cell", { number, field: cell.getField(), value: cell.getValue() });
  if (!reply) cell.restoreOldValue();
});

table.on("rowSelectionChanged", (data) => {
  if (restoring) return;
  setSelection(data.map((d) => d.number).sort((a, b) => a - b));
});

// Delete on the table deletes the selected rows. Scoped to the table rather
// than the page, so it cannot fire while the map has focus, where a silent
// deletion would be alarming.
tableEl.addEventListener("keydown", (e) => {
  if (e.key === "Delete" && !e.target.closest(".tabulator-editing")) {
    e.preventDefault();
    deleteSelected();
  }
});

function load(p) {
  if (!built) { queued = p; return; }
  queued = null;
  const keep = selection().filter((n) => p.points.some((pt) => pt.number === n));
  restoring = true;
  table.replaceData(p.points).then(() => {
    if (keep.length) table.selectRow(keep);
    restoring = false;
    setSelection(keep);
    showDetail();
  });
}

requests.select = (number) => {
  if (!built) return;
  restoring = true;
  table.deselectRow();
  table.selectRow(number);
  restoring = false;
  setSelection([number]);
  table.scrollToRow(number, "nearest", false).catch(() => {});
};

// --- the position editor --------------------------------------------------------

const hint = h("div", { class: "muted" }, "Select a row.");
const result = h("div", { class: "detail-result" });

const tieA = h("input", { type: "text", size: 4, "aria-label": "tie A point" });
const distA = h("input", { type: "number", step: "0.01", min: "0", "aria-label": "tie A distance (ft)" });
const tieB = h("input", { type: "text", size: 4, "aria-label": "tie B point" });
const distB = h("input", { type: "number", step: "0.01", min: "0", "aria-label": "tie B distance (ft)" });
const tapeRow = h("div", { class: "grid2" },
  h("label", {}, "from #"), h("div", { class: "row" }, tieA, distA, "ft"),
  h("label", {}, "from #"), h("div", { class: "row" }, tieB, distB, "ft"),
  h("span"), h("button", { onclick: () => positionAction("/api/plan/tape", (n) => ({
    number: n, tie_a: tieA.value, dist_a_ft: Number(distA.value || 0),
    tie_b: tieB.value, dist_b_ft: Number(distB.value || 0) })) }, "Solve"));
for (const el of [distA, distB]) el.style.width = "80px";

const refLine = h("select", { "aria-label": "along line" });
const station = h("input", { type: "number", step: "0.01", "aria-label": "station (ft)" });
const offset = h("input", { type: "number", step: "0.01", "aria-label": "offset (ft)" });
const offsetRow = h("div", { class: "grid2" },
  h("label", {}, "along line"), refLine,
  h("label", {}, "station (ft)"), station,
  h("label", {}, "offset (ft, + left)"), offset,
  h("span"), h("button", { onclick: () => positionAction("/api/plan/offset", (n) => ({
    number: n, line: refLine.value, station_ft: Number(station.value || 0),
    offset_ft: Number(offset.value || 0) })) }, "Apply"));

// A receiver read in the field gives lat/lon or UTM; a value read off the plan
// is in local feet. All three are accepted rather than forcing a conversion
// by hand, which is exactly where a sign or a digit gets lost.
const frame = h("select", { "aria-label": "coordinate frame" },
  ["local ft", "UTM m", "lat/lon"].map((f) => h("option", { value: f }, f)));
const xLabel = h("label", {}, "east (ft)");
const yLabel = h("label", {}, "north (ft)");
const coordX = h("input", { type: "text", spellcheck: false, "aria-label": "first coordinate" });
const coordY = h("input", { type: "text", spellcheck: false, "aria-label": "second coordinate" });
const clearCoords = h("button", { onclick: () => positionAction("/api/plan/coords/clear",
  (n) => ({ number: n })) }, "Clear, use planned");
const rtkRow = h("div", { class: "grid2" },
  h("label", {}, "frame"), frame,
  xLabel, coordX, yLabel, coordY,
  h("span"), h("div", { class: "row" },
    h("button", { onclick: () => positionAction("/api/plan/coords", (n) => ({
      number: n, frame: frame.value, x: coordX.value, y: coordY.value })) }, "Set measured position"),
    clearCoords));

frame.addEventListener("change", () => {
  const labels = plan()?.frames?.[frame.value] ?? ["east", "north"];
  xLabel.textContent = labels[0];
  yLabel.textContent = labels[1];
  const point = current();
  if (point?.observed) fillCoords(point);
});

async function positionAction(url, payload) {
  const number = currentNumber();
  if (number == null) return;
  const reply = await act(url, payload(number));
  if (reply?.result) result.textContent = reply.result;
}

const detail = h("div", { class: "group" },
  h("div", { class: "legend" }, "Position"), hint, tapeRow, offsetRow, rtkRow, result);

function currentNumber() {
  // With several rows selected there is no single point to edit, so the
  // editor goes quiet rather than applying a typed coordinate to whichever
  // row happened to be first.
  const numbers = selection();
  return numbers.length === 1 ? numbers[0] : null;
}

function current() {
  const number = currentNumber();
  return number == null ? null : plan()?.points.find((p) => p.number === number) ?? null;
}

function fillCoords(point) {
  const pair = point.observed?.[frame.value];
  coordX.value = pair ? pair[0] : "";
  coordY.value = pair ? pair[1] : "";
}

let detailFor = null;

function showDetail() {
  const numbers = selection();
  const point = current();
  deleteButton.textContent = numbers.length > 1 ? `Delete ${numbers.length}` : "Delete";
  deleteButton.disabled = numbers.length === 0;
  insertButton.disabled = point == null;
  if (!point) {
    hint.textContent = numbers.length > 1
      ? `${numbers.length} points selected. Delete removes them all; select a single row to edit its position.`
      : "Select a row.";
    tapeRow.hidden = offsetRow.hidden = rtkRow.hidden = true;
    result.textContent = "";
    detailFor = null;
    return;
  }
  if (detailFor !== point.number) result.textContent = "";
  detailFor = point.number;
  hint.textContent = point.detail;
  tapeRow.hidden = point.method !== "taped";
  offsetRow.hidden = point.method !== "station_offset";
  rtkRow.hidden = point.method !== "rtk";
  clearCoords.disabled = !point.observed;

  if (point.method === "rtk") fillCoords(point);
  if (point.method === "taped" && point.ties.length >= 2) {
    tieA.value = point.ties[0].ref; distA.value = point.ties[0].ft.toFixed(2);
    tieB.value = point.ties[1].ref; distB.value = point.ties[1].ft.toFixed(2);
  }
  if (point.method === "station_offset") {
    clear(refLine).append(...plan().line_ids.map((id) => h("option", { value: id }, id)));
    if (point.ref_line) refLine.value = point.ref_line;
    station.value = point.station_ft.toFixed(2);
    offset.value = point.offset_ft.toFixed(2);
  }
}

onSelection(() => showDetail());

// --- editing ------------------------------------------------------------------

async function deleteSelected() {
  const numbers = selection();
  if (!numbers.length) return;
  // The server confirms a bulk delete and not a single one.
  await act("/api/plan/delete", { numbers });
}

const coverage = h("div", { class: "coverage" });
const insertButton = h("button", {
  title: "Add a vertex after the selected one, halfway to the next",
  onclick: async () => {
    const number = currentNumber();
    if (number == null) return;
    const reply = await act("/api/plan/insert", { number });
    if (reply?.selected != null) requests.select(reply.selected);
  },
}, "Insert vertex");
const deleteButton = h("button", { onclick: deleteSelected, disabled: true }, "Delete");

clear(body).append(
  tools, tableEl, detail, coverage,
  h("div", { class: "row" }, insertButton, deleteButton,
    h("button", { onclick: writeFieldSheet }, "Field sheet…")));
showDetail();

subscribe((state, prev) => {
  if (changed(state, prev, "plan", "site")) load(state.plan);
  coverage.textContent = state.plan.coverage;
});
