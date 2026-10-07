// Timeline: tracks of items, drag to move / trim / reorder, snapping, zoom, film strips,
// waveforms, beat markers, transition badges and keyframe diamonds.
import {
  S, D, CAT, $, el, clamp, round, fmt, on, emit, commit, commitSoon, changed, select, tracks, mainTrack, trackOf,
  itemById, asset, layout, duration, itemStart, itemDur, assetUrl, uid,
} from "./core.js";
import { seek } from "./preview.js";
import { moveMain, compatible, addAsset, removeTrack } from "./actions.js";

const heads = $("#tl-heads"), lanes = $("#tl-lanes"), ruler = $("#tl-ruler"), scroller = $("#tl-scroll");
const playhead = $("#tl-playhead"), markersRow = $("#tl-markers");
const ICON = { main: "🎬", overlay: "🖼️", text: "T", caption: "💬", audio: "♪" };
const strips = new Map(), peaks = new Map();

const order = () => {
  const ts = tracks();
  return [...ts.filter((t) => !["main", "audio"].includes(t.kind)).reverse(), ...ts.filter((t) => t.kind === "main"),
          ...ts.filter((t) => t.kind === "audio")];
};
const X = (t) => t * S.pps;
const T = (x) => x / S.pps;

// ------------------------------------------------------------------ data loaders
function strip(aid) {
  if (!strips.has(aid)) {
    const info = { url: `/api/clips/${D.id}/strip/${encodeURIComponent(aid)}`, every: 1, count: 0, ready: false };
    strips.set(aid, info);
    fetch(info.url).then((r) => { info.every = Number(r.headers.get("X-Every")) || 1; info.count = Number(r.headers.get("X-Count")) || 0; info.ready = r.ok; draw(); });
  }
  return strips.get(aid);
}
function peak(aid) {
  if (!peaks.has(aid)) {
    peaks.set(aid, null);
    fetch(`/api/clips/${D.id}/peaks/${encodeURIComponent(aid)}`).then((r) => (r.ok ? r.json() : null)).then((p) => { peaks.set(aid, p); draw(); });
  }
  return peaks.get(aid);
}

// ------------------------------------------------------------------ drawing
function waveform(canvas, aid, inPt, dur, speed = 1) {
  const p = peak(aid);
  if (!p) return;
  const w = canvas.width = Math.max(1, Math.round(canvas.clientWidth)), h = canvas.height = canvas.clientHeight || 28;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "rgba(255,255,255,.55)";
  for (let x = 0; x < w; x++) {
    const t = inPt + (x / w) * dur * speed;
    const i = Math.floor(t * p.per_second);
    const v = p.peaks.length ? p.peaks[i % p.peaks.length] || 0 : 0;
    const bh = Math.max(1, v * h * 0.9);
    ctx.fillRect(x, (h - bh) / 2, 1, bh);
  }
}

function itemLabel(t, it) {
  const a = asset(it.asset);
  if (t.kind === "text" || t.kind === "caption") return it.text;
  if (a?.src.startsWith("emoji/")) return "emoji";
  return a?.name || it.asset;
}

