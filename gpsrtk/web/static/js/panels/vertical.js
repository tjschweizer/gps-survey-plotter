// Vertical datum panel.
//
// Shows what the elevations currently mean, because "97.9 ft" and "860.4 ft"
// look equally plausible on screen and only one of them is on the datum you
// think you are working in. When no model has been solved, it says so in as
// many words rather than leaving the reader to infer it.

import { h, clear } from "../dom.js";
import { subscribe } from "../store.js";
import { act } from "../api.js";
import { solveVertical } from "../actions.js";

const body = document.querySelector("#panel-vertical .body");
const readout = h("pre", { class: "readout" });
const clearButton = h("button", {
  onclick: () => act("/api/vertical/clear"),
}, "Clear");
clear(body).append(
  readout,
  h("div", { class: "row" },
    h("button", {
      title: "Level the laser network, solve per-session offsets, then tie the " +
             "walked data to the laser ground surface",
      onclick: () => solveVertical("local"),
    }, "Solve local datum"),
    clearButton));

subscribe((state) => {
  readout.textContent = state.vertical.text;
  clearButton.disabled = !state.vertical.solved;
});
