// Talking to the server.
//
// Every action goes through `act`: it shows the busy overlay (and what the
// server says it is doing, for long actions), applies the snapshot that comes
// back, shows any notice, asks any confirmation, and starts any download. The
// overlay is always taken down before a dialog opens, and taken down however
// the request ends - an action that fails part-way must not leave a spinner
// behind.

import { applySnapshot } from "./store.js";
import { showNotice, showToast, confirmDialog } from "./dialogs.js";

const HEADERS = { "Content-Type": "application/json", "X-Yard-Survey": "1" };
const SHOW_AFTER_MS = 180;

let depth = 0;
let showTimer = null;
let pollTimer = null;

function beginBusy(message) {
  depth += 1;
  if (depth > 1) return;
  const box = document.getElementById("busy");
  document.getElementById("busy-text").textContent = message || "Working…";
  showTimer = setTimeout(() => {
    box.hidden = false;
    poll();
  }, SHOW_AFTER_MS);
}

function endBusy() {
  depth = Math.max(0, depth - 1);
  if (depth > 0) return;
  clearTimeout(showTimer);
  clearTimeout(pollTimer);
  document.getElementById("busy").hidden = true;
}

async function poll() {
  if (depth === 0) return;
  try {
    const r = await fetch("/api/progress");
    const p = await r.json();
    if (p.busy && p.message) document.getElementById("busy-text").textContent = p.message;
  } catch { /* the action itself will report a dead server */ }
  if (depth > 0) pollTimer = setTimeout(poll, 350);
}

export async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw await failure(r);
  return r.json();
}

export async function getBuffer(url) {
  const r = await fetch(url);
  if (!r.ok) throw await failure(r);
  return r.arrayBuffer();
}

async function failure(r) {
  let notice = null;
  try { notice = (await r.json()).error; } catch { /* not JSON */ }
  const err = new Error(notice?.text ?? `${r.status} ${r.statusText}`);
  err.notice = notice ?? { title: "Error", text: `The server answered ${r.status} ${r.statusText}.`, level: "error" };
  return err;
}

export async function refresh() {
  const snapshot = await getJSON("/api/state");
  applySnapshot(snapshot);
  return snapshot;
}

/** Run an action. Resolves to the reply, or null if it failed or was declined. */
export async function act(url, body = {}, { busy = null, quiet = false } = {}) {
  let reply = null;
  let ok = false;
  beginBusy(busy);
  try {
    const r = await fetch(url, { method: "POST", headers: HEADERS, body: JSON.stringify(body) });
    reply = await r.json().catch(() => null);
    ok = r.ok;
    if (!ok && !reply?.error) {
      reply = { error: { title: "Error", text: `The server answered ${r.status} ${r.statusText}.`, level: "error" } };
    }
  } catch (err) {
    reply = { error: { title: "No connection", level: "error",
      text: "The Yard Survey server is not answering. Is it still running?\n\n" + err } };
  } finally {
    endBusy();
  }

  if (!ok) {
    await showNotice(reply.error);
    return null;
  }
  if (reply.confirm) {
    const yes = await confirmDialog(reply.confirm.title, reply.confirm.text);
    return yes ? act(url, { ...body, confirm: true }, { busy, quiet }) : null;
  }
  if (reply.state) applySnapshot(reply.state);
  if (reply.download && !reply.download.inline) download(reply.download);
  if (reply.notice && !quiet) {
    const notice = { ...reply.notice };
    if (reply.download?.inline) {
      // Opened from a click in the dialog, so a pop-up blocker lets it through.
      notice.actions = [...(notice.actions ?? []),
        { label: "Open " + reply.download.filename, href: reply.download.url }];
    }
    const run = (chosen) => chosen?.post
      && act(chosen.post, chosen.body ?? {}, { busy: chosen.label + "…" });
    // Success is reported beside the work; warnings and errors still stop it.
    if ((notice.level ?? "info") === "info") {
      showToast(notice, run);
    } else {
      await run(await showNotice(notice));
    }
  }
  return reply;
}

function download({ url, filename }) {
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.append(a);
  a.click();
  a.remove();
}