export function draw() {
  const total = Math.max(duration(), 1);
  const width = X(total) + 300;
  lanes.style.width = ruler.style.width = markersRow.style.width = width + "px";
  heads.innerHTML = ""; lanes.innerHTML = "";
  const lay = layout();
  for (const t of order()) {
    const head = el("div", { class: `thead k-${t.kind}${t.hidden ? " off" : ""}`, dataset: { track: t.id } },
      el("span", { class: "ticon" }, ICON[t.kind]),
      el("span", { class: "tname", title: t.name }, t.name),
      t.kind !== "audio" ? el("button", { class: "ib", title: t.hidden ? "Show track" : "Hide track", onclick: () => { commit("hide"); t.hidden = !t.hidden; changed(); } }, t.hidden ? "🙈" : "👁") : null,
      ["main", "overlay", "audio"].includes(t.kind) ? el("button", { class: "ib", title: t.muted ? "Unmute" : "Mute", onclick: () => { commit("mute"); t.muted = !t.muted; changed(); } }, t.muted ? "🔇" : "🔊") : null,
      t.kind !== "main" ? el("button", { class: "ib", title: "Delete track", onclick: () => { if (!t.items.length || confirm(`Delete the ${t.name} track and its ${t.items.length} item(s)?`)) removeTrack(t.id); } }, "✕") : null);
    heads.append(head);
    const lane = el("div", { class: `lane k-${t.kind}`, dataset: { track: t.id } });
    if (t.kind === "main") {
      lay.forEach(({ item, start, len, d }, i) => {
        const a = asset(item.asset) || {};
        const b = block(t, item, start, len);
        if (a.kind === "video") {
          const s = strip(item.asset);
          if (s.ready) {
            const fw = 54 * (X(s.every) / 54);  // frame width at this zoom (one frame per `every` seconds)
            Object.assign(b.style, { backgroundImage: `url(${s.url})`, backgroundSize: `${fw * s.count}px 100%`,
                                     backgroundPosition: `${-X(item.in / item.speed)}px 0` });
            b.classList.add("filmed");
          }
          if (a.audio) { const c = el("canvas", { class: "wave" }); b.append(c); requestAnimationFrame(() => waveform(c, item.asset, item.in, len, item.speed)); }
        } else if (a.kind === "image") {
          Object.assign(b.style, { backgroundImage: `url(${assetUrl(item.asset)})`, backgroundSize: "auto 100%", backgroundRepeat: "repeat-x" });
        }
        if (item.speed !== 1) b.append(el("span", { class: "badge" }, `${item.speed}×`));
        lane.append(b);
        if (i) {
          const tr = item.transition?.type || "none";
          lane.append(el("button", { class: "trans" + (tr !== "none" ? " on" : ""), title: tr === "none" ? "Add a transition" : `Transition: ${CAT.transitions[tr]}`,
            style: { left: X(start + d / 2) - 11 + "px" }, onclick: (e) => { e.stopPropagation(); select(item.id); emit("panel", "transition"); } }, tr === "none" ? "+" : "⇄"));
        }
      });
    } else {
      for (const it of t.items) {
        const b = block(t, it, it.start, it.dur);
        const a = asset(it.asset);
        if (t.kind === "audio") { const c = el("canvas", { class: "wave" }); b.append(c); requestAnimationFrame(() => waveform(c, it.asset, it.in, it.dur)); }
        if (t.kind === "overlay" && a) {
          if (a.kind === "image" && !a.src.startsWith("shape/")) b.prepend(el("img", { class: "thumb", src: assetUrl(it.asset), alt: "" }));
          if (a.src.startsWith("shape/")) b.prepend(el("span", { class: "swatch", style: { background: a.color } }));
        }
        lane.append(b);
      }
    }
    lanes.append(lane);
  }
  drawRuler(total); drawMarkers(); tick();
}

function block(t, it, start, dur) {
  const sel = S.selected?.id === it.id;
  const b = el("div", { class: `blk k-${t.kind}${sel ? " sel" : ""}`, dataset: { id: it.id, track: t.id },
    style: { left: X(start) + "px", width: Math.max(4, X(dur)) + "px" } },
    el("span", { class: "h l", dataset: { edge: "l" } }), el("span", { class: "lbl" }, itemLabel(t, it)), el("span", { class: "h r", dataset: { edge: "r" } }));
  if (sel) keyDiamonds(b, it);
  return b;
}
function keyDiamonds(b, it) {
  const times = new Set();
  Object.values(it.kf || {}).forEach((keys) => keys.forEach(([t]) => times.add(t)));
  times.forEach((t) => b.append(el("span", { class: "kd", title: `Keyframe at ${fmt(itemStart(it) + t)}`, style: { left: X(t) - 5 + "px" },
    onpointerdown: (e) => { e.stopPropagation(); seek(itemStart(it) + t); } })));
}

function drawRuler(total) {
  ruler.innerHTML = "";
  const step = S.pps > 120 ? 0.5 : S.pps > 50 ? 1 : S.pps > 20 ? 2 : S.pps > 8 ? 5 : 10;
  for (let s = 0; s <= total + 5; s += step) {
    ruler.append(el("span", { class: "tk" + (Number.isInteger(s / (step * 2)) ? " big" : ""), style: { left: X(s) + "px" } },
      Number.isInteger(s / (step * 2)) ? fmt(s).replace(/\.0$/, "") : ""));
  }
  ruler.append(el("span", { class: "endmark", style: { left: X(total) + "px" }, title: "End of the video" }));
}
function drawMarkers() {
  markersRow.innerHTML = "";
  (S.project.markers || []).forEach((m) => markersRow.append(el("span", { class: "mk", title: `Beat at ${fmt(m)} (Alt-click to remove)`,
    style: { left: X(m) - 5 + "px" }, onpointerdown: (e) => {
      e.stopPropagation();
      if (e.altKey) { commit("remove beat"); S.project.markers = S.project.markers.filter((x) => x !== m); changed("markers"); } else seek(m);
    } })));
}
export function addMarker() {
  commit("beat");
  const m = round(S.time);
  S.project.markers = [...new Set([...(S.project.markers || []), m])].sort((a, b) => a - b);
  changed("markers");
}

