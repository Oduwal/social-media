// Preview: plays the project live in the browser (DOM layers on a 1080x1920 canvas),
// and lets you select, move, resize and rotate things directly on the video.
import {
  S, CAT, W, H, $, el, clamp, round, on, emit, layout, duration, mainTrack, asset, assetUrl, itemById,
  trackOf, motionAt, at, wordTimes, shownText, fontCss, fontScale, commitSoon, changed, select, setProp,
} from "./core.js";

const stage = $("#stage"), canvas = $("#canvas");
const L = { main: $("#l-main"), tfx: $("#l-tfx"), ov: $("#l-ov"), txt: $("#l-txt"), fade: $("#l-fade"), guides: $("#l-guides") };
const box = $("#selbox");
const media = new Map();     // item id -> element
const audios = new Map();    // audio item id -> <audio>
let k = 0.3;                 // canvas -> screen scale

export const view = { safe: false, grid: false, snapLines: true };

// ------------------------------------------------------------------ build DOM for the project
function mediaEl(item, a, kind) {
  let e = media.get(item.id);
  const src = a.src.startsWith("shape/") ? null : assetUrl(item.asset);
  const want = a.src.startsWith("shape/") ? "DIV" : a.kind === "video" ? "VIDEO" : "IMG";
  if (!e || e.tagName !== want || (src && e.dataset.src !== src)) {
    if (e) e.remove();
    if (want === "DIV") e = el("div", { class: "shape" });
    else if (want === "VIDEO") { e = el("video", { playsinline: true, preload: "auto" }); e.src = src; }
    else { e = el("img", { draggable: "false", alt: "" }); e.src = src; }
    e.dataset.src = src || "";
    media.set(item.id, e);
  }
  e.dataset.id = item.id;
  e.className = (want === "DIV" ? "shape " : "") + (kind === "main" ? "mm" : "ov");
  if (want === "DIV") {
    e.style.background = a.color; e.style.borderRadius = (a.radius || 0) + "px";
    e.dataset.aspect = (a.h || 1) / (a.w || 1);
  }
  return e;
}

export function build() {
  const p = S.project, keep = new Set();
  L.main.innerHTML = ""; L.ov.innerHTML = ""; L.txt.innerHTML = "";
  for (const t of p.tracks) {
    if (t.kind === "main") t.items.forEach((it) => { const a = asset(it.asset); if (!a) return; L.main.append(mediaEl(it, a, "main")); keep.add(it.id); });
    if (t.kind === "overlay") t.items.forEach((it) => { const a = asset(it.asset); if (!a) return;
      const wrap = el("div", { class: "ovwrap", dataset: { id: it.id, track: t.id } }, mediaEl(it, a, "overlay")); L.ov.append(wrap); keep.add(it.id); });
    if (t.kind === "text" || t.kind === "caption") t.items.forEach((it) => { L.txt.append(el("div", { class: "txt", dataset: { id: it.id, track: t.id } })); keep.add(it.id); });
  }
  for (const [id, e] of media) if (!keep.has(id)) { e.pause?.(); e.remove(); media.delete(id); }
  // audio items
  const keepA = new Set();
  for (const t of p.tracks) if (t.kind === "audio") for (const it of t.items) {
    let a = audios.get(it.id);
    const src = assetUrl(it.asset);
    if (!a || a.dataset.src !== src) { a?.pause(); a = new Audio(src); a.preload = "auto"; a.dataset.src = src; audios.set(it.id, a); }
    keepA.add(it.id);
  }
  for (const [id, a] of audios) if (!keepA.has(id)) { a.pause(); audios.delete(id); }
  textCache.clear();
  frame(true);
}

