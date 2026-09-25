// Plan view.
//
// Draw order, bottom to top: aerial imagery, the derived surface (coloured
// by elevation or by slope), contours, reference linework, points, drainage
// arrows, spot markers, then the shot plan (planmap.js).
//
// Plotted in metres from the site's local origin, not raw UTM - raw northings
// are ~4.65 million, unreadable on screen and wasteful of float precision -
// and in a plain Cartesian frame rather than web mercator, since everything
// arrives already in the site's projected CRS.
//
// Unmeasured surface cells arrive fully transparent, so with imagery
// underneath the holes show the aerial photo, which reads correctly as "we
// did not survey this". Reference linework is dashed and the info line says
// REFERENCE ONLY, because parcel polygons are cartographic to a foot or three.

import { store, subscribe } from "./store.js";
import { getBuffer, getJSON } from "./api.js";
import { h, clear, remember, recall } from "./dom.js";

export const COLORMAPS = ["terrain", "gist_earth", "viridis", "cividis", "magma", "Spectral_r", "gray"];
export const COLOR_BY = ["elevation", "fix quality", "speed", "session"];

export const projection = new ol.proj.Projection({
  code: "YARD-LOCAL",
  units: "m",
  extent: [-50000, -50000, 50000, 50000],
});

const byId = (id) => document.getElementById(id);
export const controls = {
  imagery: byId("show-imagery"),
  surface: byId("show-surface"),
  opacity: byId("surface-opacity"),
  points: byId("show-points"),
  reference: byId("show-reference"),
  colorBy: byId("color-by"),
  cmap: byId("cmap"),
  mode: byId("surface-mode"),
  slopeMax: byId("slope-max"),
  contours: byId("show-contours"),
  interval: byId("contour-interval"),
  drainage: byId("show-drainage"),
};
const ARROW_COLOUR = "#1fb4c8";
const ARROW_SPACING_M = 1.5;
for (const name of COLOR_BY) controls.colorBy.append(new Option(name, name));
for (const name of COLORMAPS) controls.cmap.append(new Option(name, name));
controls.cmap.value = "terrain";

export const map = new ol.Map({
  target: "map",
  view: new ol.View({
    projection,
    center: [20, 15],
    resolution: 0.1,
    minResolution: 0.002,
    maxResolution: 40,
  }),
  controls: ol.control.defaults.defaults({ attribution: false, rotate: false })
    .extend([new ol.control.ScaleLine({ units: "metric", minWidth: 90 })]),
  // A double-click finishes a break line; it must not also zoom the map.
  interactions: ol.interaction.defaults.defaults({
    doubleClickZoom: false, altShiftDragRotate: false, pinchRotate: false,
  }),
});

new ResizeObserver(() => map.updateSize()).observe(byId("map"));

// --- layers ----------------------------------------------------------------

const basemapLayers = new Map();

const surfaceLayer = new ol.layer.Image({ zIndex: 100, opacity: 0.85 });
map.addLayer(surfaceLayer);

// Contours above the surface, below everything that is a measurement.
// Labelled every second level, in cm above the lowest measured point.
const contourSource = new ol.source.Vector();
const contourStyles = new Map();
function contourStyle(feature) {
  const major = feature.get("major");
  const key = major ? `M${feature.get("cm")}` : "m";
  let style = contourStyles.get(key);
  if (!style) {
    style = new ol.style.Style({
      stroke: new ol.style.Stroke({ color: major ? "rgba(20,20,20,0.85)" : "rgba(20,20,20,0.6)",
                                    width: major ? 1.2 : 0.7 }),
      text: major ? new ol.style.Text({
        text: String(feature.get("cm")), placement: "line", repeat: 320,
        font: "600 11px system-ui, sans-serif",
        fill: new ol.style.Fill({ color: "#111" }),
        stroke: new ol.style.Stroke({ color: "rgba(255,255,255,0.9)", width: 3 }),
      }) : undefined,
    });
    contourStyles.set(key, style);
  }
  return style;
}
const contourLayer = new ol.layer.Vector({ source: contourSource, zIndex: 105, style: contourStyle });
map.addLayer(contourLayer);