export function tick() {
  playhead.style.left = X(S.time) + "px";
  if (S.playing) {   // keep the playhead in view
    const x = X(S.time) - scroller.scrollLeft;
    if (x > scroller.clientWidth - 60 || x < 0) scroller.scrollLeft = X(S.time) - 80;
  }
}

// ------------------------------------------------------------------ zoom
export function zoom(pps, anchorTime = S.time) {
  const before = X(anchorTime) - scroller.scrollLeft;
  S.pps = clamp(pps, 2, 400);
  draw();
  scroller.scrollLeft = X(anchorTime) - before;
  $("#tl-zoom").value = Math.log(S.pps);
}
export function fitZoom() { zoom((scroller.clientWidth - 40) / Math.max(1, duration())); scroller.scrollLeft = 0; }
scroller.addEventListener("wheel", (e) => {
  if (!e.ctrlKey && !e.metaKey) return;
  e.preventDefault();
  zoom(S.pps * (e.deltaY < 0 ? 1.15 : 1 / 1.15), T(e.clientX - lanes.getBoundingClientRect().left));
}, { passive: false });

// ------------------------------------------------------------------ snapping
function snapPoints(exceptId) {
  const pts = [0, duration(), S.time, ...(S.project.markers || [])];
  for (const t of tracks()) for (const it of t.items) if (it.id !== exceptId) { const s = itemStart(it); pts.push(s, s + itemDur(it)); }
  return pts;
}
function snap(t, exceptId, also = []) {
  if (!S.snap) return { t, hit: null };
  const tol = 8 / S.pps;
  let best = null;
  for (const p of snapPoints(exceptId)) for (const off of [0, ...also]) {
    const d = Math.abs(t + off - p);
    if (d < tol && (!best || d < best.d)) best = { d, t: p - off, hit: p };
  }
  return best ? { t: best.t, hit: best.hit } : { t, hit: null };
}
const snapLine = $("#tl-snap");
function showSnap(at) { snapLine.hidden = at === null; if (at !== null) snapLine.style.left = X(at) + "px"; }

// ------------------------------------------------------------------ dragging
let drag = null;
const laneAt = (y) => [...lanes.children].find((l) => { const r = l.getBoundingClientRect(); return y >= r.top && y < r.bottom; });

