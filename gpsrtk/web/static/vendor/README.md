# Vendored browser libraries

The page is served without a build step, so these are committed as the
distribution files their packages publish, unmodified apart from removing the
trailing `sourceMappingURL` comment (the maps are not vendored, and a
reference to a missing map makes browser dev tools log a 404).

There is deliberately no `package.json` or `node_modules`: this repository
lives in a OneDrive-synced folder, and a `node_modules` tree would fail there
for the same reason a `.venv` does (see the README's launcher section).

| Library | Version | Files | Licence | Used for |
|---|---|---|---|---|
| [OpenLayers](https://openlayers.org/) | 10.10.0 | `ol/ol.js`, `ol/ol.css` (npm `ol`, `dist/ol.js`) | BSD-2-Clause | the plan view |
| [Plotly.js](https://plotly.com/javascript/) gl3d bundle | 4.1.1 | `plotly/plotly-gl3d.min.js` (npm `plotly.js-gl3d-dist-min`) | MIT | the 3D view, loaded on first use |
| [Tabulator](https://tabulator.info/) | 6.5.3 | `tabulator/tabulator.min.js` (npm `tabulator-tables`, `dist/js`), `tabulator/tabulator.min.css` (`dist/css/tabulator_simple.min.css`) | MIT | the shot-plan readings table |

Each directory carries the package's own licence file.

## Updating one

Download the package tarball from the npm registry, copy the files listed
above over the old ones, strip the `sourceMappingURL` line, and update the
version here. Then run the browser tests (`uv run pytest -m browser`), which
exercise every API these libraries provide to the page.