const vectorSource = new ol.source.Vector();
const vectorLayer = new ol.layer.Vector({
  source: vectorSource,
  zIndex: 110,
  style: new ol.style.Style({
    stroke: new ol.style.Stroke({ color: "rgb(200,40,160)", width: 1.6, lineDash: [7, 5] }),
  }),
});
map.addLayer(vectorLayer);

// Points are one image re-rendered when the view settles, rather than tens of
// thousands of live vector features redrawn on every frame of a pan.
//
// Over the surface they are drawn small and faint: some 50,000 full-size dots
// hid the very surface they were gridded into. Alone they are drawn full.
const POINT_RADIUS = { alone: 1.4, underSurface: 0.75 };
const POINT_OPACITY_UNDER_SURFACE = 0.35;
let pointRadius = POINT_RADIUS.alone;
const pointSource = new ol.source.Vector();
const pointStyles = new Map();
function pointStyle(feature) {
  const key = `${feature.get("c")}|${pointRadius}`;
  let style = pointStyles.get(key);
  if (!style) {
    const c = feature.get("c");
    const r = (c >>> 24) & 255, g = (c >>> 16) & 255, b = (c >>> 8) & 255, a = c & 255;
    style = new ol.style.Style({
      image: new ol.style.Circle({
        radius: pointRadius,
        fill: new ol.style.Fill({ color: `rgba(${r},${g},${b},${(a / 255).toFixed(3)})` }),
      }),
    });
    pointStyles.set(key, style);
  }
  return style;
}
const pointLayer = new ol.layer.VectorImage({ source: pointSource, zIndex: 120, style: pointStyle });
map.addLayer(pointLayer);

// Drainage: one fixed-length arrow per lattice point, pointing downhill. The
// colour under it already says how steep; the arrow says which way.
const drainSource = new ol.source.Vector();
const drainLayer = new ol.layer.Vector({ source: drainSource, zIndex: 125 });
map.addLayer(drainLayer);

function arrowFeature([x, y, de, dn], length) {
  const tail = [x - (de * length) / 2, y - (dn * length) / 2];
  const head = [x + (de * length) / 2, y + (dn * length) / 2];
  const feature = new ol.Feature(new ol.geom.LineString([tail, head]));
  feature.setStyle([
    new ol.style.Style({ stroke: new ol.style.Stroke({ color: ARROW_COLOUR, width: 2.2 }) }),
    new ol.style.Style({
      geometry: new ol.geom.Point(head),
      image: new ol.style.RegularShape({
        points: 3, radius: 5, rotateWithView: true,
        // A triangle's first point is up (north); turn it to face downhill.
        rotation: Math.atan2(de, dn),
        fill: new ol.style.Fill({ color: ARROW_COLOUR }),
      }),
    }),
  ]);
  return feature;
}

// SW Maps shots: a square each, labelled with the station the datum dialog
// asks for once zoomed in far enough to read it, and with the rod reading
// and date closer still.
const markerSource = new ol.source.Vector();
const spotSquare = new ol.style.RegularShape({
  points: 4, radius: 7.5, angle: Math.PI / 4,
  fill: new ol.style.Fill({ color: "rgba(255,60,60,0.86)" }),
  stroke: new ol.style.Stroke({ color: "#000", width: 1 }),
});
const SPOT_LABEL_RES = 0.12;       // metres per pixel: the station
const SPOT_DETAIL_RES = 0.04;      // ...and the kind, rod reading and date
function spotStyle(feature, resolution) {
  if (resolution > SPOT_LABEL_RES) return new ol.style.Style({ image: spotSquare });
  const s = feature.get("spot");
  let text = s.station;
  if (resolution <= SPOT_DETAIL_RES) {
    const bits = [s.kind, s.rod == null ? null : `${s.rod} in`, s.date].filter(Boolean);
    if (bits.length) text += `\n${bits.join(" · ")}`;
  }
  return new ol.style.Style({
    image: spotSquare,
    text: new ol.style.Text({
      text, offsetY: -16, textBaseline: "bottom", font: "600 11px system-ui, sans-serif",
      fill: new ol.style.Fill({ color: "#5a0000" }),
      stroke: new ol.style.Stroke({ color: "rgba(255,255,255,0.9)", width: 3 }),
    }),
  });
}
const markerLayer = new ol.layer.Vector({
  source: markerSource, zIndex: 130, style: spotStyle, declutter: true,
});
map.addLayer(markerLayer);

