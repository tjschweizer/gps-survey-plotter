// Basemap panel.
//
// Every raster that has been fetched, with a checkbox and its own opacity.
// They stack bottom to top in the order listed, which puts photographs
// underneath LiDAR products - so a hillshade at 40% over an aerial photo is
// one click and a slider away, the most useful view for spotting break lines.
//
// The alignment group shifts every basemap together. County and state orthos
// are georeferenced to a foot or two, fine for finding the house and useless
// for clicking a driveway edge. The shift moves the PHOTO; the measurements
// never move.

import { h, clear } from "../dom.js";
import { subscribe, selection } from "../store.js";
import { act } from "../api.js";
import { previewBasemapOpacity } from "../map2d.js";
import { fetchAllImagery } from "../actions.js";

const body = document.querySelector("#panel-basemaps .body");
const list = h("div", { class: "basemap-list" });
const empty = h("div", { class: "empty" });
const rows = new Map();
let names = "";

const offE = h("input", { type: "number", step: "0.1", "aria-label": "shift east (ft)" });
const offN = h("input", { type: "number", step: "0.1", "aria-label": "shift north (ft)" });
const note = h("div", { class: "muted small" });
// Shifting imagery means nothing until there is imagery, so the group stays
// folded away until the first basemap arrives.
let alignment = null;
let hadBasemaps = null;

function setOffset() {
  const de = Number(offE.value), dn = Number(offN.value);
  if (!Number.isFinite(de) || !Number.isFinite(dn)) return;
  act("/api/imagery/offset", { de_ft: de, dn_ft: dn });
}
offE.addEventListener("change", setOffset);
offN.addEventListener("change", setOffset);

clear(body).append(
  list, empty,
  h("div", { class: "row" },
    h("button", { onclick: fetchAllImagery }, "Fetch all basemaps"),
    h("button", { onclick: () => act("/api/imagery/clear") }, "Clear")),
  alignment = h("details", { class: "group alignment" },
    h("summary", { class: "legend" }, "Alignment"),
    h("div", { class: "grid2" }, h("label", {}, "east (ft)"), offE, h("label", {}, "north (ft)"), offN),
    h("div", { class: "row" },
      h("button", {
        title: "Uses plan points whose mark was clicked on a feature visible in " +
               "the photo and which have since been measured. Select rows in the " +
               "shot plan to choose them yourself.",
        onclick: () => act("/api/imagery/offset/solve", { numbers: selection() }),
      }, "Solve from points"),
      h("button", { onclick: () => act("/api/imagery/offset/clear") }, "No shift")),
    note));

function makeRow(b) {
  const check = h("input", { type: "checkbox" });
  const slider = h("input", { type: "range", min: "0", max: "100", "aria-label": `${b.name} opacity` });
  const pct = h("span", { class: "pct num" });
  const sampled = h("div", { class: "muted small",
    title: "Pixel spacing of the fetched tile. The native resolution of the source is in its name." });
  check.addEventListener("change", () => act("/api/basemap", { name: b.name, visible: check.checked }));
  slider.addEventListener("input", () => {
    pct.textContent = `${slider.value}%`;
    previewBasemapOpacity(b.name, Number(slider.value) / 100);
  });
  slider.addEventListener("change", () =>
    act("/api/basemap", { name: b.name, opacity: Number(slider.value) / 100 }));
  const label = h("label", {
    class: b.terrain ? "terrain" : "",
    title: b.terrain ? "LiDAR-derived raster, not a photograph" : b.attribution,
  }, check, b.name);
  const el = h("div", { class: "basemap" }, label, sampled,
    h("div", { class: "opacity" }, h("span", { class: "muted" }, "opacity"), slider, pct));
  return { el, check, slider, pct, sampled };
}

subscribe((state) => {
  // Rows are rebuilt only when the set of basemaps changes; tearing them down
  // on every update would fight a slider mid-drag. They are always re-synced,
  // because visibility and opacity also change from outside this panel.
  const now = state.basemaps.map((b) => b.name).join("\n");
  if (now !== names) {
    names = now;
    rows.clear();
    clear(list);
    for (const b of state.basemaps) {
      const row = makeRow(b);
      rows.set(b.name, row);
      list.append(row.el);
    }
  }
  for (const b of state.basemaps) {
    const row = rows.get(b.name);
    row.check.checked = b.visible;
    if (document.activeElement !== row.slider) row.slider.value = String(Math.round(b.opacity * 100));
    row.pct.textContent = `${Math.round(b.opacity * 100)}%`;
    row.sampled.textContent = b.sampled;
  }
  empty.textContent = state.basemaps_empty;
  empty.hidden = state.basemaps.length > 0;
  list.hidden = state.basemaps.length === 0;

  const has = state.basemaps.length > 0;
  if (has !== hadBasemaps) {
    alignment.open = has;
    hadBasemaps = has;
  }

  const off = state.imagery_offset;
  if (document.activeElement !== offE) offE.value = off.de_ft.toFixed(2);
  if (document.activeElement !== offN) offN.value = off.dn_ft.toFixed(2);
  note.textContent = off.describe;
});
