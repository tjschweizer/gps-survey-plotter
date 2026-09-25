// Modal dialogs: notices, confirmations, the file browser and the datum tie.
//
// Each call makes its own <dialog> and removes it on close, so a dialog can
// open another (the save dialog asking whether to overwrite) without the two
// fighting over one element.

import { h, clear } from "./dom.js";

const SEVERITY = { info: "Note", warning: "Warning", error: "Error", confirm: "Confirm" };

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
    // Severity in a word as well as the badge's colour, which not everyone
    // can tell apart.
    const word = SEVERITY[level];
    dlg.append(h("div", { class: "dlg" },
      h("div", { class: "dlg-title" }, h("span", { class: "badge", "aria-hidden": "true" }),
        word ? h("span", { class: "severity" }, word) : null, title),
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

// --- toasts ------------------------------------------------------------------------

// A report that something worked - a solve, a fetch, an export - is read and
// set aside, so it does not take the page hostage the way a warning should.
// It waits in a corner, with its details a click away, until dismissed.
const MAX_TOASTS = 3;
let toastBox = null;

export function showToast({ title, text, monospace = false, actions = [] }, onAction) {
  if (!toastBox) {
    toastBox = h("div", { id: "toasts", role: "status", "aria-live": "polite" });
    document.body.append(toastBox);
  }
  const full = text ?? "";
  const summary = full.split("\n").find((line) => line.trim()) ?? "";
  const details = h("div", { class: "toast-details" + (monospace ? " mono" : ""), hidden: true }, full);
  const toggle = h("button", { type: "button", class: "link", "aria-expanded": "false",
    onclick: () => {
      details.hidden = !details.hidden;
      toggle.setAttribute("aria-expanded", String(!details.hidden));
      toggle.textContent = details.hidden ? "Details" : "Hide details";
    } }, "Details");
  const el = h("div", { class: "toast" },
    h("div", { class: "toast-head" },
      h("span", { class: "badge" }), h("strong", {}, title),
      h("button", { type: "button", class: "icon close", title: "Dismiss",
                    "aria-label": "Dismiss", onclick: () => el.remove() }, "✕")),
    h("div", { class: "toast-summary" }, summary),
    details,
    h("div", { class: "toast-actions" },
      full.trim() !== summary.trim() ? toggle : null,
      actions.map((a) => a.href
        ? h("a", { class: "button", href: a.href, target: "_blank", rel: "noopener" }, a.label)
        : h("button", { type: "button", class: "primary",
                        onclick: () => { el.remove(); onAction?.(a); } }, a.label))));
  toastBox.append(el);
  while (toastBox.children.length > MAX_TOASTS) toastBox.firstElementChild.remove();
  return el;
}

export async function confirmDialog(title, text, { yes = "Yes", no = "No" } = {}) {
  const body = h("div", { class: "dlg-body" }, text);
  return (await modal("confirm", title, body,
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

function dateText(mtime) {
  if (mtime == null) return "";
  const d = new Date(mtime * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
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
  // Sorted by name, or newest first by date; folders always lead. The
  // keyboard moves `active` through the rows, as a list box should.
  let sortBy = "name";
  let rows = [];
  let active = -1;

  const pathInput = h("input", { type: "text", "aria-label": "Folder" });
  const list = h("div", { class: "list", role: "listbox", tabindex: "0", "aria-label": "Files" });
  const sortButton = (key, label) => h("button", {
    type: "button", class: "sort", dataset: { key },
    onclick: () => { sortBy = key; render(); },
  }, label);
  const header = h("div", { class: "entry header" }, h("span"),
    sortButton("name", "Name"), sortButton("date", "Modified"), h("span", { class: "size" }, "Size"));
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
    h("div", { class: "table" }, header, list),
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

  function sorted(entries) {
    const byName = (a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase());
    const dirs = entries.filter((e) => e.dir).sort(byName);
    const files = entries.filter((e) => !e.dir)
      .sort(sortBy === "date" ? (a, b) => (b.mtime ?? 0) - (a.mtime ?? 0) || byName(a, b) : byName);
    return [...dirs, ...files];
  }

  function render() {
    clear(list);
    for (const b of header.querySelectorAll("button.sort")) {
      b.setAttribute("aria-pressed", String(b.dataset.key === sortBy));
    }
    if (!listing.entries.length) list.append(h("div", { class: "entry muted" }, "", "Nothing here matches.", "", ""));
    rows = sorted(listing.entries);
    if (active >= rows.length) active = rows.length - 1;
    rows.forEach((entry, i) => {
      const row = h("div", {
        id: `file-${i}`,
        class: "entry" + (entry.dir ? " dir" : "") + (chosen === entry.path ? " chosen" : "")
               + (i === active ? " active" : ""),
        role: "option", title: entry.path, "aria-selected": String(chosen === entry.path),
        onclick: () => {
          active = i;
          if (entry.dir) { render(); return; }
          choose(entry);
        },
        ondblclick: () => open(entry),
      },
      h("span", { class: "glyph" }, entry.dir ? "▸" : "·"),
      h("span", { class: "name" }, entry.name),
      h("span", { class: "date" }, entry.dir ? "" : dateText(entry.mtime)),
      h("span", { class: "size" }, entry.dir ? "" : sizeText(entry.size)));
      list.append(row);
    });
    if (active >= 0) {
      list.setAttribute("aria-activedescendant", `file-${active}`);
      list.querySelector(".entry.active")?.scrollIntoView({ block: "nearest" });
    } else {
      list.removeAttribute("aria-activedescendant");
    }
  }

  function choose(entry) {
    chosen = entry.path;
    nameInput.value = entry.name;
    render();
  }

  function open(entry) {
    if (entry.dir) { active = -1; load(entry.path); }
    else { chosen = entry.path; nameInput.value = entry.name; accept(); }
  }

  list.addEventListener("keydown", (e) => {
    if (!rows.length) return;
    const step = { ArrowDown: 1, ArrowUp: -1, PageDown: 10, PageUp: -10 }[e.key];
    if (step) {
      e.preventDefault();
      active = Math.max(0, Math.min(rows.length - 1, (active < 0 ? -1 : active) + step));
      if (!rows[active].dir) choose(rows[active]); else render();
    } else if (e.key === "Home" || e.key === "End") {
      e.preventDefault();
      active = e.key === "Home" ? 0 : rows.length - 1;
      render();
    } else if (e.key === "Enter" && active >= 0) {
      e.preventDefault();
      open(rows[active]);
    } else if (e.key === "Backspace" && listing?.parent) {
      e.preventDefault();
      active = -1;
      load(listing.parent);
    }
  });

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
  roots.addEventListener("change", () => { active = -1; load(roots.value); });
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

// --- control marks --------------------------------------------------------------

export function controlMarksDialog(form) {
  const rows = h("tbody", {});
  const addRow = (m = { name: "", elev_ft: null, note: "" }) => {
    const tr = h("tr", {},
      h("td", {}, h("input", { type: "text", value: m.name, placeholder: "BM1",
                              "aria-label": "Mark name", size: 6 })),
      h("td", {}, h("input", { type: "number", step: "0.001",
                              value: m.elev_ft == null ? "" : String(m.elev_ft),
                              "aria-label": "Held elevation (ft)" })),
      h("td", {}, h("input", { type: "text", value: m.note, placeholder: "mag nail in the curb",
                              "aria-label": "Note" })),
      h("td", {}, h("button", { type: "button", class: "icon", title: "Remove this mark",
                               onclick: () => tr.remove() }, "✕")));
    rows.append(tr);
    return tr;
  };
  form.marks.forEach((m) => addRow(m));
  if (!form.marks.length) addRow();

  const body = h("div", { class: "dlg-body form", style: { maxWidth: "560px" } },
    h("div", { class: "muted", style: { whiteSpace: "pre-wrap" } }, form.help),
    h("table", { class: "marks" },
      h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Held elev. (ft)"),
                       h("th", {}, "Note"), h("th", {}))),
      rows),
    h("button", { type: "button", onclick: () => addRow().querySelector("input").focus() },
      "Add mark"));

  return modal("", "Control marks", body,
    [{ label: "Cancel", value: null }, { label: "OK", value: "ok", primary: true }],
    { onOpen: () => rows.querySelector("input")?.focus() })
    .then((value) => value === "ok" ? [...rows.querySelectorAll("tr")].map((tr) => {
      const [name, elev, note] = tr.querySelectorAll("input");
      return { name: name.value.trim(), elev_ft: elev.value === "" ? null : Number(elev.value),
               note: note.value };
    }).filter((m) => m.name) : null);
}

// --- the datum tie --------------------------------------------------------------

export function datumDialog(form) {
  const point = h("input", { type: "text", value: String(form.point), list: "datum-points" });
  // Each option carries what the station is, so a number can be picked by
  // what was shot there rather than remembered from the phone.
  const choices = form.choices?.length ? form.choices : form.points.map((p) => ({ point: p, label: "" }));
  const points = h("datalist", { id: "datum-points" },
    choices.map((c) => h("option", { value: String(c.point), label: c.label || undefined })));
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
