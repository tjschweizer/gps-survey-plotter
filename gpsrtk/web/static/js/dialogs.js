// Modal dialogs: notices, confirmations, the file browser and the datum tie.
//
// Each call makes its own <dialog> and removes it on close, so a dialog can
// open another (the save dialog asking whether to overwrite) without the two
// fighting over one element.

import { h, clear } from "./dom.js";

function modal(level, title, body, buttons, { onOpen, cls } = {}) {
  return new Promise((resolve) => {
    const dlg = h("dialog", { class: [level, cls].filter(Boolean).join(" ") });
    let result = null;
    const close = (value) => { result = value; dlg.close(); };
    const bar = h("div", { class: "dlg-buttons" },
      buttons.map((b) => b.href
        ? h("a", { class: "button", href: b.href, target: "_blank", rel: "noopener",
                   onclick: () => close(b.value ?? null) }, b.label)
        : h("button", { class: b.primary ? "primary" : "", type: "button",
                        onclick: () => (b.run ? b.run(close) : close(b.value)) }, b.label)));
    dlg.append(h("div", { class: "dlg" },
      h("div", { class: "dlg-title" }, h("span", { class: "badge" }), title),
      body, bar));
    dlg.addEventListener("close", () => { dlg.remove(); resolve(result); });
    dlg.addEventListener("cancel", () => { result = null; });
    document.body.append(dlg);
    dlg.showModal();
    onOpen?.(dlg, close);
    if (!onOpen) bar.querySelector("button.primary, button, a")?.focus();
  });
}

/** Show a message. Resolves to the action chosen, if any. */
export function showNotice({ title, text, level = "info", monospace = false, actions = [] }) {
  const body = h("div", { class: "dlg-body" + (monospace ? " mono" : "") }, text ?? "");
  const buttons = [
    ...actions.map((a) => ({ label: a.label, value: a, href: a.href, primary: !a.href })),
    { label: actions.length ? "Close" : "OK", value: null, primary: !actions.length },
  ];
  return modal(level, title, body, buttons);
}

export async function confirmDialog(title, text, { yes = "Yes", no = "No" } = {}) {
  const body = h("div", { class: "dlg-body" }, text);
  return (await modal("warning", title, body,
    [{ label: no, value: false }, { label: yes, value: true, primary: true }])) === true;
}

// --- the file browser ---------------------------------------------------------

export const FILTERS = {
  export: [
    { label: "Survey exports (*.zip *.swmz *.swm2 *.csv)", exts: [".zip", ".swmz", ".swm2", ".csv"] },
    { label: "SW Maps project (*.swmz *.swm2)", exts: [".swmz", ".swm2"] },
    { label: "SW Maps CSV export (*.zip *.csv)", exts: [".zip", ".csv"] },
    { label: "All files", exts: [] },
  ],
  project: [
    { label: "Yard survey project (*.yardproj)", exts: [".yardproj"] },
    { label: "All files", exts: [] },
  ],
  plan: [
    { label: "Shot plan (*.yardplan)", exts: [".yardplan"] },
    { label: "All files", exts: [] },
  ],
  raster: [
    { label: "Rasters (*.tif *.tiff *.png *.jpg)", exts: [".tif", ".tiff", ".png", ".jpg"] },
    { label: "All files", exts: [] },
  ],
};

function splitPath(path, sep) {
  const i = path.lastIndexOf(sep);
  return i < 0 ? ["", path] : [path.slice(0, i) || sep, path.slice(i + 1)];
}

function joinPath(dir, name, sep) {
  return dir.endsWith(sep) ? dir + name : dir + sep + name;
}

