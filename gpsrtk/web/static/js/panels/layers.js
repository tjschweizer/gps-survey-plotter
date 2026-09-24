// Layer panel.
//
// Two independent things per layer, which are easy to conflate:
//
//   visible  whether it is drawn
//   active   whether it is the layer the filter chain and surface are built from
//
// Only one layer can be active. Spot layers stay visible as reference while
// the surface is built from continuous logging - as an independent check, not
// as surface input. Mixing them injects the antenna-height offset as spikes.

import { h, clear } from "../dom.js";
import { subscribe } from "../store.js";
import { act } from "../api.js";

const body = document.querySelector("#panel-layers .body");

function render(state) {
  clear(body);
  if (!state.layers.length) {
    body.append(h("div", { class: "empty" }, "No data loaded.\nFile ▸ Open export…"));
    return;
  }
  const grid = h("div", { class: "layers" },
    h("span", { class: "head" }, "show"),
    h("span", { class: "head" }, "source"),
    h("span", { class: "head" }, "layer"));
  for (const layer of state.layers) {
    grid.append(
      h("input", {
        type: "checkbox", checked: layer.visible, title: "draw this layer",
        "aria-label": `show ${layer.name}`,
        onchange: (e) => act("/api/layers/visible", { name: layer.name, visible: e.target.checked }),
      }),
      h("input", {
        type: "radio", name: "active-layer", checked: layer.active,
        title: "build the surface from this layer", "aria-label": `build from ${layer.name}`,
        onchange: (e) => e.target.checked &&
          act("/api/layers/active", { name: layer.name }, { busy: "Rebuilding the surface…" }),
      }),
      h("div", {}, h("div", {}, layer.name), h("div", { class: "desc" }, layer.describe)));
  }
  body.append(grid);
}

subscribe((state) => render(state));
