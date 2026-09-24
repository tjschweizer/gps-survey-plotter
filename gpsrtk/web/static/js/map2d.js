// Plan view.
//
// Draw order, bottom to top: aerial imagery, the derived surface, reference
// linework, points, spot markers, then the shot plan (planmap.js).
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
};
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
const pointSource = new ol.source.Vector();
const pointStyles = new Map();
function pointStyle(feature) {
  const key = feature.get("c");
  let style = pointStyles.get(key);
  if (!style) {
    const r = (key >>> 24) & 255, g = (key >>> 16) & 255, b = (key >>> 8) & 255, a = key & 255;
    style = new ol.style.Style({
      image: new ol.style.Circle({
        radius: 1.4,
        fill: new ol.style.Fill({ color: `rgba(${r},${g},${b},${(a / 255).toFixed(3)})` }),
      }),
    });
    pointStyles.set(key, style);
  }
  return style;
}
const pointLayer = new ol.layer.VectorImage({ source: pointSource, zIndex: 120, style: pointStyle });
map.addLayer(pointLayer);

const markerSource = new ol.source.Vector();
const markerLayer = new ol.layer.Vector({
  source: markerSource,
  zIndex: 130,
  style: new ol.style.Style({
    image: new ol.style.RegularShape({
      points: 4, radius: 7.5, angle: Math.PI / 4,
      fill: new ol.style.Fill({ color: "rgba(255,60,60,0.86)" }),
      stroke: new ol.style.Stroke({ color: "#000", width: 1 }),
    }),
  }),
});
map.addLayer(markerLayer);

// --- keeping up with the state ----------------------------------------------------

let pointsKey = null;
let pointsToken = 0;
let featuresKey = null;
let homeKey = null;

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
  const url = `/api/surface.png?cmap=${encodeURIComponent(cmap)}&r=${state.rev.result}.${state.rev.site}`;
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
  const key = `${state.rev.result}|${state.rev.site}|${colorBy}|${cmap}`;
  pointLayer.setVisible(controls.points.checked);
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
  markerSource.addFeatures(markers.map((xy) => new ol.Feature(new ol.geom.Point(xy))));
  vectorSource.clear(true);
  vectorSource.addFeatures(vectors.map((v) => new ol.Feature(new ol.geom.LineString(v.xy))));
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
  if (home) map.getView().fit(home, { size: map.getSize() });
}

export function redraw(state = store.state) {
  if (!state) return;
  syncBasemaps(state);
  syncSurface(state);
  syncPoints(state).catch((err) => console.error(err));
  syncFeatures(state).catch((err) => console.error(err));
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

for (const el of Object.values(controls)) {
  el.addEventListener(el.type === "range" ? "input" : "change", () => redraw());
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
  };
}

export function applyView(v) {
  if (!v) return;
  if (COLORMAPS.includes(v.colormap)) controls.cmap.value = v.colormap;
  if (COLOR_BY.includes(v.color_by)) controls.colorBy.value = v.color_by;
  for (const [key, el] of [["show_imagery", controls.imagery], ["show_surface", controls.surface],
                           ["show_points", controls.points], ["show_reference", controls.reference]]) {
    if (key in v) el.checked = !!v[key];
  }
  if ("surface_opacity" in v) controls.opacity.value = String(v.surface_opacity);
  redraw();
}

// --- where the pointer is ------------------------------------------------------------

map.on("pointermove", (e) => {
  const [x, y] = e.coordinate;
  byId("cursor").textContent =
    `E ${x.toFixed(2)}  N ${y.toFixed(2)} m   (${(x / 0.3048).toFixed(2)}, ${(y / 0.3048).toFixed(2)} ft)`;
});
map.getViewport().addEventListener("mouseleave", () => { byId("cursor").textContent = ""; });
