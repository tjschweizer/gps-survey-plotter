// The menu bar. Same menus, same items and the same shortcuts as the desktop
// app, so the README's "Data ▸ Fetch all basemaps" still says where to click.

import { h, clear } from "./dom.js";
import * as A from "./actions.js";

export const SHORTCUTS = {
  "ctrl+o": A.openExport,
  "ctrl+shift+a": A.addExport,
  "ctrl+shift+o": A.openProject,
  "ctrl+s": () => A.saveProject(false),
  "ctrl+shift+s": () => A.saveProject(true),
};

function spec(providers) {
  const list = (items, run) => items.map((p) => ({ label: p.name, tip: p.description, run: () => run(p.name) }));
  return [
    { label: "File", items: [
      { label: "Open export…", key: "Ctrl+O", run: A.openExport,
        tip: "Replace everything loaded with this export" },
      { label: "Add export…", key: "Ctrl+Shift+A", run: A.addExport,
        tip: "Merge another outing into what is already loaded" },
      "-",
      { label: "Open project…", key: "Ctrl+Shift+O", run: A.openProject },
      { label: "Save project", key: "Ctrl+S", run: () => A.saveProject(false) },
      { label: "Save project as…", key: "Ctrl+Shift+S", run: () => A.saveProject(true) },
      "-",
      { label: "Quit", run: A.quit, tip: "Stop the Yard Survey server" },
    ] },
    { label: "Data", items: [
      { label: "Fetch all basemaps", run: A.fetchAllImagery,
        tip: "Try every imagery and LiDAR source once, then toggle them in the Basemaps panel" },
      "-",
      { label: "Fetch one imagery source", items: list(providers.photo, A.fetchImagery) },
      { label: "Fetch LiDAR raster", items: list(providers.terrain, A.fetchImagery) },
      { label: "Load imagery file…", run: A.loadImageryFile },
      { label: "Clear imagery", run: A.clearImagery },
      "-",
      { label: "Fetch reference linework", items: list(providers.vector, A.fetchVectors) },
      { label: "Clear reference linework", run: A.clearVectors },
      "-",
      { label: "Check service availability…", run: A.checkServices },
      "-",
      { label: "Sessions and overlap…", run: A.showSessions,
        tip: "Which outings are loaded, and whether their vertical offsets can be solved" },
    ] },
    { label: "Datum", items: [
      { label: "Datum tie…", run: A.datumTie,
        tip: "Which shot the survey hangs from, and at what elevation" },
      { label: "Control marks…", run: A.controlMarks,
        tip: "Permanent marks shot at the start and end of every outing" },
      "-",
      { label: "Solve local datum", run: () => A.solveVertical("local"),
        tip: "Tie the walked data to the laser spots" },
      { label: "Solve NAVD88 (orthometric)", run: () => A.solveVertical("navd88") },
      { label: "Solve session offsets only", run: () => A.solveVertical("ellipsoidal") },
      "-",
      { label: "Clear vertical model", run: A.clearVertical },
    ] },
    { label: "Plan", items: [
      { label: "Add shot", run: () => A.setMode("add_point") },
      { label: "Add line", run: () => A.setMode("add_line") },
      { label: "Add outline", run: () => A.setMode("add_outline"),
        tip: "A building, bed, fence, street or the lot; its corners become shots, and a keep-out outline stays out of the surface" },
      { label: "Add laser setup", run: () => A.setMode("add_setup") },
      { label: "Add tie transects", run: A.addTieTransects,
        tip: "Lines through the best-covered ground, to walk or mow first so the next outing overlaps this one" },
      { label: "Navigate", key: "Esc", run: () => A.setMode("navigate") },
      "-",
      { label: "Print field sheet…", run: A.writeFieldSheet },
      "-",
      { label: "Open plan…", run: A.openPlan },
      { label: "Save plan…", run: A.savePlan },
    ] },
    { label: "Export", items: [
      { label: "Heightmap raster…", run: A.exportHeightmap },
      { label: "Revit points file…", run: A.exportRevit },
      "-",
      { label: "Slope map (PNG)…", run: A.exportSlopeMap,
        tip: "Slope with arrows pointing downhill, at the plan view's slope scale" },
      { label: "Contour map (PNG)…", run: A.exportContourMap,
        tip: "Shaded relief with contours at the plan view's interval" },
    ] },
    { label: "View", items: [
      { label: "Reset 2D view", run: A.resetView2D },
      { label: "Reset 3D view", run: A.resetView3D },
    ] },
  ];
}

let openRoot = null;

function closeMenus() {
  if (!openRoot) return;
  openRoot.button.setAttribute("aria-expanded", "false");
  openRoot.menu.hidden = true;
  openRoot = null;
}

