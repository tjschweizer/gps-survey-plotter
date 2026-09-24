// Wiring: tabs, resizable docks, keyboard, and the first snapshot.

import { store, subscribe, changed, planMode } from "./store.js";
import { refresh } from "./api.js";
import * as map2d from "./map2d.js";
import * as view3d from "./view3d.js";
import { cancelLine } from "./planmap.js";
import "./panels/layers.js";
import "./panels/chain.js";
import "./panels/basemaps.js";
import "./panels/plan.js";
import "./panels/vertical.js";
import { buildMenus, SHORTCUTS } from "./menus.js";
import { setMode } from "./actions.js";
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
for (const [key, fallback] of [["left-w", 300], ["right-w", 390], ["qc-h", 128]]) {
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
let menusFor = null;

subscribe((state, prev) => {
  document.title = state.title;
  qc.textContent = state.qc;
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

// For tests and the console: the latest snapshot, the map, and a way to
// fetch the state again after something changed it from outside the page.
window.yardsurvey = { store, map: map2d.map, refresh };
