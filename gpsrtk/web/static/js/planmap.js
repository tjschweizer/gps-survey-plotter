// Drawing and editing a shot plan on the plan view.
//
// Division of labour, chosen deliberately: the MAP edits position, the TABLE
// edits everything else. Clicking a marker selects its row, because finding
// row 47 of a table by eye when you can see the shot on the aerial is silly.
//
// A marker is drawn at the point's BEST KNOWN position. Once a shot has been
// measured it becomes a square and stops being draggable - dragging it would
// edit the planned position, which is no longer what you are looking at. A
// faint leader runs back to the original click: that gap is the error in the
// plan, and where the plan was clicked off an aerial it is also a direct
// reading of how far that aerial is out.
//
// An outline - a building, a bed, a fence, the lot - is drawn from the same
// markers: its corners are shots, and dragging or measuring one moves the
// outline. A keep-out outline is hatched, because its inside is ground the
// surface leaves out.

import { map } from "./map2d.js";
import { store, subscribe, changed, planMode, setPlanMode, onPlanMode,
         onSelection, requests, selectedOutline, setSelectedOutline,
         onOutlineSelection, outlineKind } from "./store.js";
import { act } from "./api.js";

// Coloured by purpose GROUP: terrain, built feature, or control. Individual
// purposes are far too numerous to distinguish by colour legibly.
const GROUP_PEN = { terrain: [192, 57, 43], feature: [31, 95, 168], control: [43, 122, 61],
                    guide: [120, 60, 160] };
const SELECTED = [255, 190, 0];
const SETUP = [230, 126, 34];
const LEADER = [110, 110, 110];
const rgba = (c, a = 1) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;

let highlighted = null;

// --- layers -------------------------------------------------------------------

const leaderSource = new ol.source.Vector();
map.addLayer(new ol.layer.Vector({
  source: leaderSource, zIndex: 140,
  style: new ol.style.Style({
    stroke: new ol.style.Stroke({ color: rgba(LEADER), width: 1, lineDash: [2, 3] }),
  }),
}));

const lineSource = new ol.source.Vector();
map.addLayer(new ol.layer.Vector({
  source: lineSource, zIndex: 150, updateWhileInteracting: true,
  // A tie transect is a path to walk or mow first, not a break line to shoot.
  style: (f) => (f.get("kind") === "transect" ? transectStyle(f) : breaklineStyle),
}));
const breaklineStyle = new ol.style.Style({
  stroke: new ol.style.Stroke({ color: rgba(GROUP_PEN.terrain), width: 2.2, lineDash: [8, 5] }),
});
function transectStyle(f) {
  return new ol.style.Style({
    stroke: new ol.style.Stroke({ color: rgba(GROUP_PEN.guide, 0.85), width: 3, lineDash: [2, 6] }),
    text: new ol.style.Text({
      text: `${f.get("line_id")}: walk first`, placement: "line", font: "600 12px system-ui, sans-serif",
      fill: new ol.style.Fill({ color: rgba(GROUP_PEN.guide) }),
      stroke: new ol.style.Stroke({ color: "rgba(255,255,255,0.9)", width: 3 }),
    }),
  });
}

// Diagonal hatching over a light grey tint: "not ground we surface", legible
// over both an aerial and the shaded surface, and unlike any data colour.
const HATCH = (() => {
  const c = document.createElement("canvas");
  c.width = c.height = 8;
  const g = c.getContext("2d");
  g.fillStyle = "rgba(60,60,60,0.12)";
  g.fillRect(0, 0, 8, 8);
  g.strokeStyle = "rgba(40,40,40,0.55)";
  g.lineWidth = 1.2;
  g.beginPath();
  g.moveTo(0, 8); g.lineTo(8, 0);
  g.moveTo(-2, 2); g.lineTo(2, -2);
  g.moveTo(6, 10); g.lineTo(10, 6);
  g.stroke();
  return g.createPattern(c, "repeat");
})();
// A property line is control and dashed like a surveyor's boundary; a fence
// is dotted; everything else is a built feature's solid outline.
const OUTLINE_DASH = { "property line": [12, 4, 2, 4], fence: [2, 4] };

