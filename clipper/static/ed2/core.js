// Editor core: the project (same format as clipper/project.py), undo/redo, selection,
// change notifications, and the timing maths shared with the renderer (layout, keyframes,
// entrance/exit animations), so the preview moves exactly like the final video.

export const D = JSON.parse(document.getElementById("editor-data").textContent);
export const CAT = D.catalog;
export const W = CAT.canvas.w, H = CAT.canvas.h, FPS = CAT.canvas.fps;

export const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
export const round = (v, d = 3) => Math.round(v * 10 ** d) / 10 ** d;
export const fmt = (s) => {
  s = Math.max(0, s);
  const m = Math.floor(s / 60);
  return `${m}:${(s % 60).toFixed(1).padStart(4, "0")}`;
};
export const uid = (p = "i") => p + Math.random().toString(36).slice(2, 8);
export const deep = (o) => JSON.parse(JSON.stringify(o));
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
export function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(e.style, v);
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(e.dataset, v);
    else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) e.append(c.nodeType ? c : document.createTextNode(c));
  return e;
}

// ------------------------------------------------------------------ state + history
export const S = {
  project: D.project,
  selected: null,        // {track, id} | null
  multi: [],             // extra selected ids (same track)
  time: 0,
  playing: false,
  pps: 40,               // timeline pixels per second
  snap: true,
  ripple: false,
  autoKey: true,
  dirty: false,
};

const listeners = {};
export function on(evt, fn) { (listeners[evt] = listeners[evt] || []).push(fn); }
export function emit(evt, data) { (listeners[evt] || []).forEach((fn) => fn(data)); }

const undoStack = [], redoStack = [];
let pending = null;
export function commit(label = "edit") {
  // call BEFORE changing the project: remembers how it was
  undoStack.push({ label, project: deep(S.project), selected: S.selected && { ...S.selected } });
  if (undoStack.length > 120) undoStack.shift();
  redoStack.length = 0;
}
export function commitSoon(label) {
  // for slider drags: one undo step per burst of changes
  if (!pending) commit(label);
  clearTimeout(pending);
  pending = setTimeout(() => { pending = null; }, 600);
}
export function changed(what = "project") {
  S.dirty = true;
  emit("change", what);
}
export function undo() {
  if (!undoStack.length) return;
  redoStack.push({ project: deep(S.project), selected: S.selected });
  const st = undoStack.pop();
  S.project = st.project; S.selected = st.selected; S.multi = [];
  changed("undo");
}
export function redo() {
  if (!redoStack.length) return;
  undoStack.push({ project: deep(S.project), selected: S.selected });
  const st = redoStack.pop();
  S.project = st.project; S.selected = st.selected; S.multi = [];
  changed("redo");
}
export const canUndo = () => undoStack.length > 0;
export const canRedo = () => redoStack.length > 0;

// ------------------------------------------------------------------ lookups
export const tracks = () => S.project.tracks;
export const mainTrack = () => S.project.tracks.find((t) => t.kind === "main");
export const trackOf = (id) => S.project.tracks.find((t) => t.items.some((i) => i.id === id));
export const itemById = (id) => { for (const t of S.project.tracks) { const i = t.items.find((x) => x.id === id); if (i) return i; } return null; };
export const asset = (id) => S.project.assets[id];
export function selectedItem() { return S.selected ? itemById(S.selected.id) : null; }
export function select(id) {
  S.selected = id ? { track: trackOf(id)?.id, id } : null;
  S.multi = [];
  emit("select");
}
export function assetUrl(aid) { return `/api/clips/${D.id}/asset/${encodeURIComponent(aid)}`; }

// ------------------------------------------------------------------ main-track layout (= project.layout)
export const itemLength = (it) => (it.out - it.in) / it.speed;
export function layout() {
  const out = []; let cursor = 0;
  mainTrack().items.forEach((it, i) => {
    const len = itemLength(it);
    let d = 0;
    const tr = it.transition || {};
    if (i && tr.type && tr.type !== "none") d = Math.min(tr.dur || 0.5, len / 2, out[i - 1].len / 2);
    const start = cursor - d;
    out.push({ item: it, start, len, d });
    cursor = start + len;
  });
  return out;
}
export function duration() {
  const lay = layout();
  if (lay.length) return round(lay[lay.length - 1].start + lay[lay.length - 1].len);
  let end = 0;
  S.project.tracks.forEach((t) => t.items.forEach((i) => { if (t.kind !== "main") end = Math.max(end, i.start + i.dur); }));
  return end;
}
export function itemStart(it) {
  const t = trackOf(it.id);
  if (t && t.kind === "main") return layout().find((l) => l.item === it)?.start ?? 0;
  return it.start;
}
export function itemDur(it) {
  const t = trackOf(it.id);
  return t && t.kind === "main" ? itemLength(it) : it.dur;
}

