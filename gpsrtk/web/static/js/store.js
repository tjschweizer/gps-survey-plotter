// The latest snapshot from the server, plus the few things that exist only in
// the browser: the plan drawing tool, which plan rows are selected, which
// outline is, and what kind of outline the next one drawn will be.
//
// Every panel subscribes and redraws itself from the snapshot. Heavy views
// compare revision counters (`changed`) to decide whether their data moved.

export const store = { state: null };

const listeners = [];

export function subscribe(fn) {
  listeners.push(fn);
}

export function applySnapshot(snapshot) {
  const prev = store.state;
  store.state = snapshot;
  for (const fn of listeners) {
    try { fn(snapshot, prev); } catch (err) { console.error(err); }
  }
}

export function changed(state, prev, ...topics) {
  if (!prev) return true;
  return topics.some((t) => state.rev[t] !== prev.rev[t]);
}

// --- browser-only state -----------------------------------------------------

const ui = { mode: "navigate", selection: [], outline: null, outlineKind: "building" };
const modeListeners = [];
const selectionListeners = [];
const outlineListeners = [];

export function planMode() { return ui.mode; }

export function setPlanMode(mode) {
  if (mode === ui.mode) return;
  const previous = ui.mode;
  ui.mode = mode;
  for (const fn of modeListeners) fn(mode, previous);
}

export function onPlanMode(fn) { modeListeners.push(fn); }

// Selection flows one way: the table decides, the map reflects. A click on a
// marker asks the table to select its row; it never sets this directly.
export function selection() { return ui.selection; }

export function setSelection(numbers) {
  const next = [...numbers];
  if (next.length === ui.selection.length && next.every((n, i) => n === ui.selection[i])) return;
  ui.selection = next;
  for (const fn of selectionListeners) fn(next);
}

export function onSelection(fn) { selectionListeners.push(fn); }

// One outline at a time: picked in the list or clicked on the map, drawn
// highlighted on the map.
export function selectedOutline() { return ui.outline; }

export function setSelectedOutline(id) {
  const next = id ?? null;
  if (next === ui.outline) return;
  ui.outline = next;
  for (const fn of outlineListeners) fn(next);
}

export function onOutlineSelection(fn) { outlineListeners.push(fn); }

export function outlineKind() { return ui.outlineKind; }
export function setOutlineKind(kind) { ui.outlineKind = kind; }

// Hooks one module offers another without importing it. The plan table sets
// `select`; the map calls it when a marker is clicked.
export const requests = {
  select: (number) => {},
};