function sizeText(bytes) {
  if (bytes == null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

/**
 * Pick a file on this machine. mode "open" needs an existing file; mode
 * "save" takes a name and asks before overwriting. `suffix` is added to a
 * saved name that has none. Resolves to a full path, or null.
 */
export function fileDialog({ title, mode = "open", filters = FILTERS.export,
                             suggested = null, suffix = null }) {
  let listing = null;
  let filter = filters[0];
  let chosen = null;

  const pathInput = h("input", { type: "text", "aria-label": "Folder" });
  const list = h("div", { class: "list", role: "listbox", tabindex: "0" });
  const nameInput = h("input", { type: "text", "aria-label": "File name" });
  const filterSelect = h("select", { "aria-label": "File type" },
    filters.map((f, i) => h("option", { value: String(i) }, f.label)));
  const roots = h("select", { "aria-label": "Drive", title: "Drive" });
  const error = h("div", { class: "muted small" });

  const body = h("div", { class: "dlg-body form files" },
    h("div", { class: "pathbar" },
      roots,
      h("button", { type: "button", title: "Up one folder", onclick: () => listing?.parent && load(listing.parent) }, "Up"),
      pathInput,
      h("button", { type: "button", onclick: () => load(pathInput.value) }, "Go")),
    list,
    h("div", { class: "namebar" },
      h("span", {}, mode === "save" ? "Save as" : "File"), nameInput, filterSelect),
    error);

  let finish = null;

  async function load(dir) {
    const exts = filter.exts.join(",");
    const r = await fetch(`/api/files?dir=${encodeURIComponent(dir ?? "")}&exts=${encodeURIComponent(exts)}`);
    const reply = await r.json();
    if (!r.ok) {
      error.textContent = reply?.error?.text ?? "Could not read that folder.";
      return;
    }
    error.textContent = "";
    listing = reply;
    pathInput.value = listing.dir;
    clear(roots);
    for (const root of listing.roots) {
      roots.append(h("option", { value: root, selected: listing.dir.startsWith(root) }, root));
    }
    render();
  }

  function render() {
    clear(list);
    if (!listing.entries.length) list.append(h("div", { class: "entry muted" }, "", "Nothing here matches.", ""));
    for (const entry of listing.entries) {
      const row = h("div", {
        class: "entry" + (entry.dir ? " dir" : "") + (chosen === entry.path ? " chosen" : ""),
        role: "option", title: entry.path,
        onclick: () => {
          if (entry.dir) return;
          chosen = entry.path;
          nameInput.value = entry.name;
          render();
        },
        ondblclick: () => {
          if (entry.dir) load(entry.path);
          else { chosen = entry.path; nameInput.value = entry.name; accept(); }
        },
      },
      h("span", { class: "glyph" }, entry.dir ? "▸" : "·"),
      h("span", { class: "name" }, entry.name),
      h("span", { class: "size" }, entry.dir ? "" : sizeText(entry.size)));
      list.append(row);
    }
  }

  async function accept() {
    let name = nameInput.value.trim();
    if (!name || !listing) return;
    const sep = listing.sep;
    const isAbsolute = name.startsWith("/") || /^[A-Za-z]:[\\/]/.test(name);
    if (mode === "save" && suffix && !/\.[^\\/.]+$/.test(name)) name += suffix;
    const path = isAbsolute ? name : joinPath(listing.dir, name, sep);
    if (mode === "save") {
      const exists = listing.entries.some((e) => !e.dir && e.name === name);
      if (exists && !(await confirmDialog("Replace file?",
          `${name} already exists. Replace it?`, { yes: "Replace", no: "Cancel" }))) return;
    } else {
      const known = listing.entries.find((e) => e.name === name);
      if (known?.dir) { load(known.path); return; }
      if (!known && !isAbsolute) { error.textContent = `${name} is not in this folder.`; return; }
    }
    finish(path);
  }

  filterSelect.addEventListener("change", () => {
    filter = filters[Number(filterSelect.value)];
    load(listing?.dir);
  });
  roots.addEventListener("change", () => load(roots.value));
  pathInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); load(pathInput.value); } });
  nameInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); accept(); } });

  let start = null;
  if (suggested) {
    const sep = suggested.includes("\\") && !suggested.includes("/") ? "\\" : "/";
    const [dir, name] = splitPath(suggested, sep);
    start = dir;
    nameInput.value = name;
  }

  return modal("", title, body,
    [{ label: "Cancel", value: null },
     { label: mode === "save" ? "Save" : "Open", primary: true, run: () => accept() }],
    {
      cls: "file-dialog",
      onOpen: (dlg, close) => {
        finish = (path) => close(path);
        load(start).then(() => (mode === "save" ? nameInput : list).focus());
      },
    });
}

// --- the datum tie --------------------------------------------------------------

export function datumDialog(form) {
  const point = h("input", { type: "text", value: String(form.point), list: "datum-points" });
  const points = h("datalist", { id: "datum-points" }, form.points.map((p) => h("option", { value: String(p) })));
  const elev = h("input", { type: "number", step: "0.0001", value: String(form.elev_ft) });
  const note = h("input", { type: "text", value: form.note, placeholder: "garage slab at the overhead door, say" });
  const frame = h("input", { type: "text", value: form.frame });
  const tied = h("input", { type: "checkbox", checked: form.tied });
  const warning = h("div", {});
  const sync = () => {
    warning.textContent = tied.checked ? form.tied_note : form.local_note;
    warning.style.color = tied.checked ? "var(--accent)" : "var(--muted)";
  };
  tied.addEventListener("change", sync);
  sync();

  const body = h("div", { class: "dlg-body form", style: { maxWidth: "470px" } },
    h("div", { class: "muted", style: { whiteSpace: "pre-wrap" } }, form.help),
    h("div", { class: "grid2" },
      h("label", {}, "Benchmark point"), h("div", {}, point, points),
      h("label", {}, "Held at elevation (ft)"), elev,
      h("label", {}, "What was shot"), note,
      h("label", {}, "Reference frame"), frame,
      h("span"), h("label", {}, tied, "This is a real elevation in that frame, not a chosen number")),
    warning);

  return modal("", "Datum tie", body,
    [{ label: "Cancel", value: null }, { label: "OK", value: "ok", primary: true }],
    { onOpen: () => point.focus() })
    .then((value) => value === "ok" ? {
      point: point.value, elev_ft: elev.value, note: note.value,
      frame: frame.value, tied: tied.checked,
    } : null);
}
