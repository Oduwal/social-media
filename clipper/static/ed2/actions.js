// Editing commands (all undoable): add media / titles / presets, split, delete, duplicate,
// copy & paste, move between tracks, transitions, clip effects, captions.
import {
  S, D, CAT, W, H, deep, uid, round, clamp, commit, changed, select, tracks, mainTrack, trackOf, itemById, asset,
  layout, duration, itemStart, itemDur, itemLength, selectedItem, emit,
} from "./core.js";

const COMPAT = { overlay: ["overlay"], text: ["text", "caption"], caption: ["caption", "text"], audio: ["audio"], main: ["main"] };
export const compatible = (trackKind, itemKind) => (COMPAT[itemKind] || []).includes(trackKind);

const NAMES = { overlay: "Overlay", text: "Text", caption: "Captions", audio: "Audio" };

// A track of this kind that is free from `start` to `end` (like Studio, things that overlap
// in time get their own rows); a new one when they're all busy. Captions keep one track.
export function ensureTrack(kind, name, start = null, end = null) {
  const same = tracks().filter((x) => x.kind === kind);
  const free = (t) => start === null || kind === "caption" || !t.items.some((i) => i.start < end - 1e-3 && i.start + i.dur > start + 1e-3);
  let t = same.find(free);
  if (!t) {
    t = { id: uid(kind[0]), kind, name: name || NAMES[kind] + (same.length ? ` ${same.length + 1}` : ""), hidden: false, muted: false, items: [] };
    // visual tracks go on top of the ones there; audio at the end
    if (kind === "audio") tracks().push(t);
    else { const firstAudio = tracks().findIndex((x) => x.kind === "audio"); tracks().splice(firstAudio < 0 ? tracks().length : firstAudio, 0, t); }
  }
  return t;
}
export function addTrack(kind) {
  commit("add track");
  const t = { id: uid(kind[0]), kind, name: { overlay: "Overlay", text: "Text", caption: "Captions", audio: "Audio" }[kind], hidden: false, muted: false, items: [] };
  if (kind === "audio") tracks().push(t); else { const i = tracks().findIndex((x) => x.kind === "audio"); tracks().splice(i < 0 ? tracks().length : i, 0, t); }
  changed();
  return t;
}
export function removeTrack(id) {
  const t = tracks().find((x) => x.id === id);
  if (!t || t.kind === "main") return;
  commit("delete track");
  S.project.tracks = tracks().filter((x) => x !== t);
  if (S.selected && !itemById(S.selected.id)) select(null);
  changed();
}

const freeAt = (time) => clamp(time, 0, Math.max(0, duration() - 0.5));

// ------------------------------------------------------------------ assets
export function registerAsset(id, a) { S.project.assets[id] = a; }

export function addAsset(aid, time = S.time, trackId = null, opts = {}) {
  const a = asset(aid) || D.assets[aid];
  if (!a) return null;
  if (!asset(aid)) registerAsset(aid, a);
  commit("add media");
  let item, track;
  const target = trackId && tracks().find((t) => t.id === trackId);
  if (a.kind === "audio" || (target && target.kind === "audio")) {
    const start = freeAt(time);
    const isMusic = a.src.startsWith("music/");
    const dur = round(Math.min(isMusic ? duration() - start : a.duration || 5, duration() - start));
    track = target && target.kind === "audio" ? target : ensureTrack("audio", isMusic ? "Music" : "Sound", start, start + dur);
    item = { id: uid("a"), asset: aid, start, dur,
             in: 0, volume: isMusic ? 0.25 : 1, fade_in: isMusic ? 0.5 : 0, fade_out: isMusic ? 1 : 0, music: isMusic, duck: isMusic, kf: {} };
  } else if ((target && target.kind === "main") || opts.main) {
    track = mainTrack();
    item = { id: uid("c"), asset: aid, in: 0, out: a.kind === "video" ? round(a.duration) : 3, speed: 1, volume: 1, look: "none",
             transition: { type: "none", dur: 0.5 }, kf: {} };
    // insert after the clip under the playhead
    const lay = layout(); let idx = lay.findIndex((l) => time < l.start + l.len);
    idx = idx < 0 ? track.items.length : idx + 1;
    track.items.splice(idx, 0, item);
  } else {
    const start = freeAt(time);
    const isEmoji = a.src.startsWith("emoji/");
    const w = isEmoji ? 260 : a.src.startsWith("shape/") ? a.w : Math.min(W * 0.6, a.w || 600);
    const dur = a.kind === "video" ? Math.min(a.duration, duration() - start) : Math.min(3, duration() - start);
    track = target && target.kind === "overlay" ? target : ensureTrack("overlay", null, start, start + dur);
    item = { id: uid("o"), asset: aid, start, dur: round(dur), in: 0, x: W / 2, y: isEmoji ? H * 0.32 : H / 2, w,
             rotation: 0, opacity: 1, volume: a.kind === "video" && a.audio ? 0.6 : 0, fit: "free", scale_mode: "uniform",
             anim_in: isEmoji ? "pop" : "fade", anim_out: "fade", kf: {} };
  }
  if (track.kind !== "main") { track.items.push(item); track.items.sort((p, q) => p.start - q.start); }
  changed(); select(item.id);
  return item;
}