// ------------------------------------------------------------------ per-frame
const TRANS = {
  slideleft: (p) => ({ transform: `translateX(${(1 - p) * 100}%)` }), slideright: (p) => ({ transform: `translateX(${-(1 - p) * 100}%)` }),
  slideup: (p) => ({ transform: `translateY(${(1 - p) * 100}%)` }), slidedown: (p) => ({ transform: `translateY(${-(1 - p) * 100}%)` }),
  wipeleft: (p) => ({ clipPath: `inset(0 0 0 ${(1 - p) * 100}%)` }), wiperight: (p) => ({ clipPath: `inset(0 ${(1 - p) * 100}% 0 0)` }),
  wipeup: (p) => ({ clipPath: `inset(${(1 - p) * 100}% 0 0 0)` }), wipedown: (p) => ({ clipPath: `inset(0 0 ${(1 - p) * 100}% 0)` }),
  circleopen: (p) => ({ clipPath: `circle(${p * 75}% at 50% 50%)` }), circleclose: (p) => ({ opacity: p }),
  zoomin: (p) => ({ opacity: p, transform: `scale(${1.3 - 0.3 * p})` }),
};

function syncMedia(e, target, rate, active, volume) {
  if (e.tagName !== "VIDEO") return;
  if (!active) { if (!e.paused) e.pause(); return; }
  e.volume = clamp(volume, 0, 1);
  e.playbackRate = rate;
  if (S.playing) {
    if (Math.abs(e.currentTime - target) > 0.3) e.currentTime = target;
    if (e.paused) e.play().catch(() => {});
  } else {
    if (!e.paused) e.pause();
    if (Math.abs(e.currentTime - target) > 0.04 && !e.seeking) e.currentTime = target;
  }
}

