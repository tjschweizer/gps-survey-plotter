// 3D surface view.
//
// Vertical exaggeration is a view setting, never a pipeline setting - the
// numbers underneath stay in real metres. It is applied as the plot's aspect
// ratio, so the axes still read true heights. The default of 8x is chosen
// because the site has roughly 1.2 m of relief across 46 m; at 1:1 a
// residential lawn looks perfectly flat and tells you nothing about drainage.
//
// Unmeasured cells are holes (null heights), the same refusal to colour
// ground that was never surveyed as the plan view makes.
//
// Plotly is large, so it is only loaded the first time this tab is opened.

import { store, subscribe, changed } from "./store.js";
import { getJSON } from "./api.js";
import { COLORMAPS } from "./map2d.js";

const plot = document.getElementById("plot3d");
const ve = document.getElementById("ve");
const veLabel = document.getElementById("ve-label");
const cmap = document.getElementById("cmap3d");
for (const name of COLORMAPS) cmap.append(new Option(name, name));
cmap.value = "terrain";

const DEFAULT_CAMERA = { eye: { x: 1.35, y: -1.35, z: 1.05 }, up: { x: 0, y: 0, z: 1 },
                         center: { x: 0, y: 0, z: -0.1 } };

let visible = false;
let plotly = null;
let grid = null;
let gridKey = null;
let cameraRevision = 0;

function webglAvailable() {
  try {
    const canvas = document.createElement("canvas");
    return !!(canvas.getContext("webgl2") || canvas.getContext("webgl"));
  } catch { return false; }
}

function loadPlotly() {
  if (!plotly) {
    plotly = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "/static/vendor/plotly/plotly-gl3d.min.js";
      script.onload = () => resolve(window.Plotly);
      script.onerror = () => reject(new Error("could not load the 3D library"));
      document.head.append(script);
    });
  }
  return plotly;
}

function message(text) {
  if (window.Plotly) window.Plotly.purge(plot);
  plot.replaceChildren(Object.assign(document.createElement("div"),
    { className: "message-3d", textContent: text }));
  plot.style.display = "grid";
}

export function setVisible(on) {
  visible = on;
  if (on) render();
}

export async function render() {
  if (!visible) return;
  const state = store.state;
  if (!webglAvailable()) {
    message("3D view unavailable — this browser has no WebGL.\n\n" +
            "The plan view and every export are unaffected.");
    return;
  }
  if (!state?.surface) {
    message("No surface yet. Load an export to see it in 3D.");
    return;
  }
  const key = `${state.rev.result}|${state.rev.site}|${cmap.value}`;
  if (key !== gridKey) {
    grid = await getJSON(`/api/surface/grid?cmap=${encodeURIComponent(cmap.value)}`);
    gridKey = key;
  }
  let Plotly;
  try {
    Plotly = await loadPlotly();
  } catch (err) {
    message(`3D view unavailable — ${err.message}.`);
    return;
  }
  if (!grid) return;
  if (plot.querySelector(".message-3d")) plot.replaceChildren();
  plot.style.display = "";

  const exaggeration = Number(ve.value);
  const xr = grid.x[grid.x.length - 1] - grid.x[0] || 1;
  const yr = grid.y[grid.y.length - 1] - grid.y[0] || 1;
  const zr = Math.max(grid.zmax - grid.zmin, 1e-3);

  const trace = {
    type: "surface",
    x: grid.x, y: grid.y, z: grid.z,
    colorscale: grid.colorscale,
    cmin: grid.zmin, cmax: grid.zmax,
    connectgaps: false,
    // Relief here is barely a metre, so ticks need two decimals to say
    // anything at all.
    colorbar: { title: { text: "elevation (m)", side: "right" }, tickformat: ".2f",
                len: 0.6, thickness: 14, x: 0.94 },
    lighting: { ambient: 0.35, diffuse: 0.7, specular: 0.15, roughness: 0.6 },
    hovertemplate: "E %{x:.2f} m<br>N %{y:.2f} m<br>%{z:.3f} m<extra></extra>",
  };
  const layout = {
    margin: { l: 0, r: 0, t: 0, b: 0 },
    paper_bgcolor: "#ffffff",
    uirevision: "keep",
    scene: {
      uirevision: `camera-${cameraRevision}`,
      aspectmode: "manual",
      aspectratio: { x: 1, y: yr / xr, z: (zr * exaggeration) / xr },
      xaxis: { title: { text: "east (m)" } },
      yaxis: { title: { text: "north (m)" } },
      zaxis: { title: { text: "elevation (m)" }, tickformat: ".2f" },
      camera: DEFAULT_CAMERA,
    },
  };
  await Plotly.react(plot, [trace], layout,
    { displaylogo: false, responsive: true, modeBarButtonsToRemove: ["toImage"] });
}

export function resetView() {
  cameraRevision += 1;
  render();
}

export function exaggeration() { return Number(ve.value); }

export function applyExaggeration(value) {
  ve.value = String(value);
  veLabel.textContent = `${ve.value}×`;
  render();
}

ve.addEventListener("input", () => {
  veLabel.textContent = `${ve.value}×`;
  render();
});
cmap.addEventListener("change", () => render());
document.getElementById("reset3d").addEventListener("click", resetView);
new ResizeObserver(() => {
  if (visible && window.Plotly && plot.data) window.Plotly.Plots.resize(plot);
}).observe(plot);

subscribe((state, prev) => {
  if (changed(state, prev, "result", "site")) render();
});
