// Sessions panel: every outing in the active layer, compared rather than pooled.
//
// Each session gets a checkbox that takes its points off the map, an "only"
// button that shows it alone, and the numbers that say how good it is:
// how much of it was RTK fixed, how much the filter stack kept, and its
// internal repeatability - crossovers between its own passes, the figure to
// compare outings by. Underneath, each pair of overlapping sessions shows how
// far apart they sit, which is the step the vertical model has to remove.
//
// By default hiding a session only changes what is drawn. With "surface and
// QC from shown sessions only" ticked it also changes what is surfaced and
// measured, so one outing's surface and QC can be looked at on their own.
// The datum is solved from every session either way.

import { h, clear } from "../dom.js";
import { subscribe } from "../store.js";
import { act } from "../api.js";
import { colourBySession } from "../map2d.js";

const body = document.querySelector("#panel-sessions .body");
let shown = null;

const fmt = (n, digits = 2) => (n == null ? "–" : n.toFixed(digits));
const signed = (n) => (n == null ? "–" : `${n >= 0 ? "+" : "−"}${Math.abs(n).toFixed(2)}`);

function sessionRow(s, count) {
  const minutes = s.minutes == null ? "" : ` · ${Math.round(s.minutes)} min`;
  const repeat = s.pairs
    ? [h("strong", {}, `${fmt(s.rms_cm)} cm`), ` RMS, median ${fmt(s.median_cm)} cm (${s.pairs.toLocaleString()} pairs)`]
    : ["no crossovers within the session"];
  return h("div", { class: "session" + (s.shown ? "" : " hidden-session") },
    h("div", { class: "session-head" },
      h("label", { title: s.shown ? "Hide this session's points" : "Show this session's points" },
        h("input", {
          type: "checkbox", checked: s.shown,
          onchange: (e) => act("/api/sessions/visible", { name: s.name, visible: e.target.checked },
                               { busy: "Updating sessions…" }),
        }),
        h("span", { class: "swatch", style: { background: s.colour } }),
        h("span", {}, s.name)),
      count > 1 ? h("button", {
        class: "icon", title: "Show only this session",
        onclick: () => act("/api/sessions/shown", { names: [s.name] }, { busy: "Updating sessions…" }),
      }, "only") : null),
    h("div", { class: "metrics" },
      `${s.points.toLocaleString()} pts · ${fmt(s.fixed_pct, 0)}% fixed · ${s.kept.toLocaleString()} kept${minutes}`),
    h("div", { class: "metrics", title: "Crossovers between this session's own passes: within 30 cm, over 60 s apart" },
      "repeatability ", ...repeat),
    s.offset_cm == null ? null
      : h("div", { class: "metrics", title: "The constant the vertical model removes from this session" },
          `solved offset ${signed(s.offset_cm)} cm`),
    s.checks == null ? null
      : h("div", { class: "metrics checks" + (s.checks_flagged ? " flagged" : ""),
                   title: "Check shots on control marks at the start and end of the outing, after its offset" },
          s.checks));
}

function pairRow(p) {
  const after = p.after_cm == null ? ""
    : `; after the solved offsets ${signed(p.after_cm)} cm`;
  return h("div", {},
    h("div", { class: "pair-names" }, `${p.later} − ${p.earlier}`),
    h("div", {}, h("strong", {}, `${signed(p.diff_cm)} cm`),
      ` ±${fmt(p.scatter_cm)} (${p.n.toLocaleString()} pairs)${after}`));
}

function render(sessions) {
  clear(body);
  if (!sessions.list.length) {
    body.append(h("div", { class: "empty" }, "No sessions: load an export first."));
    return;
  }
  const count = sessions.list.length;
  for (const s of sessions.list) body.append(sessionRow(s, count));

  const anyHidden = sessions.list.some((s) => !s.shown);
  body.append(
    h("div", { class: "row" },
      anyHidden ? h("button", { onclick: () => act("/api/sessions/shown", { names: null },
                                                   { busy: "Updating sessions…" }) }, "Show all") : null,
      count > 1 ? h("button", { title: "Colour the map's points to match the swatches",
                                onclick: colourBySession }, "Colour by session") : null),
    h("label", { title: "Hidden sessions also leave the surface, the 3D view, the QC readout and the exports. The datum is still solved from every session." },
      h("input", {
        type: "checkbox", checked: sessions.surface_from_shown,
        onchange: (e) => act("/api/sessions/surface", { on: e.target.checked },
                             { busy: "Rebuilding the surface…" }),
      }),
      "Surface and QC from shown sessions only"));

  if (sessions.pairs.length) {
    body.append(h("div", { class: "pairs-title" }, "Between sessions (later − earlier, as logged)"),
      h("div", { class: "pairs" }, sessions.pairs.map(pairRow)));
  } else if (count > 1) {
    body.append(h("div", { class: "muted small" },
      "These sessions share no ground, so how far apart they sit is not measured."));
  }
}

subscribe((state) => {
  const key = JSON.stringify(state.sessions);
  if (key === shown) return;
  shown = key;
  render(state.sessions);
});