function openMenu(root) {
  if (openRoot === root) return;
  closeMenus();
  openRoot = root;
  root.button.setAttribute("aria-expanded", "true");
  root.menu.hidden = false;
}

function buildItems(items) {
  const menu = h("div", { class: "menu", role: "menu", hidden: true });
  for (const item of items) {
    if (item === "-") { menu.append(h("div", { class: "sep", role: "separator" })); continue; }
    if (item.items) {
      const sub = buildItems(item.items.length ? item.items
        : [{ label: "(none available)", run: null }]);
      const entry = h("div", { class: "item has-sub", role: "menuitem", tabindex: "-1",
                               "aria-haspopup": "true" }, h("span", {}, item.label), sub);
      entry.addEventListener("mouseenter", () => { sub.hidden = false; entry.classList.add("open"); });
      entry.addEventListener("mouseleave", () => { sub.hidden = true; entry.classList.remove("open"); });
      menu.append(entry);
      continue;
    }
    menu.append(h("button", {
      class: "item", role: "menuitem", title: item.tip ?? "", disabled: !item.run,
      onclick: (e) => { e.stopPropagation(); closeMenus(); item.run?.(); },
    }, h("span", {}, item.label), item.key ? h("span", { class: "key" }, item.key) : null));
  }
  return menu;
}

// --- the keyboard --------------------------------------------------------------
//
// Menus used to open on hover only. Now, as in any desktop menu bar:
// Down/Enter/Space on a menu opens it; Up/Down/Home/End move through its
// items; Right opens a submenu, or moves to the next menu; Left closes a
// submenu, or moves to the previous menu; Escape backs out one level.

let roots = [];

const itemsOf = (menu) => [...menu.children].filter(
  (el) => el.classList.contains("item") && !el.disabled);

function openSub(entry) {
  const sub = entry.querySelector(":scope > .menu");
  sub.hidden = false;
  entry.classList.add("open");
  itemsOf(sub)[0]?.focus();
}

function closeSub(entry) {
  entry.querySelector(":scope > .menu").hidden = true;
  entry.classList.remove("open");
  entry.focus();
}

function openAndFocus(root) {
  openMenu(root);
  itemsOf(root.menu)[0]?.focus();
}

function onMenuKey(e) {
  const at = roots.findIndex((r) => r.button === e.target || r.menu.contains(e.target));
  if (at < 0) return;
  const root = roots[at];
  const neighbour = (step) => roots[(at + step + roots.length) % roots.length];
  let handled = true;

  if (e.target === root.button) {
    if (["ArrowDown", "Enter", " "].includes(e.key)) openAndFocus(root);
    else if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      const next = neighbour(e.key === "ArrowRight" ? 1 : -1);
      if (openRoot) openAndFocus(next); else next.button.focus();
    } else handled = false;
  } else {
    const menu = e.target.closest(".menu");
    const list = itemsOf(menu);
    const i = list.indexOf(e.target);
    const inSub = menu !== root.menu;
    const hasSub = e.target.classList.contains("has-sub");
    switch (e.key) {
      case "ArrowDown": list[(i + 1) % list.length]?.focus(); break;
      case "ArrowUp": list[(i - 1 + list.length) % list.length]?.focus(); break;
      case "Home": list[0]?.focus(); break;
      case "End": list[list.length - 1]?.focus(); break;
      case "ArrowRight":
        if (hasSub) openSub(e.target); else openAndFocus(neighbour(1));
        break;
      case "ArrowLeft":
        if (inSub) closeSub(menu.parentElement); else openAndFocus(neighbour(-1));
        break;
      case "Enter": case " ":
        if (hasSub) openSub(e.target); else handled = false;   // a button clicks itself
        break;
      case "Escape":
        if (inSub) closeSub(menu.parentElement);
        else { closeMenus(); root.button.focus(); }
        break;
      default: handled = false;
    }
  }
  if (handled) {
    e.preventDefault();
    e.stopPropagation();
  }
}

export function buildMenus(bar, providers) {
  closeMenus();
  clear(bar);
  roots = [];
  for (const top of spec(providers)) {
    const button = h("button", { "aria-haspopup": "true", "aria-expanded": "false" }, top.label);
    const menu = buildItems(top.items);
    const root = { button, menu };
    button.addEventListener("click", (e) => {
      e.stopPropagation();
      if (openRoot === root) closeMenus(); else openMenu(root);
    });
    button.addEventListener("mouseenter", () => { if (openRoot) openMenu(root); });
    bar.append(h("div", { class: "menu-root" }, button, menu));
    roots.push(root);
  }
  if (!bar.dataset.keys) {
    bar.addEventListener("keydown", onMenuKey);
    bar.dataset.keys = "on";
  }
}

document.addEventListener("click", closeMenus);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenus(); });
