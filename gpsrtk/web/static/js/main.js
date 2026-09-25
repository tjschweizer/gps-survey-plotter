// Wiring: tabs, resizable docks, keyboard, and the first snapshot.

import { store, subscribe, changed, planMode } from "./store.js";
import { refresh } from "./api.js";
import * as map2d from "./map2d.js";
import * as view3d from "./view3d.js";
import { cancelLine } from "./planmap.js";
import "./panels/layers.js";
import "./panels/sessions.js";
import "./panels/chain.js";
import "./panels/basemaps.js";
import "./panels/plan.js";
import "./panels/vertical.js";
import { buildMenus, SHORTCUTS } from "./menus.js";
import { setMode, openExport, openProject } from "./actions.js";
import { act } from "./api.js";
import { h } from "./dom.js";
import { isTyping, remember, recall } from "./dom.js";
import { showNotice } from "./dialogs.js";

// --- tabs ------------------------------------------------------------------

export function selectTab(name) {
  for (const b of document.querySelectorAll("#tabs button")) {
    b.setAttribute("aria-selected", String(b.dataset.tab === name));
  }
  document.getElementById("view-plan").hidden = name !== "plan";
  document.getElementById("view-3d").hidden = name !== "3d";
  view3d.setVisible(name === "3d");
  if (name === "plan") map2d.map.updateSize();
}
for (const b of document.querySelectorAll("#tabs button")) {
  b.addEventListener("click", () => selectTab(b.dataset.tab));
}

// --- resizable docks ------------------------------------------------------------

// Narrow enough that both docks and the map fit on a laptop screen, wide
// enough that the panels are still usable.
const DOCK_MIN = 210;
const root = document.documentElement.style;
for (const [key, fallback] of [["left-w", 300], ["right-w", 390], ["qc-h", 150]]) {
  root.setProperty(`--${key}`, `${recall(key, fallback)}px`);
}

function drag(handle, onMove, key) {
  handle.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    handle.classList.add("dragging");
    const move = (ev) => {
      const value = Math.round(onMove(ev));
      root.setProperty(`--${key}`, `${value}px`);
      remember(key, value);
    };
    const up = () => {
      handle.classList.remove("dragging");
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
  });
}
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
drag(document.querySelector('.splitter[data-side="left"]'),
     (e) => clamp(e.clientX - 3, DOCK_MIN, window.innerWidth * 0.45), "left-w");
drag(document.querySelector('.splitter[data-side="right"]'),
     (e) => clamp(window.innerWidth - e.clientX - 3, DOCK_MIN, window.innerWidth * 0.55), "right-w");
drag(document.querySelector(".hsplitter"), (e) => {
  const bottom = document.getElementById("centre").getBoundingClientRect().bottom;
  return clamp(bottom - e.clientY - 3, 36, 480);
}, "qc-h");

// --- keyboard ------------------------------------------------------------------

document.addEventListener("keydown", (e) => {
  if (document.querySelector("dialog[open]")) return;
  const combo = [(e.ctrlKey || e.metaKey) && "ctrl", e.shiftKey && "shift", e.key.toLowerCase()]
    .filter(Boolean).join("+");
  if (SHORTCUTS[combo]) {
    e.preventDefault();
    SHORTCUTS[combo]();
    return;
  }
  // Escape puts the current drawing tool down. Without it the only way out
  // of a placing mode is to find the Navigate button, which is the wrong
  // reflex when you have just misclicked.
  if (e.key === "Escape" && !isTyping(e.target) && planMode() !== "navigate") {
    e.preventDefault();
    cancelLine();
    setMode("navigate");
  }
});

// --- the page's own readouts ----------------------------------------------------------

const qc = document.getElementById("qc");
const status = document.getElementById("status");
const strip = document.getElementById("status-strip");
let menusFor = null;

subscribe((state, prev) => {
  document.title = state.title;
  qc.textContent = state.qc;
  // What the heights mean, and how the data stands, above the map where it
  // is seen rather than at the bottom of the last panel.
  strip.replaceChildren(...(state.strip ?? []).map((s) =>
    Object.assign(document.createElement("span"), { className: `seg ${s.level}`, textContent: s.text })));
  if (changed(state, prev, "status")) {
    status.textContent = state.status || "Ready. File ▸ Open export… to begin.";
  }
  const providers = JSON.stringify(state.providers);
  if (providers !== menusFor) {
    menusFor = providers;
    buildMenus(document.getElementById("menubar"), state.providers);
  }
  // A project reopens looking the way it was saved.
  if (prev && changed(state, prev, "view")) {
    const v = state.view ?? {};
    map2d.applyView(v);
    if ("vertical_exaggeration" in v) view3d.applyExaggeration(v.vertical_exaggeration);
    if ("tab" in v) selectTab(Number(v.tab) === 1 ? "3d" : "plan");
  }
});

refresh().catch((err) => {
  status.textContent = "Could not reach the server.";
  showNotice({ title: "No connection", level: "error",
               text: "The Yard Survey server is not answering.\n\n" + err });
});

// --- the empty page ---------------------------------------------------------------
//
// With nothing loaded the map is a blank canvas with no obvious first move.
// It offers the two ways in, and the last few files opened.

const start = document.getElementById("start");
let startFor = null;

function renderStart(state) {
  start.hidden = state.has_data;
  if (state.has_data) return;
  const key = JSON.stringify(state.recent ?? []);
  if (key === startFor) return;
  startFor = key;
  const open = (r) => act(r.kind === "project" ? "/api/project/open" : "/api/export/open",
                          { path: r.path }, { busy: `Opening ${r.name}…` });
  start.replaceChildren(h("div", { class: "start-box" },
    h("h2", {}, "Yard Survey"),
    h("div", { class: "row" },
      h("button", { class: "primary", onclick: openExport }, "Open export…"),
      h("button", { onclick: openProject }, "Open project…")),
    (state.recent ?? []).length
      ? h("div", { class: "recent" },
          h("div", { class: "recent-title" }, "Recent"),
          state.recent.map((r) => h("button", { class: "recent-item", title: r.path, onclick: () => open(r) },
            h("span", { class: "kind" }, r.kind === "project" ? "project" : "export"),
            h("span", { class: "name" }, r.name),
            h("span", { class: "folder" }, r.folder))))
      : h("div", { class: "muted" }, "Recently opened exports and projects will be listed here.")));
}
subscribe(renderStart);

// For tests and the console: the latest snapshot, the map, and a way to
// fetch the state again after something changed it from outside the page.
window.yardsurvey = { store, map: map2d.map, refresh };
