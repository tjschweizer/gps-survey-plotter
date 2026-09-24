// Filter stack panel.
//
// The chain as an ordered list of stages, each with its parameters and its
// points in and out. The counts are the point of this panel as much as the
// controls are: a chain that quietly drops most of the data is the easiest
// way to produce a confident, wrong surface.
//
// Parameter editors are generated from what the server says each stage takes,
// so a newly registered stage appears here with working controls and no UI
// code.

import { h, clear } from "../dom.js";
import { subscribe } from "../store.js";
import { act } from "../api.js";

const body = document.querySelector("#panel-chain .body");
let shown = null;
let chosenKind = null;

const edit = (payload) => act("/api/chain/edit", payload, { busy: "Filtering…" });

function stageCard(stage, index, count) {
  const params = h("div", { class: "params grid2" });
  for (const p of stage.params) {
    params.append(h("label", { class: "param-label", for: `p-${index}-${p.key}` }, p.key));
    if (p.bool) {
      params.append(h("input", {
        id: `p-${index}-${p.key}`, type: "checkbox", checked: p.value,
        onchange: (e) => edit({ index, params: { [p.key]: e.target.checked } }),
      }));
    } else {
      params.append(h("input", {
        id: `p-${index}-${p.key}`, type: "text", value: p.text, placeholder: "none",
        spellcheck: false,
        onchange: (e) => edit({ index, params: { [p.key]: e.target.value } }),
        onkeydown: (e) => { if (e.key === "Enter") e.target.blur(); },
      }));
    }
  }
  return h("div", { class: "stage" + (stage.enabled ? "" : " off") },
    h("div", { class: "stage-head" },
      h("label", { class: "stage-name" },
        h("input", {
          type: "checkbox", checked: stage.enabled,
          onchange: (e) => edit({ index, enabled: e.target.checked }),
        }), stage.label),
      h("button", { class: "icon", title: "move up", disabled: index === 0,
        onclick: () => act("/api/chain/move", { index, delta: -1 }, { busy: "Filtering…" }) }, "▲"),
      h("button", { class: "icon", title: "move down", disabled: index === count - 1,
        onclick: () => act("/api/chain/move", { index, delta: 1 }, { busy: "Filtering…" }) }, "▼"),
      h("button", { class: "icon", title: "remove stage",
        onclick: () => act("/api/chain/remove", { index }, { busy: "Filtering…" }) }, "✕")),
    stage.params.length ? params : null,
    h("div", { class: "counts" }, stage.counts ?? ""));
}

function render(chain) {
  clear(body);
  chain.stages.forEach((stage, i) => body.append(stageCard(stage, i, chain.stages.length)));
  const picker = h("select", { class: "grow", "aria-label": "stage to add",
    onchange: (e) => { chosenKind = e.target.value; } },
    chain.kinds.map((k) => h("option", { value: k, selected: k === chosenKind }, k)));
  chosenKind = picker.value;
  body.append(
    h("div", { class: "row" }, picker,
      h("button", { onclick: () => act("/api/chain/add", { kind: picker.value }, { busy: "Filtering…" }) }, "Add")),
    h("div", { class: "total" }, chain.total));
}

subscribe((state) => {
  // Rebuilt only when something about the chain changed, so a panel being
  // redrawn for an unrelated reason never steals focus from a field.
  const key = JSON.stringify(state.chain);
  if (key === shown) return;
  shown = key;
  render(state.chain);
});