// ------------------------------------------------------------------ keyframes + animations (= project.at / motion)
export function at(points, t, dflt) {
  if (!points || !points.length) return dflt;
  if (t <= points[0][0]) return points[0][1];
  for (let i = 0; i < points.length - 1; i++) {
    const [t0, v0] = points[i], [t1, v1] = points[i + 1];
    if (t <= t1) return t1 === t0 ? v0 : v0 + (v1 - v0) * (t - t0) / (t1 - t0);
  }
  return points[points.length - 1][1];
}
const PROPS = { x: "add", y: "add", scale: "mul", rotation: "add", opacity: "mul" };
const ANIM_KEY = { x: "dx", y: "dy", scale: "scale", rotation: "rotation", opacity: "opacity" };

// value of every moving property of a text / overlay at timeline time t
export function motionAt(it, t) {
  const start = it.start, dur = it.dur, kf = it.kf || {};
  const base = { x: it.x, y: it.y, scale: 1, rotation: it.rotation || 0, opacity: it.opacity ?? 1 };
  const aIn = CAT.anim_curves[it.anim_in || "none"] || {}, aOut = CAT.anim_curves[it.anim_out || "none"] || {};
  const dIn = Math.min(CAT.anim_dur[it.anim_in] || 0, dur / 2), dOut = Math.min(CAT.anim_dur[it.anim_out] || 0, dur / 2);
  const sIn = dIn ? dIn / CAT.anim_dur[it.anim_in] : 1, sOut = dOut ? dOut / CAT.anim_dur[it.anim_out] : 1;
  const out = {};
  for (const [prop, mode] of Object.entries(PROPS)) {
    const keys = (kf[prop] || []).map(([kt, v]) => [start + kt, v]);
    let v = at(keys, t, base[prop]);
    const neutral = mode === "add" ? 0 : 1;
    let m = neutral;
    const cin = aIn[ANIM_KEY[prop]], cout = aOut[ANIM_KEY[prop]];
    if (cin && t <= start + dIn + 1e-9) m = at(cin.map(([a, b]) => [start + a * sIn, b]), t, neutral);
    if (cout && t >= start + dur - dOut - 1e-9) {
      const mo = at(cout.map(([a, b]) => [start + dur - a * sOut, b]).reverse(), t, neutral);
      m = mode === "add" ? m + mo : m * mo;
    }
    out[prop] = mode === "add" ? v + m : v * m;
  }
  return out;
}

// set a keyframable property: with keyframes (or auto-key on and some exist) it keys at the playhead
export function setProp(it, prop, value, opts = {}) {
  const kf = it.kf || (it.kf = {});
  const rel = round(clamp(S.time - itemStart(it), 0, itemDur(it)));
  if (kf[prop] && kf[prop].length) {
    const keys = kf[prop];
    const i = keys.findIndex(([t]) => Math.abs(t - rel) < 0.02);
    if (i >= 0) keys[i][1] = value; else { keys.push([rel, value]); keys.sort((a, b) => a[0] - b[0]); }
  } else if (opts.key) {
    kf[prop] = [[rel, value]];
  } else if (prop in it) {
    it[prop] = value;
  }
}
export function hasKeyAt(it, prop) {
  const rel = S.time - itemStart(it);
  return (it.kf?.[prop] || []).some(([t]) => Math.abs(t - rel) < 0.02);
}
export function toggleKey(it, prop, current) {
  const kf = it.kf || (it.kf = {});
  const rel = round(clamp(S.time - itemStart(it), 0, itemDur(it)));
  const keys = kf[prop] || [];
  const i = keys.findIndex(([t]) => Math.abs(t - rel) < 0.02);
  if (i >= 0) keys.splice(i, 1); else { keys.push([rel, current]); keys.sort((a, b) => a[0] - b[0]); }
  if (keys.length) kf[prop] = keys; else delete kf[prop];
}

// when each word of a text starts (relative to the text), as the renderer does
export function wordTimes(it) {
  const words = shownText(it).split(/\s+/).filter(Boolean);
  if (it.words && it.words.length === words.length) return it.words;
  const step = it.dur / Math.max(1, words.length);
  return words.map((_, i) => [i * step, (i + 1) * step]);
}
export const shownText = (it) => (it.uppercase ? it.text.toUpperCase() : it.text);

// ------------------------------------------------------------------ fonts (sized like libass: see catalog.font_metrics)
export function installFonts() {
  const css = Object.entries(CAT.fonts).map(([name, f]) => {
    const src = f.file ? `url("/static/fonts/${f.file}")` : `local("${name}")`;
    return `@font-face { font-family: "${name} (clip)"; src: ${src}; font-weight: ${f.bold ? 700 : 400};
      ascent-override: ${f.ascent_pct}%; descent-override: ${f.descent_pct}%; line-gap-override: 0%; }`;
  }).join("\n");
  document.head.append(el("style", {}, css));
}
export const fontCss = (name) => `"${name} (clip)", "${name}", Arial, sans-serif`;
export const fontScale = (name) => (CAT.fonts[name] || { scale: 0.75 }).scale;
