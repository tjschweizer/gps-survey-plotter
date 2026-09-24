// Small DOM helpers. No framework: the page is a handful of panels, and each
// one rebuilds itself from the latest snapshot.

export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props ?? {})) {
    if (value == null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") {
      el.addEventListener(key.slice(2), value);
    } else if (key in el && typeof value !== "string") el[key] = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export function $(selector, root = document) {
  return root.querySelector(selector);
}

export function isTyping(target) {
  if (!target) return false;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA" ||
    target.isContentEditable || !!target.closest?.(".tabulator-editing");
}

export function fmtFt(m) {
  return (m / 0.3048).toFixed(2);
}

export function debounce(fn, ms) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

// Per-browser conveniences only (dock widths). Storage can be unavailable in
// a private window; nothing depends on it.
export function remember(key, value) {
  try { localStorage.setItem("yardsurvey." + key, JSON.stringify(value)); } catch { /* ignore */ }
}
export function recall(key, fallback) {
  try {
    const raw = localStorage.getItem("yardsurvey." + key);
    return raw == null ? fallback : JSON.parse(raw);
  } catch { return fallback; }
}
