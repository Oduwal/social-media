// Editor start-up: wires the toolbar, keyboard shortcuts, autosave and saving.
import { S, D, $, $$, el, on, emit, undo, redo, canUndo, canRedo, changed, select, installFonts, fmt, duration, commit } from "./core.js";
import * as P from "./preview.js";
import * as T from "./timeline.js";
import * as A from "./actions.js";
import * as UI from "./panels.js";

installFonts();
UI.tabs($("#left")); UI.tabs($("#right"));

// ------------------------------------------------------------------ toolbar
const btn = (id, fn) => $(id)?.addEventListener("click", (e) => { e.preventDefault(); fn(); });
btn("#b-undo", undo); btn("#b-redo", redo);
btn("#b-play", () => P.play());
btn("#b-start", () => P.seek(0)); btn("#b-end", () => P.seek(duration()));
btn("#b-split", () => A.split()); btn("#b-del", () => A.remove()); btn("#b-dup", () => A.duplicate());
btn("#b-beat", () => T.addMarker());
btn("#b-fit", () => T.fitZoom());
btn("#b-text", () => A.addText(D.catalog.titles[0].item));
btn("#b-addtrack", () => {
  const kind = prompt("Add which track? overlay / text / audio", "overlay");
  if (["overlay", "text", "audio"].includes(kind)) A.addTrack(kind);
});
const toggle = (id, key, obj = S, after) => {
  const b = $(id);
  const sync = () => b.classList.toggle("on", !!obj[key]);
  b.addEventListener("click", () => { obj[key] = !obj[key]; sync(); after?.(); });
  sync();
};
toggle("#b-snap", "snap"); toggle("#b-ripple", "ripple");
toggle("#b-safe", "safe", P.view, P.redrawGuides); toggle("#b-grid", "grid", P.view, P.redrawGuides);
$("#tl-zoom").addEventListener("input", (e) => T.zoom(Math.exp(Number(e.target.value))));
btn("#b-theme", () => {
  const dark = document.documentElement.dataset.theme !== "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  try { localStorage.setItem("ed2-theme", dark ? "dark" : "light"); } catch { /* private window */ }
});
try { const th = localStorage.getItem("ed2-theme"); if (th) document.documentElement.dataset.theme = th; } catch { /* ignore */ }

on("playstate", () => { $("#b-play").textContent = S.playing ? "❚❚" : "▶"; });
on("tick", () => { $("#clock").textContent = `${fmt(S.time)} / ${fmt(duration())}`; });
on("change", () => {
  $("#b-undo").disabled = !canUndo(); $("#b-redo").disabled = !canRedo();
  $("#clock").textContent = `${fmt(S.time)} / ${fmt(duration())}`;
  scheduleDraft();
});
on("panel", (name) => { if (name === "transition") UI.showTab("design"); });
on("edit-text", () => { UI.showTab("design"); setTimeout(() => $("#tx-edit")?.focus(), 50); });

// ------------------------------------------------------------------ keyboard
document.addEventListener("keydown", (e) => {
  const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName) && document.activeElement.type !== "range";
  const mod = e.ctrlKey || e.metaKey;
  if (mod && e.key.toLowerCase() === "z" && !typing) { e.preventDefault(); e.shiftKey ? redo() : undo(); return; }
  if (mod && e.key.toLowerCase() === "y" && !typing) { e.preventDefault(); redo(); return; }
  if (mod && e.key.toLowerCase() === "s") { e.preventDefault(); save(); return; }
  if (typing) return;
  if (mod && e.key.toLowerCase() === "d") { e.preventDefault(); A.duplicate(); return; }
  if (mod && e.key.toLowerCase() === "c") { A.copy(); return; }
  if (mod && e.key.toLowerCase() === "v") { A.paste(); return; }
  const step = e.shiftKey ? 1 : 1 / 30;
  switch (e.key) {
    case " ": e.preventDefault(); P.play(); break;
    case "s": case "S": A.split(); break;
    case "b": case "B": T.addMarker(); break;
    case "Delete": case "Backspace": A.remove(); break;
    case "ArrowLeft": e.preventDefault(); P.seek(S.time - step); break;
    case "ArrowRight": e.preventDefault(); P.seek(S.time + step); break;
    case "Home": P.seek(0); break;
    case "End": P.seek(duration()); break;
    case "Escape": select(null); break;
    case "+": case "=": T.zoom(S.pps * 1.25); break;
    case "-": T.zoom(S.pps / 1.25); break;
    case "?": $("#help").hidden = !$("#help").hidden; break;
    default: break;
  }
});
btn("#b-help", () => { $("#help").hidden = !$("#help").hidden; });
btn("#help-close", () => { $("#help").hidden = true; });

// ------------------------------------------------------------------ drafts + saving
let draftTimer = null, saving = false;
const status = (txt) => { $("#savestate").textContent = txt; };
function scheduleDraft() {
  clearTimeout(draftTimer);
  status("Unsaved changes");
  draftTimer = setTimeout(saveDraft, 2500);
}
async function saveDraft() {
  try {
    const r = await fetch(`/api/clips/${D.id}/draft`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(S.project) });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || r.status);
    status("Draft saved " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
  } catch (err) { status("Couldn't save the draft: " + err.message); }
}
async function save() {
  if (saving) return;
  if (!D.editable) { saveDraft(); alert(`This clip is ${D.state}: your work is kept as a draft, but the final video can't change now.`); return; }
  saving = true;
  const b = $("#b-save"); b.disabled = true; b.textContent = "Saving…";
  try {
    const r = await fetch(`/api/clips/${D.id}/project`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(S.project) });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || r.status);
    S.dirty = false; clearTimeout(draftTimer);
    location.href = body.redirect;
  } catch (err) {
    alert("Couldn't save: " + err.message);
    b.disabled = false; b.textContent = "Save & render";
  }
  saving = false;
}
btn("#b-save", save);
on("save", save);
window.addEventListener("beforeunload", (e) => {
  if (S.dirty) { saveDraft(); e.preventDefault(); e.returnValue = ""; }
});
if (D.draft_saved_at) {
  const bar = el("div", { class: "banner info small", id: "draftbar" }, "You have unsaved changes from before (kept as a draft). ",
    el("button", { class: "btn small", onclick: async () => {
      if (!confirm("Throw away the draft and go back to the clip's saved edit?")) return;
      await fetch(`/api/clips/${D.id}/discard_draft`, { method: "POST" });
      location.reload();
    } }, "Discard draft"));
  $("#topbar").after(bar);
}

// ------------------------------------------------------------------ go
UI.renderDesign(); UI.renderLayers(); UI.renderExport(); UI.renderMedia(); UI.renderCatalog(); UI.renderCaptions(); UI.renderAI();
P.start(); T.fitZoom();
$("#b-undo").disabled = true; $("#b-redo").disabled = true;
window.ED2 = { S, A, P, T, commit, changed, select, emit };   // for debugging and tests