lanes.addEventListener("pointerdown", (e) => {
  if (e.target.closest(".trans, .kd")) return;   // badges and keyframe diamonds handle their own clicks
  const b = e.target.closest(".blk");
  if (!b) { if (e.target.closest(".lane")) { seekAt(e); select(null); drag = { kind: "seek" }; lanes.setPointerCapture(e.pointerId); } return; }
  const it = itemById(b.dataset.id), t = trackOf(b.dataset.id);
  if (!it) return;
  if (S.selected?.id !== it.id) select(it.id);
  const edge = e.target.dataset.edge;
  drag = { kind: edge ? "trim" : "move", edge, it, track: t, x0: e.clientX, y0: e.clientY, orig: JSON.parse(JSON.stringify(it)),
           start: itemStart(it), dur: itemDur(it), moved: false, block: b };
  lanes.setPointerCapture(e.pointerId);
  e.preventDefault();
});
lanes.addEventListener("pointermove", (e) => {
  if (!drag) return;
  if (drag.kind === "seek") { seekAt(e); return; }
  const dt = T(e.clientX - drag.x0);
  if (!drag.moved && Math.abs(e.clientX - drag.x0) < 3 && Math.abs(e.clientY - drag.y0) < 3) return;
  drag.moved = true;
  const { it, track, orig } = drag;
  if (track.kind === "main") {
    if (drag.kind === "move") {   // show where the clip would go
      drag.block.style.transform = `translateX(${e.clientX - drag.x0}px)`;
      drag.block.classList.add("dragging");
      const x = T(e.clientX - lanes.getBoundingClientRect().left + scroller.scrollLeft * 0);
      drag.dropIndex = layout().filter((l) => l.start + l.len / 2 < x).length;
      return;
    }
    commitSoon("trim");
    const a = asset(it.asset) || {};
    const limit = a.kind === "video" ? a.duration : 3600;
    if (drag.edge === "l") {
      const s = snap(drag.start + dt, it.id);
      it.in = round(clamp(orig.in + (s.t - drag.start) * orig.speed, 0, orig.out - 0.1));
      showSnap(s.hit);
    } else {
      const s = snap(drag.start + drag.dur + dt, it.id);
      it.out = round(clamp(orig.out + (s.t - drag.start - drag.dur) * orig.speed, orig.in + 0.1, limit));
      showSnap(s.hit);
    }
    changed("trim");
    return;
  }
  commitSoon(drag.kind);
  if (drag.kind === "move") {
    const s = snap(drag.start + dt, it.id, [drag.dur]);
    it.start = round(clamp(s.t, 0, Math.max(0, duration() - 0.1)));
    showSnap(s.hit);
    const lane = laneAt(e.clientY);   // move to another track of the same kind
    const to = lane && tracks().find((x) => x.id === lane.dataset.track);
    if (to && to !== trackOf(it.id) && compatible(to.kind, track.kind === "caption" ? "text" : track.kind)) {
      const from = trackOf(it.id);
      from.items.splice(from.items.indexOf(it), 1); to.items.push(it);
    }
  } else if (drag.edge === "l") {
    const s = snap(drag.start + dt, it.id);
    const ns = clamp(s.t, 0, drag.start + drag.dur - 0.1);
    const d = ns - drag.start;
    it.start = round(ns); it.dur = round(drag.dur - d);
    if ("in" in it && (asset(it.asset)?.kind === "video" || asset(it.asset)?.kind === "audio")) it.in = round(Math.max(0, orig.in + d));
    it.kf = shiftKf(orig.kf, -d);
    if (orig.words) it.words = orig.words.map(([a, b]) => [round(a - d), round(b - d)]).every(([a]) => a >= 0) ? orig.words.map(([a, b]) => [round(a - d), round(b - d)]) : null;
    showSnap(s.hit);
  } else {
    const s = snap(drag.start + drag.dur + dt, it.id);
    const a = asset(it.asset) || {};
    let max = duration() - drag.start;
    if (a.kind === "video" && track.kind === "overlay") max = Math.min(max, a.duration - (it.in || 0));
    it.dur = round(clamp(s.t - drag.start, 0.1, max));
    showSnap(s.hit);
  }
  changed("trim");
});
function shiftKf(kf, by) {
  const out = {};
  for (const [p, keys] of Object.entries(kf || {})) { const ks = keys.map(([t, v]) => [round(t + by), v]).filter(([t]) => t >= 0); if (ks.length) out[p] = ks; }
  return out;
}
const endDrag = () => {
  if (drag?.kind === "move" && drag.track.kind === "main" && drag.moved && drag.dropIndex !== undefined) moveMain(drag.it, drag.dropIndex);
  else if (drag?.moved) { tracks().forEach((t) => t.kind !== "main" && t.items.sort((p, q) => p.start - q.start)); changed(); }
  drag = null; showSnap(null);
};
lanes.addEventListener("pointerup", endDrag);
lanes.addEventListener("pointercancel", endDrag);

function seekAt(e) { seek(T(e.clientX - lanes.getBoundingClientRect().left)); }
let rulerDrag = false;
[ruler, markersRow].forEach((r) => r.addEventListener("pointerdown", (e) => { rulerDrag = true; r.setPointerCapture(e.pointerId); seekAt(e); }));
[ruler, markersRow].forEach((r) => r.addEventListener("pointermove", (e) => rulerDrag && seekAt(e)));
[ruler, markersRow].forEach((r) => r.addEventListener("pointerup", () => { rulerDrag = false; }));
scroller.addEventListener("scroll", () => { heads.scrollTop = scroller.scrollTop; });

// drop media from the left panel onto a track
lanes.addEventListener("dragover", (e) => { if (e.dataTransfer.types.includes("application/x-clipper-asset")) e.preventDefault(); });
lanes.addEventListener("drop", (e) => {
  const aid = e.dataTransfer.getData("application/x-clipper-asset");
  if (!aid) return;
  e.preventDefault();
  const lane = e.target.closest(".lane");
  addAsset(aid, T(e.clientX - lanes.getBoundingClientRect().left), lane?.dataset.track);
});

on("change", (what) => { if (what !== "canvas" && what !== "props") draw(); else if (S.selected) draw(); });
on("select", draw);
on("tick", tick);
export { uid };