const outlineSource = new ol.source.Vector();
const outlineStyles = new Map();
function outlineStyle(f) {
  const kind = f.get("kind");
  const chosen = f.get("line_id") === selectedOutline();
  const keepOut = f.get("keep_out");
  const key = `${kind}|${keepOut}|${chosen}|${f.get("line_id")}`;
  let style = outlineStyles.get(key);
  if (style) return style;
  const pen = chosen ? SELECTED : kind === "property line" ? GROUP_PEN.control : GROUP_PEN.feature;
  style = new ol.style.Style({
    stroke: new ol.style.Stroke({ color: rgba(pen), width: chosen ? 3.5 : 2.2,
                                  lineDash: OUTLINE_DASH[kind] }),
    fill: keepOut ? new ol.style.Fill({ color: HATCH }) : undefined,
    // A hatched area is named in its middle; anything else along its edge,
    // since the middle of the lot's outline is where everything else is.
    text: new ol.style.Text({
      text: f.get("line_id") + (keepOut ? " (keep-out)" : ""),
      placement: keepOut ? "point" : "line", textBaseline: keepOut ? "middle" : "bottom",
      font: "600 12px system-ui, sans-serif", overflow: true,
      fill: new ol.style.Fill({ color: "#111" }),
      stroke: new ol.style.Stroke({ color: "rgba(255,255,255,0.9)", width: 3 }),
    }),
  });
  outlineStyles.set(key, style);
  return style;
}
const outlineLayer = new ol.layer.Vector({
  source: outlineSource, zIndex: 135, style: outlineStyle, updateWhileInteracting: true,
});
map.addLayer(outlineLayer);

const setupSource = new ol.source.Vector();
map.addLayer(new ol.layer.Vector({
  source: setupSource, zIndex: 155,
  style: (f) => new ol.style.Style({
    image: new ol.style.RegularShape({
      points: 3, radius: 9.5,
      fill: new ol.style.Fill({ color: rgba(SETUP, 220 / 255) }),
      stroke: new ol.style.Stroke({ color: "#000", width: 1.2 }),
    }),
    text: new ol.style.Text({
      text: f.get("name"), offsetY: 16, font: "600 12px system-ui, sans-serif",
      fill: new ol.style.Fill({ color: "#5a3000" }),
      stroke: new ol.style.Stroke({ color: "rgba(255,255,255,0.9)", width: 3 }),
    }),
  }),
}));

const markerStyles = new Map();
function markerStyle(feature) {
  const number = feature.get("number");
  const locked = feature.get("locked");
  const chosen = number === highlighted;
  const key = `${number}|${locked}|${feature.get("group")}|${chosen}`;
  let style = markerStyles.get(key);
  if (style) return style;

  const colour = chosen ? SELECTED : (GROUP_PEN[feature.get("group")] ?? GROUP_PEN.terrain);
  const stroke = new ol.style.Stroke({ color: rgba(colour), width: chosen ? 3 : 2 });
  // A measured marker is filled solidly: it is a fact, not a proposal.
  const alpha = locked ? (chosen ? 150 : 110) : (chosen ? 90 : 40);
  const fill = new ol.style.Fill({ color: rgba(colour, alpha / 255) });
  // Square for measured, circle for planned. Colour is already carrying the
  // purpose group, so shape is what is left to say surveyed or guessed.
  const image = locked
    ? new ol.style.RegularShape({ points: 4, radius: 7.5, angle: Math.PI / 4, stroke, fill })
    : new ol.style.Circle({ radius: 5.5, stroke, fill });
  style = new ol.style.Style({
    image,
    zIndex: chosen ? 2 : 1,
    text: new ol.style.Text({
      text: String(number), offsetX: 11, offsetY: -11,
      font: "600 12px system-ui, sans-serif",
      fill: new ol.style.Fill({ color: "#111" }),
      stroke: new ol.style.Stroke({ color: "rgba(255,255,255,0.9)", width: 3 }),
    }),
  });
  markerStyles.set(key, style);
  return style;
}

export const markerSource = new ol.source.Vector();
const markerLayer = new ol.layer.Vector({
  source: markerSource, zIndex: 160, style: markerStyle, updateWhileInteracting: true,
});
map.addLayer(markerLayer);

// --- drawing from the snapshot -------------------------------------------------------