let lastBox = "";
export function frame(force) {
  const t = S.time, p = S.project, main = mainTrack(), lay = layout(), total = duration();
  const lookCss = CAT.css_looks[p.look.filter] || "none";
  L.main.style.filter = lookCss === "none" ? "" : lookCss;
  // main track
  let fadeColor = null, fadeAlpha = 0;
  lay.forEach(({ item, start, len, d }, i) => {
    const e = media.get(item.id);
    if (!e) return;
    const a = asset(item.asset);
    const active = t >= start - 1e-6 && t < start + len;
    const preload = !active && t < start && start - t < 1.2;
    e.style.display = active ? "" : "none";
    const rel = clamp(t - start, 0, len);
    const z = at(item.kf?.zoom, rel, 1), px = at(item.kf?.panx, rel, 0), py = at(item.kf?.pany, rel, 0);
    let style = { transform: `translate(${px}px, ${py}px) scale(${z})`, opacity: "", clipPath: "", zIndex: i };
    if (active && d > 0 && rel < d) {
      const prog = rel / d, type = item.transition.type;
      if (type === "fadeblack" || type === "fadewhite") {
        fadeColor = type === "fadeblack" ? "#000" : "#fff"; fadeAlpha = 1 - Math.abs(2 * prog - 1);
        if (prog < 0.5) style.opacity = 0;
      } else if (TRANS[type]) {
        const tr = TRANS[type](prog);
        style = { ...style, ...tr, transform: (tr.transform ? tr.transform + " " : "") + style.transform };
      } else style.opacity = prog;
    }
    Object.assign(e.style, style);
    e.style.filter = item.look && item.look !== "none" ? CAT.css_looks[item.look] : "";
    if (a.kind === "video") {
      if (preload && e.paused && Math.abs(e.currentTime - item.in) > 0.1 && !e.seeking) e.currentTime = item.in;
      syncMedia(e, item.in + rel * item.speed, item.speed, active, main.muted ? 0 : item.volume);
    }
  });
  L.tfx.style.background = fadeColor || "transparent"; L.tfx.style.opacity = fadeAlpha;
  L.main.style.visibility = main.hidden ? "hidden" : "";
  // overlays
  for (const tr of p.tracks.filter((x) => x.kind === "overlay")) {
    for (const it of tr.items) {
      const wrap = L.ov.querySelector(`.ovwrap[data-id="${it.id}"]`);
      if (!wrap) continue;
      const e = media.get(it.id), a = asset(it.asset);
      const on = !tr.hidden && t >= it.start && t < it.start + it.dur;
      wrap.style.display = on ? "" : "none";
      if (a.kind === "video") syncMedia(e, it.in + (t - it.start), 1, on, tr.muted ? 0 : it.volume);
      if (!on) continue;
      const m = motionAt(it, t);
      if (it.fit === "fill") {
        Object.assign(wrap.style, { left: "0px", top: "0px", width: W + "px", height: H + "px", transform: "", opacity: m.opacity });
        e.style.width = "100%"; e.style.height = "100%"; e.style.objectFit = "cover";
      } else {
        const aspect = a.w && a.h ? a.h / a.w : Number(e.dataset.aspect) || 1;
        const w = it.w, h = it.w * aspect;
        const sc = it.scale_mode === "width" ? `scale(${m.scale}, 1)` : `scale(${m.scale})`;
        Object.assign(wrap.style, { left: m.x - w / 2 + "px", top: m.y - h / 2 + "px", width: w + "px", height: h + "px",
          transform: `rotate(${m.rotation}deg) ${sc}`, opacity: clamp(m.opacity, 0, 1) });
        e.style.width = "100%"; e.style.height = "100%"; e.style.objectFit = "fill";
      }
    }
  }
  // text
  for (const tr of p.tracks.filter((x) => x.kind === "text" || x.kind === "caption")) {
    for (const it of tr.items) {
      const e = L.txt.querySelector(`.txt[data-id="${it.id}"]`);
      if (!e) continue;
      const on = !tr.hidden && t >= it.start && t < it.start + it.dur;
      e.style.display = on ? "" : "none";
      if (on || force) drawText(e, it, t);
    }
  }
  // audio items
  for (const tr of p.tracks.filter((x) => x.kind === "audio")) {
    for (const it of tr.items) {
      const a = audios.get(it.id);
      if (!a) continue;
      const on = t >= it.start && t < it.start + it.dur && !tr.muted;
      const rel = t - it.start;
      let v = at(it.kf?.volume, rel, it.volume);
      if (it.fade_in && rel < it.fade_in) v *= rel / it.fade_in;
      if (it.fade_out && rel > it.dur - it.fade_out) v *= (it.dur - rel) / it.fade_out;
      a.volume = clamp(v, 0, 1);
      if (!on || !S.playing) { if (!a.paused) a.pause(); continue; }
      let target = it.in + rel;
      if (isFinite(a.duration) && a.duration > 0) target %= a.duration;
      if (Math.abs(a.currentTime - target) > 0.3) a.currentTime = target;
      if (a.paused) a.play().catch(() => {});
    }
  }
  // global fade
  let dark = 0;
  if (p.look.fade_in && t < p.look.fade_in) dark = 1 - t / p.look.fade_in;
  if (p.look.fade_out && t > total - p.look.fade_out) dark = Math.max(dark, (t - (total - p.look.fade_out)) / p.look.fade_out);
  L.fade.style.opacity = dark;
  drawSelection();
}