// --- keeping up with the state ----------------------------------------------------

let pointsKey = null;
let pointsToken = 0;
let featuresKey = null;
let homeKey = null;
let contoursKey = null;
let drainKey = null;

function syncBasemaps(state) {
  const wanted = new Map(state.basemaps.map((b, i) => [b.name, [b, i]]));
  for (const [name, layer] of basemapLayers) {
    if (!wanted.has(name)) {
      map.removeLayer(layer);
      basemapLayers.delete(name);
    }
  }
  for (const [name, [b, depth]] of wanted) {
    let layer = basemapLayers.get(name);
    if (!layer) {
      layer = new ol.layer.Image({});
      map.addLayer(layer);
      basemapLayers.set(name, layer);
    }
    // The alignment offset is already in the extent: it moves the PHOTO and
    // never the measurements.
    const url = `/api/basemap.png?name=${encodeURIComponent(name)}&k=${b.key}`;
    const key = url + "|" + b.extent.join(",");
    if (layer.get("key") !== key) {
      layer.setSource(new ol.source.ImageStatic({ url, imageExtent: b.extent, projection }));
      layer.set("key", key);
    }
    // Photographs underneath LiDAR products, in the order they were declared.
    layer.setZIndex(depth);
    layer.setOpacity(b.opacity);
    layer.setVisible(controls.imagery.checked && b.visible);
  }
}

export function previewBasemapOpacity(name, opacity) {
  basemapLayers.get(name)?.setOpacity(opacity);
}

function syncSurface(state) {
  if (!state.surface) {
    surfaceLayer.setSource(null);
    surfaceLayer.set("key", null);
    return;
  }
  const cmap = controls.cmap.value;
  const mode = controls.mode.value;
  const shade = mode === "slope"
    ? `mode=slope&slope_max=${slopeMax()}`
    : `mode=elevation&cmap=${encodeURIComponent(cmap)}`;
  const url = `/api/surface.png?${shade}&r=${state.rev.result}.${state.rev.site}`;
  const key = url + "|" + state.surface.extent.join(",");
  if (surfaceLayer.get("key") !== key) {
    surfaceLayer.setSource(new ol.source.ImageStatic({
      url, imageExtent: state.surface.extent, projection,
      // One pixel is one surface cell; smoothing it would blur the mask edge.
      interpolate: false,
    }));
    surfaceLayer.set("key", key);
  }
  surfaceLayer.setVisible(controls.surface.checked);
  surfaceLayer.setOpacity(Number(controls.opacity.value) / 100);
}

async function syncPoints(state) {
  const colorBy = controls.colorBy.value;
  const cmap = controls.cmap.value;
  // Hiding a session redraws the points even when the surface is unchanged.
  const key = `${state.rev.result}|${state.rev.site}|${state.rev.sessions}|${colorBy}|${cmap}`;
  pointLayer.setVisible(controls.points.checked);
  const underSurface = controls.surface.checked && !!state.surface;
  const radius = underSurface ? POINT_RADIUS.underSurface : POINT_RADIUS.alone;
  pointLayer.setOpacity(underSurface ? POINT_OPACITY_UNDER_SURFACE : 1);
  if (radius !== pointRadius) {
    pointRadius = radius;
    pointLayer.changed();
  }
  if (key === pointsKey) return;
  pointsKey = key;
  const token = ++pointsToken;
  if (!state.has_data) {
    pointSource.clear(true);
    return;
  }
  const buf = await getBuffer(`/api/points?color_by=${encodeURIComponent(colorBy)}&cmap=${encodeURIComponent(cmap)}`);
  if (token !== pointsToken) return;           // a newer request superseded this one
  const view = new DataView(buf);
  const n = view.getUint32(4, true);
  const xs = new Float32Array(buf, 8, n);
  const ys = new Float32Array(buf, 8 + 4 * n, n);
  const rgba = new Uint8Array(buf, 8 + 8 * n, 4 * n);
  const features = new Array(n);
  for (let i = 0; i < n; i++) {
    const c = ((rgba[4 * i] << 24) | (rgba[4 * i + 1] << 16) | (rgba[4 * i + 2] << 8) | rgba[4 * i + 3]) >>> 0;
    features[i] = new ol.Feature({ geometry: new ol.geom.Point([xs[i], ys[i]]), c });
  }
  pointSource.clear(true);
  pointSource.addFeatures(features);
}