function rebuild(plan) {
  markerSource.clear(true);
  markerSource.addFeatures(plan.points.map((p) => new ol.Feature({
    geometry: new ol.geom.Point([p.x, p.y]),
    number: p.number, locked: p.locked, group: p.group, tooltip: p.tooltip,
  })));
  lineSource.clear(true);
  lineSource.addFeatures(plan.lines.filter((ln) => !ln.outline && ln.xy.length > 1).map((ln) => new ol.Feature({
    geometry: new ol.geom.LineString(ln.xy), line_id: ln.line_id, numbers: ln.numbers,
    closed: ln.closed, kind: ln.kind,
  })));
  outlineSource.clear(true);
  outlineSource.addFeatures(plan.lines.filter((ln) => ln.outline && ln.xy.length > 1).map((ln) => new ol.Feature({
    // The closing corner is repeated, so a closed outline with an inside
    // has at least four coordinates.
    geometry: ln.closed && ln.xy.length > 3 ? new ol.geom.Polygon([ln.xy]) : new ol.geom.LineString(ln.xy),
    line_id: ln.line_id, numbers: ln.numbers, closed: ln.closed, kind: ln.kind, keep_out: ln.keep_out,
  })));
  setupSource.clear(true);
  setupSource.addFeatures(plan.setups.map((s) => new ol.Feature({
    geometry: new ol.geom.Point([s.x, s.y]), name: s.name,
  })));
  leaderSource.clear(true);
  if (plan.leaders.length) {
    leaderSource.addFeature(new ol.Feature(new ol.geom.MultiLineString(plan.leaders)));
  }
}

subscribe((state, prev) => {
  if (changed(state, prev, "plan", "site")) rebuild(state.plan);
});

onSelection((numbers) => {
  // The map highlights one point; with several rows selected there is
  // nothing sensible to highlight, so it clears.
  highlighted = numbers.length === 1 ? numbers[0] : null;
  markerLayer.changed();
});

onOutlineSelection(() => outlineLayer.changed());

export function markerAt(pixel) {
  return map.forEachFeatureAtPixel(pixel, (f) => f,
    { layerFilter: (layer) => layer === markerLayer, hitTolerance: 4 }) ?? null;
}

function outlineAt(pixel) {
  return map.forEachFeatureAtPixel(pixel, (f) => f,
    { layerFilter: (layer) => layer === outlineLayer, hitTolerance: 4 }) ?? null;
}

// --- dragging a planned mark -----------------------------------------------------------

// Dragging markers while placing new ones is maddening, so they only move in
// Navigate - and a measured point never moves, whatever the mode.
const translate = new ol.interaction.Translate({
  layers: [markerLayer],
  filter: (f) => planMode() === "navigate" && !f.get("locked"),
  hitTolerance: 4,
});
map.addInteraction(translate);

let dragStart = null;
translate.on("translatestart", (e) => {
  const f = e.features.item(0);
  dragStart = f ? f.getGeometry().getCoordinates().slice() : null;
});
translate.on("translating", (e) => {
  // Keep the break lines and outlines attached while the vertex moves.
  const f = e.features.item(0);
  if (!f) return;
  const number = f.get("number");
  const [x, y] = f.getGeometry().getCoordinates();
  for (const line of [...lineSource.getFeatures(), ...outlineSource.getFeatures()]) {
    const numbers = line.get("numbers");
    if (!numbers.includes(number)) continue;
    const geometry = line.getGeometry();
    const polygon = geometry instanceof ol.geom.Polygon;
    const coords = polygon ? geometry.getCoordinates()[0] : geometry.getCoordinates();
    const drawn = numbers.filter((n) => store.state.plan.points.some((p) => p.number === n));
    drawn.forEach((n, i) => { if (n === number) coords[i] = [x, y]; });
    if (line.get("closed") && coords.length > drawn.length) coords[coords.length - 1] = coords[0];
    geometry.setCoordinates(polygon ? [coords] : coords);
  }
});
translate.on("translateend", async (e) => {
  const f = e.features.item(0);
  if (!f || !dragStart) return;
  const [x, y] = f.getGeometry().getCoordinates();
  const moved = Math.hypot(x - dragStart[0], y - dragStart[1]) > 1e-6;
  dragStart = null;
  if (!moved) return;                 // a click, not a drag
  const number = f.get("number");
  const reply = await act("/api/plan/point/move", { number, x, y });
  if (!reply) rebuild(store.state.plan);        // refused: put it back
  requests.select(number);
});