// ------------------------------------------------------------------ text (CSS that matches libass)
const textCache = new Map();
function drawText(e, it, t) {
  const m = motionAt(it, t);
  const px = it.size * fontScale(it.font);
  const key = [it.text, it.font, it.size, it.color, it.outline, it.outline_color, it.shadow, it.box, it.box_color,
    it.uppercase, it.align, it.effect, it.accent].join("|");
  if (textCache.get(it.id) !== key) {
    textCache.set(it.id, key);
    Object.assign(e.style, {
      fontFamily: fontCss(it.font), fontSize: px + "px", lineHeight: it.size + "px", color: it.color,
      fontWeight: CAT.fonts[it.font]?.bold ? 700 : 400, textAlign: it.align,
      textShadow: it.shadow && !it.box ? `${it.shadow}px ${it.shadow}px 0 rgba(0,0,0,.41)` : "",
    });
    const inner = el("span", { class: "tin" });
    if (it.box) {
      const pad = Math.max(8, Math.floor(it.size / 6));
      Object.assign(inner.style, { background: it.box_color, padding: `0 ${pad}px`, boxDecorationBreak: "clone",
        WebkitBoxDecorationBreak: "clone", WebkitTextStroke: "" });
    } else if (it.outline) {
      Object.assign(inner.style, { WebkitTextStroke: `${it.outline * 2}px ${it.outline_color}`, paintOrder: "stroke fill" });
    }
    e.replaceChildren(inner);
    e.dataset.mode = "";
  }
  const inner = e.firstChild, rel = t - it.start;
  const text = shownText(it);
  let mode = it.effect, html = null;
  if (mode === "highlight" || mode === "words") {
    const words = text.split(/\s+/).filter(Boolean), times = wordTimes(it);
    let cur = 0; times.forEach(([s], i) => { if (s <= rel + 1e-6) cur = i; });
    const shown = times.filter(([s]) => s <= rel + 1e-6).length;
    const sig = mode + (mode === "highlight" ? cur : shown);
    if (e.dataset.mode !== sig) {
      e.dataset.mode = sig;
      inner.replaceChildren(...words.flatMap((w, i) => {
        const s = el("span", {}, w);
        if (mode === "highlight" && i === cur) s.style.color = it.accent;
        if (mode === "words" && i >= shown) s.style.visibility = "hidden";
        return i ? [" ", s] : [s];
      }));
    }
  } else if (mode === "typewriter") {
    const span = Math.min(1.2, it.dur * 0.4);
    const n = Math.min(text.length, Math.floor((rel / (span / Math.max(1, text.length))) + 1e-6) + 1);
    const sig = "tw" + n;
    if (e.dataset.mode !== sig) {
      e.dataset.mode = sig;
      inner.replaceChildren(text.slice(0, n), el("span", { style: { visibility: "hidden" } }, text.slice(n)));
    }
  } else if (e.dataset.mode !== "plain") { e.dataset.mode = "plain"; inner.textContent = text; }
  const anchor = it.align === "left" ? "0%" : it.align === "right" ? "-100%" : "-50%";
  Object.assign(e.style, { left: m.x + "px", top: m.y + "px", opacity: clamp(m.opacity, 0, 1),
    transform: `translate(${anchor}, -50%) rotate(${m.rotation}deg) scale(${m.scale})`,
    transformOrigin: it.align === "left" ? "0% 50%" : it.align === "right" ? "100% 50%" : "50% 50%" });
  void html;
}

// ------------------------------------------------------------------ fit, selection box, interaction
export function fit() {
  const area = $("#stagearea");
  const h = area.clientHeight - 16, w = area.clientWidth - 16;
  k = Math.min(h / H, w / W);
  stage.style.width = W * k + "px"; stage.style.height = H * k + "px";
  canvas.style.transform = `scale(${k})`;
  drawGuides();
  drawSelection();
}

function drawGuides() {
  L.guides.innerHTML = "";
  if (view.safe) {
    // where TikTok / Reels put their buttons and captions
    [[0, 0, W, 150], [0, H - 420, W, 420], [W - 150, 700, 150, 800]].forEach(([x, y, w, h]) =>
      L.guides.append(el("div", { class: "safe", style: { left: x + "px", top: y + "px", width: w + "px", height: h + "px" } })));
  }
  if (view.grid) [1, 2].forEach((i) => {
    L.guides.append(el("div", { class: "gridv", style: { left: (W * i) / 3 + "px" } }));
    L.guides.append(el("div", { class: "gridh", style: { top: (H * i) / 3 + "px" } }));
  });
}
export function redrawGuides() { drawGuides(); }

function targetEl() {
  if (!S.selected) return null;
  return L.ov.querySelector(`.ovwrap[data-id="${S.selected.id}"]`) || L.txt.querySelector(`.txt[data-id="${S.selected.id}"]`);
}
function drawSelection() {
  const e = targetEl();
  if (!e || e.style.display === "none") { box.hidden = true; return; }
  const r = e.getBoundingClientRect(), s = stage.getBoundingClientRect();
  const sig = [r.left - s.left, r.top - s.top, r.width, r.height].map(Math.round).join(",");
  box.hidden = false;
  if (sig === lastBox) return;
  lastBox = sig;
  Object.assign(box.style, { left: r.left - s.left + "px", top: r.top - s.top + "px", width: r.width + "px", height: r.height + "px" });
}