async function syncFeatures(state) {
  const key = ["layers", "selection", "imagery", "site"].map((t) => state.rev[t]).join("|");
  vectorLayer.setVisible(controls.reference.checked);
  if (key === featuresKey) return;
  featuresKey = key;
  const { markers, vectors } = await getJSON("/api/features");
  markerSource.clear(true);
  markerSource.addFeatures(markers.map((m) => new ol.Feature({
    geometry: new ol.geom.Point([m.x, m.y]), spot: m,
  })));
  vectorSource.clear(true);
  vectorSource.addFeatures(vectors.map((v) => new ol.Feature(new ol.geom.LineString(v.xy))));
  syncLegend(store.state);          // its key lists the spot squares now drawn
}

function slopeMax() {
  const v = Number(controls.slopeMax.value);
  return Number.isFinite(v) && v >= 1 ? Math.min(v, 50) : 10;
}

async function syncContours(state) {
  const on = controls.contours.checked && !!state.surface;
  contourLayer.setVisible(on);
  if (!on) return;
  const interval = controls.interval.value;
  const key = `${state.rev.result}|${state.rev.site}|${interval}`;
  if (key === contoursKey) return;
  contoursKey = key;
  const data = await getJSON(`/api/contours?interval_cm=${interval}`);
  if (key !== contoursKey) return;
  contourSource.clear(true);
  if (!data) return;
  const features = [];
  for (const level of data.levels) {
    for (const line of level.lines) {
      features.push(new ol.Feature({ geometry: new ol.geom.LineString(line),
                                     cm: level.cm, major: level.major }));
    }
  }
  contourSource.addFeatures(features);
}

async function syncDrainage(state) {
  const on = controls.drainage.checked && !!state.surface;
  drainLayer.setVisible(on);
  if (!on) return;
  const key = `${state.rev.result}|${state.rev.site}`;
  if (key === drainKey) return;
  drainKey = key;
  const data = await getJSON(`/api/drainage?spacing_m=${ARROW_SPACING_M}`);
  if (key !== drainKey) return;
  drainSource.clear(true);
  if (!data) return;
  const length = 0.6 * data.spacing_m;
  drainSource.addFeatures(data.arrows.map((a) => arrowFeature(a, length)));
}

// --- the legend ---------------------------------------------------------------

// Collapsible, and the home view is framed with room for it (`resetView`),
// so at 1280 px it no longer sits on the survey. Where the map is narrow it
// starts folded to its title.
const NARROW_MAP_PX = 700;
const legendBody = h("div", { class: "legend-body" });
const legendToggle = h("button", {
  type: "button", class: "legend-toggle", "aria-expanded": "true", title: "Fold the legend",
  onclick: () => setLegendOpen(legendBody.hidden, true),
});
const legendHead = h("div", { class: "legend-head" }, h("span", { class: "legend-name" }), legendToggle);
const legend = h("div", { class: "map-legend", "aria-live": "polite" }, legendHead, legendBody);
map.addControl(new ol.control.Control({ element: legend }));