export function emojiAsset(code) {
  const id = "emoji:" + code;
  if (!asset(id)) registerAsset(id, { kind: "image", src: "emoji/" + code, name: "emoji " + code, w: 512, h: 512, duration: 0, audio: false });
  return id;
}
export function shapeAsset(name) {
  const s = CAT.shapes[name];
  const id = "shape:" + name;
  if (!asset(id)) registerAsset(id, { kind: "image", src: "shape/" + name, name, w: s.w, h: s.h, color: s.color, radius: s.radius, duration: 0, audio: false });
  return id;
}

// ------------------------------------------------------------------ text, titles, social packs
const fill = (s) => s.replace("{credit}", D.credit || "@creator").replace("{title}", D.title || "Wait for it");
export function addText(preset, time = S.time, kind = "text") {
  commit("add text");
  const start = freeAt(time);
  const dur = round(Math.min(3, duration() - start));
  const track = ensureTrack(kind, null, start, start + dur);
  const item = { ...deep(preset), id: uid("t"), text: fill(preset.text || "Your text"), start, dur, words: null, auto: false };
  track.items.push(item); track.items.sort((p, q) => p.start - q.start);
  changed(); select(item.id);
  return item;
}
export function addSocial(pack, time = S.time) {
  commit("add " + pack.name);
  let first = null;
  const t0 = pack.whole ? 0 : freeAt(time);
  for (const raw of pack.items) {
    const it = deep(raw);
    it.start = round(t0 + (it.start || 0));
    it.dur = round(pack.whole ? duration() : Math.min(it.dur || pack.dur || 3, duration() - it.start));
    const track = ensureTrack(it.track === "overlay" ? "overlay" : "text", null, it.start, it.start + it.dur);
    delete it.track;
    it.id = uid(track.kind[0]);
    if (it.asset?.startsWith("shape:")) it.asset = shapeAsset(it.asset.slice(6));
    else if (it.asset?.startsWith("emoji:")) it.asset = emojiAsset(it.asset.slice(6));
    if (it.text) it.text = fill(it.text);
    if (it.kf) for (const [prop, keys] of Object.entries(it.kf)) it.kf[prop] = keys.map(([t, v]) => [t === "end" ? it.dur : t, v]);
    Object.assign(it, { rotation: it.rotation || 0, opacity: it.opacity ?? 1, fit: it.fit || "free", scale_mode: it.scale_mode || "uniform",
                        anim_in: it.anim_in || "none", anim_out: it.anim_out || "none", in: 0, volume: 0, kf: it.kf || {} });
    track.items.push(it); track.items.sort((p, q) => p.start - q.start);
    first = first || it;
  }
  changed(); if (first) select(first.id);
}
export function addEmoji(code, time = S.time) { return addAsset(emojiAsset(code), time); }