let drag = null;
stage.addEventListener("pointerdown", (ev) => {
  const handle = ev.target.closest("[data-h]");
  const hit = handle ? targetEl() : ev.target.closest(".ovwrap, .txt");
  if (!hit) { if (!ev.target.closest("#selbox")) select(null); return; }
  const id = hit.dataset.id, it = itemById(id);
  if (!it) return;
  if (!handle && S.selected?.id !== id) select(id);
  const m = motionAt(it, S.time);
  drag = { id, kind: handle ? handle.dataset.h : "move", x0: ev.clientX, y0: ev.clientY, m, size: it.size, w: it.w,
           rect: hit.getBoundingClientRect() };
  stage.setPointerCapture(ev.pointerId);
  ev.preventDefault();
});
stage.addEventListener("pointermove", (ev) => {
  if (!drag) return;
  const it = itemById(drag.id); if (!it) return;
  const dx = (ev.clientX - drag.x0) / k, dy = (ev.clientY - drag.y0) / k;
  commitSoon("move on video");
  if (drag.kind === "move") {
    let x = drag.m.x + dx, y = drag.m.y + dy;
    const guides = [];
    if (S.snap && view.snapLines) {
      if (Math.abs(x - W / 2) < 14) { x = W / 2; guides.push("v"); }
      if (Math.abs(y - H / 2) < 14) { y = H / 2; guides.push("h"); }
    }
    stage.dataset.snap = guides.join("");
    setProp(it, "x", round(x, 1)); setProp(it, "y", round(y, 1));
  } else if (drag.kind === "scale") {
    const c = { x: drag.rect.left + drag.rect.width / 2, y: drag.rect.top + drag.rect.height / 2 };
    const d0 = Math.hypot(drag.x0 - c.x, drag.y0 - c.y), d1 = Math.hypot(ev.clientX - c.x, ev.clientY - c.y);
    const f = clamp(d1 / Math.max(1, d0), 0.05, 20);
    if (it.text !== undefined) it.size = Math.round(clamp(drag.size * f, 12, 400));
    else it.w = round(clamp(drag.w * f, 8, 4 * W), 1);
  } else if (drag.kind === "rotate") {
    const c = { x: drag.rect.left + drag.rect.width / 2, y: drag.rect.top + drag.rect.height / 2 };
    let ang = (Math.atan2(ev.clientY - c.y, ev.clientX - c.x) * 180) / Math.PI + 90;
    if (S.snap) for (const s of [-180, -90, 0, 90, 180]) if (Math.abs(ang - s) < 4) ang = s;
    setProp(it, "rotation", round(ang, 1));
  }
  changed("canvas");
});
const endDrag = () => { drag = null; delete stage.dataset.snap; };
stage.addEventListener("pointerup", endDrag);
stage.addEventListener("pointercancel", endDrag);
stage.addEventListener("dblclick", (ev) => { if (ev.target.closest(".txt")) emit("edit-text"); });

// moving / restyling things only needs a redraw; adding or removing them rebuilds the layers
on("change", (what) => { if (what === "canvas" || what === "props") { textCache.clear(); frame(true); } else build(); });
on("select", () => { lastBox = ""; drawSelection(); });
window.addEventListener("resize", fit);

// ------------------------------------------------------------------ the clock
let lastTs = 0, lastTick = -1;
export function play(want = !S.playing) {
  if (want && S.time >= duration() - 0.05) S.time = 0;
  S.playing = want;
  lastTs = performance.now();
  emit("playstate");
  if (!want) frame();
}
function loop(ts) {
  if (S.playing) {
    S.time += (ts - lastTs) / 1000;
    if (S.time >= duration()) { S.time = duration(); play(false); }
  }
  lastTs = ts;
  frame();
  if (S.time !== lastTick) { lastTick = S.time; emit("tick"); }  // only when the time moved
  requestAnimationFrame(loop);
}
export function seek(t) { S.time = clamp(t, 0, duration()); frame(); }
export function start() { build(); fit(); requestAnimationFrame(loop); }
export { trackOf };