function setLegendOpen(open, byUser = false) {
  legendBody.hidden = !open;
  legendToggle.setAttribute("aria-expanded", String(open));
  legendToggle.textContent = open ? "▾" : "▸";
  legendToggle.title = open ? "Fold the legend" : "Show the legend";
  if (byUser) remember("legend-open", open);
}
setLegendOpen(recall("legend-open", byId("map").clientWidth >= NARROW_MAP_PX || !byId("map").clientWidth));

function gradient(stops) {
  return `linear-gradient(to right, ${stops.map(([t, c]) => `${c} ${(t * 100).toFixed(1)}%`).join(", ")})`;
}

/** Five labels under a colour bar, at its ends and quarters. */
function ticks(lo, hi, format, last = format) {
  return h("div", { class: "ticks" },
    [0, 1, 2, 3, 4].map((i) => h("span", {}, (i === 4 ? last : format)(lo + ((hi - lo) * i) / 4))));
}

// What the map's marker shapes are. Colour on a plan shot is its purpose
// group, as in the shot-plan table.
const KEY = [
  ["spot", "SW Maps shot (station on zoom)"],
  ["plan terrain", "plan shot: terrain"],
  ["plan feature", "plan shot: built feature"],
  ["plan control", "plan shot: control"],
  ["setup", "laser setup"],
];

function syncLegend(state) {
  const t = state.terrain;
  const showSurface = controls.surface.checked && t;
  const plan = state.plan ?? { points: [], setups: [] };
  const groups = new Set(plan.points.filter((p) => !p.guide).map((p) => p.group));
  const keys = KEY.filter(([k]) =>
    (k === "spot" && markerSource.getFeatures().length) ||
    (k.startsWith("plan ") && groups.has(k.slice(5))) ||
    (k === "setup" && plan.setups.length));
  legend.hidden = !(showSurface || keys.length
                    || (t && (controls.contours.checked || controls.drainage.checked)));
  byId("slope-max-label").hidden = controls.mode.value !== "slope";
  if (legend.hidden) return;
  clear(legendBody);
  let name = "Legend";
  if (showSurface) {
    if (controls.mode.value === "slope") {
      name = "Slope (%)";
      const max = slopeMax();
      legendBody.append(
        h("div", { class: "bar", style: { background: gradient(state.scales.magma_r) } }),
        ticks(0, max, (v) => v.toFixed(v < 10 && max < 20 ? 1 : 0), () => `≥ ${max}`),
        h("div", { class: "note" },
          `median ${t.slope.median?.toFixed(1) ?? "–"}%, p90 ${t.slope.p90?.toFixed(1) ?? "–"}%`));
    } else {
      const e = t.elevation;
      name = `Elevation (ft, ${e.datum})`;
      legendBody.append(
        h("div", { class: "bar", style: { background: gradient(state.scales[controls.cmap.value]) } }),
        ticks(e.lo_ft, e.hi_ft, (v) => v.toFixed(2)),
        h("div", { class: "note" }, `${t.relief_cm.toFixed(0)} cm relief · ${Math.round(t.mapped_m2).toLocaleString()} m² mapped`));
    }
  }
  legendHead.querySelector(".legend-name").textContent = name;
  if (controls.contours.checked && t) {
    legendBody.append(h("div", { class: "note" },
      `contours every ${controls.interval.value} cm, labelled in cm above the lowest point`));
  }
  if (controls.drainage.checked && t) {
    legendBody.append(h("div", { class: "note" }, h("span", { class: "arrow" }, "➜ "), "points downhill"));
  }
  if (keys.length) {
    legendBody.append(h("div", { class: "key" },
      keys.map(([k, text]) => h("div", {}, h("span", { class: `mark ${k}` }), text))));
  }
}

function syncInfo(state) {
  const bits = [];
  if (controls.points.checked && state.points_note) bits.push(state.points_note);
  bits.push(...state.info);
  const info = byId("map-info");
  info.textContent = bits.join("  ·  ");
  info.title = info.textContent;          // the line elides; the tooltip does not
}

/** Frame the surveyed ground, not everything on the canvas.
 *
 * Reference linework returns whole parcels that merely intersect the request
 * box, so some of it runs hundreds of metres off-site. Fitting to that would
 * shrink the lot to a thumbnail.
 */