// --- drawing a break line --------------------------------------------------------------

const draw = new ol.interaction.Draw({
  type: "LineString",
  style: new ol.style.Style({
    stroke: new ol.style.Stroke({ color: rgba(SELECTED), width: 2, lineDash: [6, 4] }),
    image: new ol.style.Circle({ radius: 4, fill: new ol.style.Fill({ color: rgba(SELECTED) }) }),
  }),
});
draw.on("drawend", (e) => {
  const vertices = e.feature.getGeometry().getCoordinates();
  // The server ignores fewer than two vertices: a stray click is not a line.
  act("/api/plan/line/add", { vertices });
});

// An outline closes itself: double-click the last corner, and the side back
// to the first is drawn.
const outlineDraw = new ol.interaction.Draw({
  type: "Polygon",
  style: new ol.style.Style({
    stroke: new ol.style.Stroke({ color: rgba(SELECTED), width: 2, lineDash: [6, 4] }),
    fill: new ol.style.Fill({ color: rgba(SELECTED, 0.12) }),
    image: new ol.style.Circle({ radius: 4, fill: new ol.style.Fill({ color: rgba(SELECTED) }) }),
  }),
});
outlineDraw.on("drawend", async (e) => {
  const vertices = e.feature.getGeometry().getCoordinates()[0];
  // The server ignores fewer than three corners: that is not an area.
  const reply = await act("/api/plan/outline/add", { vertices, kind: outlineKind() },
                          { busy: "Rebuilding the surface…" });
  if (reply?.outline) setSelectedOutline(reply.outline);
});

const DRAWS = { add_line: draw, add_outline: outlineDraw };

/** Drop whatever line or outline is being drawn. */
export function cancelLine() {
  draw.abortDrawing();
  outlineDraw.abortDrawing();
}

onPlanMode((mode, previous) => {
  if (DRAWS[previous]) {
    // Leaving the tool commits what was drawn, as long as it is a line (or
    // an outline with an inside).
    DRAWS[previous].finishDrawing();
    map.removeInteraction(DRAWS[previous]);
  }
  if (DRAWS[mode]) map.addInteraction(DRAWS[mode]);
  translate.setActive(mode === "navigate");
  map.getTargetElement().classList.toggle("placing", mode !== "navigate");
  hideTip();
});

// --- clicking --------------------------------------------------------------------------

map.on("click", async (e) => {
  const mode = planMode();
  if (mode === "navigate") {
    const hit = markerAt(e.pixel);
    if (hit) {
      requests.select(hit.get("number"));
      return;
    }
    // Off every marker, a click on an outline picks it in the outline list;
    // a click on open ground puts the picked one down.
    setSelectedOutline(outlineAt(e.pixel)?.get("line_id") ?? null);
    return;
  }
  const [x, y] = e.coordinate;
  if (mode === "add_point") {
    const reply = await act("/api/plan/point/add", { x, y });
    if (reply?.selected != null) requests.select(reply.selected);
  } else if (mode === "add_setup") {
    await act("/api/plan/setup/add", { x, y });
    setPlanMode("navigate");
  }
});

// --- hover ------------------------------------------------------------------------------

const tip = document.getElementById("map-tip");
function hideTip() { tip.hidden = true; }

map.on("pointermove", (e) => {
  const target = map.getTargetElement();
  if (e.dragging || planMode() !== "navigate") {
    target.classList.remove("over-marker", "draggable");
    hideTip();
    return;
  }
  const hit = markerAt(e.pixel);
  target.classList.toggle("over-marker", !!hit);
  target.classList.toggle("draggable", !!hit && !hit.get("locked"));
  if (!hit) { hideTip(); return; }
  tip.textContent = hit.get("tooltip");
  tip.style.left = `${target.offsetLeft + e.pixel[0] + 14}px`;
  tip.style.top = `${target.offsetTop + e.pixel[1] + 14}px`;
  tip.hidden = false;
});
map.getViewport().addEventListener("mouseleave", hideTip);