// ------------------------------------------------------------------ split / delete / duplicate / clipboard
export function split(it = selectedItem(), time = S.time) {
  if (!it) { const l = layout().find((x) => time > x.start && time < x.start + x.len); it = l?.item; }
  if (!it) return;
  const track = trackOf(it.id), start = itemStart(it), dur = itemDur(it), rel = time - start;
  if (rel < 0.1 || rel > dur - 0.1) return;
  commit("split");
  const b = deep(it); b.id = uid(it.id[0]);
  const shiftKeys = (kf, cut, keepFirst) => {
    const out = {};
    for (const [p, keys] of Object.entries(kf || {})) {
      const ks = keepFirst ? keys.filter(([t]) => t <= cut) : keys.filter(([t]) => t >= cut).map(([t, v]) => [round(t - cut), v]);
      if (ks.length) out[p] = ks;
    }
    return out;
  };
  if (track.kind === "main") {
    const cutSrc = round(it.in + rel * it.speed);
    b.in = cutSrc; it.out = cutSrc; b.transition = { type: "none", dur: 0.5 };
    b.kf = shiftKeys(it.kf, rel, false); it.kf = shiftKeys(it.kf, rel, true);
    track.items.splice(track.items.indexOf(it) + 1, 0, b);
  } else {
    b.start = round(start + rel); b.dur = round(dur - rel); it.dur = round(rel);
    if ("in" in b) b.in = round((it.in || 0) + rel);
    b.kf = shiftKeys(it.kf, rel, false); it.kf = shiftKeys(it.kf, rel, true);
    if (it.words) { it.words = null; b.words = null; }
    b.anim_in = "none"; it.anim_out = "none";
    track.items.push(b); track.items.sort((p, q) => p.start - q.start);
  }
  changed(); select(b.id);
}
export function remove(it = selectedItem()) {
  if (!it) return;
  const track = trackOf(it.id);
  if (track.kind === "main" && track.items.length === 1) { alert("The video needs at least one clip on the main track."); return; }
  commit("delete");
  const start = it.start, dur = it.dur;
  track.items.splice(track.items.indexOf(it), 1);
  if (S.ripple && track.kind !== "main") track.items.forEach((x) => { if (x.start > start) x.start = round(Math.max(0, x.start - dur)); });
  select(null); changed();
}
export function duplicate(it = selectedItem()) {
  if (!it) return;
  const track = trackOf(it.id);
  commit("duplicate");
  const b = deep(it); b.id = uid(it.id[0]);
  if (track.kind === "main") track.items.splice(track.items.indexOf(it) + 1, 0, b);
  else {
    b.start = round(Math.min(it.start + it.dur, Math.max(0, duration() - b.dur)));
    const t = ensureTrack(track.kind, null, b.start, b.start + b.dur);
    t.items.push(b); t.items.sort((p, q) => p.start - q.start);
  }
  changed(); select(b.id);
}
let clip = null;
export function copy() { const it = selectedItem(); if (it) clip = { kind: trackOf(it.id).kind, item: deep(it) }; }
export function paste() {
  if (!clip) return;
  commit("paste");
  const b = deep(clip.item); b.id = uid(b.id[0]);
  if (clip.kind === "main") { const t = mainTrack(); t.items.push(b); }
  else {
    b.start = freeAt(S.time); b.dur = round(Math.min(b.dur, duration() - b.start));
    const t = ensureTrack(clip.kind, null, b.start, b.start + b.dur);
    t.items.push(b); t.items.sort((p, q) => p.start - q.start);
  }
  changed(); select(b.id);
}

// ------------------------------------------------------------------ main track
export function moveMain(it, toIndex) {
  const t = mainTrack(), from = t.items.indexOf(it);
  if (from < 0 || from === toIndex || from + 1 === toIndex) return;
  commit("reorder");
  t.items.splice(from, 1);
  t.items.splice(toIndex > from ? toIndex - 1 : toIndex, 0, it);
  changed();
}
export function setTransition(it, type, dur) {
  commit("transition");
  it.transition = { type, dur: dur ?? it.transition?.dur ?? 0.5 };
  changed();
}
export function transitionAll(type) {
  commit("transitions");
  mainTrack().items.forEach((it, i) => { if (i) it.transition = { type, dur: it.transition?.dur || 0.5 }; });
  changed();
}
export function clipEffect(fx, it = selectedItem()) {
  if (!it || trackOf(it.id).kind !== "main") { alert("Select a clip on the main video track first."); return; }
  commit("clip effect");
  const len = itemLength(it);
  it.kf = {};
  for (const [p, keys] of Object.entries(fx.kf)) it.kf[p] = keys.map(([t, v]) => [t === "end" ? round(len) : Math.min(t, len), v]);
  changed();
}

// ------------------------------------------------------------------ captions
export function setCaptions(items) {
  commit("auto captions");
  const t = ensureTrack("caption");
  t.items = items.map((x) => ({ ...x, id: uid("k") }));
  changed();
}
export function styleCaptions(style) {
  const t = tracks().find((x) => x.kind === "caption");
  if (!t || !t.items.length) { alert("Make captions first (Captions tab → Auto captions)."); return; }
  commit("caption style");
  const keep = ["id", "text", "start", "dur", "words", "auto", "kf"];
  t.items.forEach((it) => { for (const [k, v] of Object.entries(style)) if (!keep.includes(k)) it[k] = deep(v); });
  changed();
}
export function applyToAllCaptions(prop, value) {
  const t = tracks().find((x) => x.kind === "caption");
  t?.items.forEach((it) => { it[prop] = value; });
}
export { emit };