export function resetView() {
  const home = store.state?.home;
  if (!home) return;
  // Leave the legend its own margin, so it frames the survey rather than
  // covering it.
  const room = legend.hidden ? 0 : legend.offsetWidth + 16;
  const size = map.getSize();
  const right = size && size[0] - room > 200 ? room : 0;
  map.getView().fit(home, { size, padding: [8, right, 8, 8] });
}

export function redraw(state = store.state) {
  if (!state) return;
  syncBasemaps(state);
  syncSurface(state);
  syncPoints(state).catch((err) => console.error(err));
  syncFeatures(state).catch((err) => console.error(err));
  syncContours(state).catch((err) => console.error(err));
  syncDrainage(state).catch((err) => console.error(err));
  syncLegend(state);
  syncInfo(state);
}

subscribe((state, prev) => {
  redraw(state);
  // New ground: frame it. A filter change leaves the view where you put it.
  const home = JSON.stringify(state.home);
  if (home !== homeKey) {
    homeKey = home;
    if (state.home) setTimeout(resetView, 0);
  }
});

// Slope mode stacks colour, arrows and squares; the points on top of all
// that are noise, so they are off there unless asked for. The choice is
// remembered per shade mode. (Registered before the redraw below.)
const pointsDefault = (mode) => mode !== "slope";
controls.mode.addEventListener("change", () => {
  const mode = controls.mode.value;
  controls.points.checked = recall(`points-${mode}`, pointsDefault(mode));
});
controls.points.addEventListener("change", () => {
  remember(`points-${controls.mode.value}`, controls.points.checked);
});

for (const el of Object.values(controls)) {
  el.addEventListener(el.type === "range" ? "input" : "change", () => redraw());
}

/** Colour the points by session: what the Sessions panel's swatches mean. */
export function colourBySession() {
  controls.colorBy.value = "session";
  redraw();
}

// --- view settings, as saved in a project ------------------------------------------

export function viewSettings() {
  return {
    colormap: controls.cmap.value,
    color_by: controls.colorBy.value,
    show_imagery: controls.imagery.checked,
    show_surface: controls.surface.checked,
    show_points: controls.points.checked,
    show_reference: controls.reference.checked,
    surface_opacity: Number(controls.opacity.value),
    surface_mode: controls.mode.value,
    slope_max: slopeMax(),
    show_contours: controls.contours.checked,
    contour_interval_cm: Number(controls.interval.value),
    show_drainage: controls.drainage.checked,
  };
}

export function applyView(v) {
  if (!v) return;
  if (COLORMAPS.includes(v.colormap)) controls.cmap.value = v.colormap;
  if (COLOR_BY.includes(v.color_by)) controls.colorBy.value = v.color_by;
  for (const [key, el] of [["show_imagery", controls.imagery], ["show_surface", controls.surface],
                           ["show_points", controls.points], ["show_reference", controls.reference],
                           ["show_contours", controls.contours], ["show_drainage", controls.drainage]]) {
    if (key in v) el.checked = !!v[key];
  }
  if ("surface_opacity" in v) controls.opacity.value = String(v.surface_opacity);
  if (v.surface_mode === "slope" || v.surface_mode === "elevation") controls.mode.value = v.surface_mode;
  if (Number(v.slope_max) >= 1) controls.slopeMax.value = String(v.slope_max);
  if ([...controls.interval.options].some((o) => Number(o.value) === Number(v.contour_interval_cm))) {
    controls.interval.value = String(Number(v.contour_interval_cm));
  }
  redraw();
}

// --- where the pointer is ------------------------------------------------------------

map.on("pointermove", (e) => {
  const [x, y] = e.coordinate;
  byId("cursor").textContent =
    `E ${x.toFixed(2)}  N ${y.toFixed(2)} m   (${(x / 0.3048).toFixed(2)}, ${(y / 0.3048).toFixed(2)} ft)`;
});
map.getViewport().addEventListener("mouseleave", () => { byId("cursor").textContent = ""; });
