// Side panels. Left: Media, Catalog, Captions, AI. Right: Design (inspector with keyframes),
// Layers, Export.
import {
  S, D, CAT, W, H, $, $$, el, clamp, round, fmt, on, emit, commit, commitSoon, changed, select, tracks, trackOf,
  itemById, asset, assetUrl, selectedItem, itemStart, itemDur, itemLength, motionAt, setProp, hasKeyAt, toggleKey,
  duration, mainTrack, deep, fontCss,
} from "./core.js";
import * as A from "./actions.js";
import { seek } from "./preview.js";

// ------------------------------------------------------------------ tabs
export function tabs(root) {
  const btns = $$(".ptab", root), pages = $$(".ppage", root);
  btns.forEach((b) => b.addEventListener("click", () => {
    btns.forEach((x) => x.classList.toggle("on", x === b));
    pages.forEach((p) => { p.hidden = p.dataset.page !== b.dataset.page; });
  }));
}
export const showTab = (name) => $(`.ptab[data-page="${name}"]`)?.click();

// ------------------------------------------------------------------ small form helpers
const row = (label, ...ctl) => el("label", { class: "frow" }, el("span", { class: "flab" }, label), ...ctl);
function num(value, opts, onInput) {
  const range = el("input", { type: "range", min: opts.min, max: opts.max, step: opts.step || 1, value });
  const box = el("input", { type: "number", min: opts.min, max: opts.max, step: opts.step || 1, value: round(value, 2), class: "nbox" });
  const fire = (v) => { v = clamp(Number(v), opts.min, opts.max); if (!isFinite(v)) return; range.value = v; box.value = round(v, 2); onInput(v); };
  range.addEventListener("input", () => fire(range.value));
  box.addEventListener("change", () => fire(box.value));
  return el("span", { class: "numctl" }, range, box);
}
function select_(value, options, onChange) {
  const s = el("select", {});
  for (const [k, label] of Object.entries(options)) s.add(new Option(label, k));
  s.value = value;
  s.addEventListener("change", () => onChange(s.value));
  return s;
}
function color(value, onInput) { const c = el("input", { type: "color", value: value.toLowerCase() }); c.addEventListener("input", () => onInput(c.value.toUpperCase())); return c; }
function check(value, label, onChange) { const c = el("input", { type: "checkbox" }); c.checked = !!value; c.addEventListener("change", () => onChange(c.checked)); return el("label", { class: "chk" }, c, label); }
const section = (title, ...kids) => el("section", { class: "isec" }, el("h4", {}, title), ...kids);

// a keyframable number: value + ◆ (key at playhead) + ‹ › (jump between keys)
function kfNum(it, prop, label, opts, current) {
  const keyed = (it.kf?.[prop] || []).length > 0;
  const btn = el("button", { class: "kfb" + (hasKeyAt(it, prop) ? " on" : keyed ? " has" : ""), title: hasKeyAt(it, prop) ? "Remove the keyframe here" : "Add a keyframe at the playhead",
    onclick: (e) => { e.preventDefault(); commit("keyframe"); toggleKey(it, prop, current); changed("props"); renderDesign(); } }, "◆");
  const jump = (dir) => (e) => {
    e.preventDefault();
    const rel = S.time - itemStart(it), keys = it.kf?.[prop] || [];
    const k = dir > 0 ? keys.find(([t]) => t > rel + 0.02) : [...keys].reverse().find(([t]) => t < rel - 0.02);
    if (k) seek(itemStart(it) + k[0]);
  };
  return row(label, num(current, opts, (v) => { commitSoon(label); setProp(it, prop, v); changed("props"); }),
    keyed ? el("button", { class: "kfn", onclick: jump(-1), title: "Previous keyframe" }, "‹") : null, btn,
    keyed ? el("button", { class: "kfn", onclick: jump(1), title: "Next keyframe" }, "›") : null);
}
const set = (it, label, fn) => (v) => { commitSoon(label); fn(v); changed("props"); };
const setAll = (it, prop, label) => (v) => {
  commitSoon(label);
  const t = trackOf(it.id);
  if (t.kind === "caption" && $("#cap-all")?.checked) t.items.forEach((x) => { x[prop] = v; }); else it[prop] = v;
  changed("props");
};

// ------------------------------------------------------------------ Design (inspector)
const design = $("#p-design");
export function renderDesign() {
  const it = selectedItem();
  design.replaceChildren();
  if (!it) { design.append(projectSettings()); return; }
  const t = trackOf(it.id);
  const head = el("div", { class: "ihead" }, el("b", {}, { main: "Video clip", overlay: "Image / video", text: "Text", caption: "Caption", audio: "Sound" }[t.kind]),
    el("span", { class: "muted" }, ` ${fmt(itemStart(it))} → ${fmt(itemStart(it) + itemDur(it))}`),
    el("span", { class: "grow" }),
    el("button", { class: "ib", title: "Split at playhead (S)", onclick: () => A.split(it) }, "✂"),
    el("button", { class: "ib", title: "Duplicate (Ctrl+D)", onclick: () => A.duplicate(it) }, "⧉"),
    el("button", { class: "ib danger", title: "Delete (Del)", onclick: () => A.remove(it) }, "🗑"));
  design.append(head);
  if (t.kind === "main") design.append(...mainClip(it));
  else if (t.kind === "overlay") design.append(...overlay(it));
  else if (t.kind === "text" || t.kind === "caption") design.append(...text(it, t));
  else if (t.kind === "audio") design.append(...sound(it));
}

function mainClip(it) {
  const a = asset(it.asset) || {};
  const rel = clamp(S.time - itemStart(it), 0, itemLength(it));
  const tr = it.transition || { type: "none", dur: 0.5 };
  const isFirst = mainTrack().items[0] === it;
  return [
    section("Clip", el("p", { class: "muted small" }, `${a.name || it.asset} · ${fmt(it.in)}–${fmt(it.out)} of the source`),
      a.kind === "video" ? row("Speed", num(it.speed, { min: 0.25, max: 4, step: 0.05 }, set(it, "speed", (v) => { it.speed = v; }))) : null,
      a.kind === "video" ? row("Volume", num(it.volume, { min: 0, max: 3, step: 0.05 }, set(it, "volume", (v) => { it.volume = v; }))) : null,
      row("Look", select_(it.look || "none", CAT.looks, set(it, "look", (v) => { it.look = v; })))),
    isFirst ? null : section("Transition into this clip",
      row("Type", select_(tr.type, CAT.transitions, (v) => { A.setTransition(it, v); renderDesign(); })),
      tr.type !== "none" ? row("Length", num(tr.dur, { min: 0.1, max: 2, step: 0.05 }, set(it, "transition", (v) => { it.transition = { ...it.transition, dur: v }; }))) : null,
      el("button", { class: "btn small", onclick: () => A.transitionAll(tr.type) }, "Use on every cut")),
    section("Camera (keyframes)",
      kfNum(it, "zoom", "Zoom", { min: 1, max: 4, step: 0.01 }, round(at(it.kf?.zoom, rel, 1), 3)),
      kfNum(it, "panx", "Pan ←→", { min: -540, max: 540, step: 1 }, round(at(it.kf?.panx, rel, 0), 1)),
      kfNum(it, "pany", "Pan ↑↓", { min: -960, max: 960, step: 1 }, round(at(it.kf?.pany, rel, 0), 1)),
      el("div", { class: "chips" }, ...CAT.clip_effects.map((fx) => el("button", { class: "chip", onclick: () => A.clipEffect(fx, it) }, fx.name)))),
  ];
}
const at = (keys, t, d) => {
  if (!keys || !keys.length) return d;
  if (t <= keys[0][0]) return keys[0][1];
  for (let i = 0; i < keys.length - 1; i++) { const [a, va] = keys[i], [b, vb] = keys[i + 1]; if (t <= b) return b === a ? va : va + (vb - va) * (t - a) / (b - a); }
  return keys[keys.length - 1][1];
};

function motionSection(it) {
  const m = motionAt(it, clamp(S.time, it.start, it.start + it.dur));
  const rel = clamp(S.time - it.start, 0, it.dur);
  return section("Position & motion (keyframes)",
    kfNum(it, "x", "X", { min: -W, max: 2 * W, step: 1 }, round(at(it.kf?.x, rel, it.x), 1)),
    kfNum(it, "y", "Y", { min: -H, max: 2 * H, step: 1 }, round(at(it.kf?.y, rel, it.y), 1)),
    kfNum(it, "scale", "Scale", { min: 0, max: 5, step: 0.01 }, round(at(it.kf?.scale, rel, 1), 3)),
    kfNum(it, "rotation", "Rotate", { min: -360, max: 360, step: 1 }, round(at(it.kf?.rotation, rel, it.rotation || 0), 1)),
    kfNum(it, "opacity", "Opacity", { min: 0, max: 1, step: 0.01 }, round(at(it.kf?.opacity, rel, it.opacity ?? 1), 2)),
    el("div", { class: "two" },
      row("In", select_(it.anim_in, CAT.anims, set(it, "anim in", (v) => { it.anim_in = v; }))),
      row("Out", select_(it.anim_out, CAT.anims, set(it, "anim out", (v) => { it.anim_out = v; })))),
    el("p", { class: "muted small" }, `Now: ${round(m.x)} , ${round(m.y)} · ◆ adds a keyframe at the playhead; once a setting has keyframes, changing it keys it.`));
}

function overlay(it) {
  const a = asset(it.asset) || {};
  return [
    section("Media", el("p", { class: "muted small" }, a.name || it.asset),
      row("Fit", select_(it.fit, { free: "Free (move & size)", fill: "Fill the screen (B-roll)" }, set(it, "fit", (v) => { it.fit = v; }))),
      it.fit === "free" ? row("Width", num(it.w, { min: 8, max: 2160, step: 1 }, set(it, "width", (v) => { it.w = v; }))) : null,
      a.src?.startsWith("shape/") ? row("Stretch", select_(it.scale_mode, { uniform: "Keep shape", width: "Sideways only (bars)" }, set(it, "scale mode", (v) => { it.scale_mode = v; }))) : null,
      a.kind === "video" && a.audio ? row("Volume", num(it.volume, { min: 0, max: 3, step: 0.05 }, set(it, "volume", (v) => { it.volume = v; }))) : null),
    motionSection(it),
  ];
}

function text(it, t) {
  const ta = el("textarea", { rows: 2, id: "tx-edit" }, it.text);
  ta.addEventListener("input", () => {
    commitSoon("text");
    it.text = ta.value;
    if (it.words && it.words.length !== it.text.split(/\s+/).filter(Boolean).length) it.words = null;
    changed("props");
  });
  const fonts = Object.fromEntries(Object.keys(CAT.fonts).map((f) => [f, f]));
  const fontSel = select_(it.font, fonts, setAll(it, "font", "font"));
  [...fontSel.options].forEach((o) => { o.style.fontFamily = fontCss(o.value); });
  return [
    t.kind === "caption" ? el("label", { class: "chk allcap" }, el("input", { type: "checkbox", id: "cap-all", checked: true }), "Style changes apply to all captions") : null,
    section("Text", ta,
      row("Font", fontSel),
      row("Size", num(it.size, { min: 12, max: 400, step: 1 }, setAll(it, "size", "size"))),
      el("div", { class: "two" }, row("Colour", color(it.color, setAll(it, "color", "colour"))),
        row("Align", select_(it.align, { center: "Centre", left: "Left", right: "Right" }, setAll(it, "align", "align")))),
      el("div", { class: "two" }, row("Outline", num(it.outline, { min: 0, max: 30, step: 1 }, setAll(it, "outline", "outline"))),
        row("", color(it.outline_color, setAll(it, "outline_color", "outline colour")))),
      row("Shadow", num(it.shadow, { min: 0, max: 20, step: 1 }, setAll(it, "shadow", "shadow"))),
      row("Opacity (translucent)", num(round(it.opacity ?? 1, 2), { min: 0, max: 1, step: 0.05 }, setAll(it, "opacity", "opacity"))),
      el("div", { class: "two" }, check(it.box, "Box behind", setAll(it, "box", "box")), color(it.box_color, setAll(it, "box_color", "box colour"))),
      it.box ? row("Box opacity", num(round(it.box_opacity ?? 1, 2), { min: 0, max: 1, step: 0.05 }, setAll(it, "box_opacity", "box opacity"))) : null,
      check(it.uppercase, "ALL CAPS", setAll(it, "uppercase", "caps"))),
    section("Effect",
      row("Words", select_(it.effect, { none: "None", highlight: "Highlight the spoken word", words: "Appear word by word", typewriter: "Typewriter" }, setAll(it, "effect", "effect"))),
      it.effect === "highlight" ? row("Highlight", color(it.accent, setAll(it, "accent", "highlight colour"))) : null),
    motionSection(it),
  ];
}

function sound(it) {
  const a = asset(it.asset) || {};
  return [
    section("Sound", el("p", { class: "muted small" }, a.name || it.asset),
      kfNum(it, "volume", "Volume", { min: 0, max: 3, step: 0.01 }, round(at(it.kf?.volume, clamp(S.time - it.start, 0, it.dur), it.volume), 2)),
      row("Start in song", num(it.in, { min: 0, max: Math.max(1, a.duration || 600), step: 0.1 }, set(it, "in", (v) => { it.in = v; }))),
      row("Fade in", num(it.fade_in, { min: 0, max: 10, step: 0.1 }, set(it, "fade in", (v) => { it.fade_in = v; }))),
      row("Fade out", num(it.fade_out, { min: 0, max: 10, step: 0.1 }, set(it, "fade out", (v) => { it.fade_out = v; }))),
      check(it.duck, "Lower it while someone talks", set(it, "duck", (v) => { it.duck = v; })),
      check(it.music, "This is background music (TikTok gets a version without it, for a trending sound)", set(it, "music", (v) => { it.music = v; })))];
}

function projectSettings() {
  const p = S.project;
  return el("div", {},
    el("p", { class: "muted small" }, "Nothing selected: click something on the video or the timeline. These settings apply to the whole video."),
    section("Look", el("div", { class: "swatches" }, ...Object.entries(CAT.looks).map(([k, label]) => el("button", {
      class: "swatch" + (p.look.filter === k ? " on" : ""), onclick: () => { commit("look"); p.look.filter = k; changed("props"); renderDesign(); } },
      el("span", { class: "sample", style: { filter: CAT.css_looks[k] } }), label)))),
    section("Fades",
      row("Fade in", num(p.look.fade_in, { min: 0, max: 3, step: 0.1 }, set(null, "fade in", (v) => { p.look.fade_in = v; }))),
      row("Fade out", num(p.look.fade_out, { min: 0, max: 3, step: 0.1 }, set(null, "fade out", (v) => { p.look.fade_out = v; })))),
    section("Sound", check(p.ducking, "Lower music while someone talks", set(null, "ducking", (v) => { p.ducking = v; }))));
}

// ------------------------------------------------------------------ Layers
const layers = $("#p-layers");
export function renderLayers() {
  layers.replaceChildren();
  for (const t of tracks()) {
    layers.append(el("div", { class: "lgroup" }, el("b", {}, t.name), el("span", { class: "muted small" }, ` ${t.items.length}`)));
    for (const it of t.items) {
      const a = asset(it.asset);
      const label = it.text ?? (a?.name || it.asset);
      layers.append(el("button", { class: "litem" + (S.selected?.id === it.id ? " on" : ""), onclick: () => { select(it.id); seek(itemStart(it) + 0.01); } },
        el("span", { class: "muted mono" }, fmt(itemStart(it))), " ", label));
    }
  }
}

// ------------------------------------------------------------------ Export
export function renderExport() {
  const p = S.project, x = p.export;
  const box = $("#p-export");
  box.replaceChildren(
    section("Video", row("Size", select_(String(x.height), { 1920: "1080 × 1920 (1080p)", 1280: "720 × 1280 (720p)" }, set(null, "size", (v) => { x.height = Number(v); }))),
      row("Frame rate", select_(String(x.fps), { 24: "24 fps", 30: "30 fps", 60: "60 fps" }, set(null, "fps", (v) => { x.fps = Number(v); }))),
      row("Quality", select_(x.quality, { small: "Smaller file", standard: "Standard", high: "High" }, set(null, "quality", (v) => { x.quality = v; })))),
    el("p", { class: "muted small" }, `Length ${fmt(duration())}. Rendering takes about a minute on this PC (more with many effects).`),
    D.editable ? el("button", { class: "btn primary wide", onclick: () => emit("save") }, "Save & render") :
      el("p", { class: "banner warn small" }, `This clip is ${D.state}, so its final video can't change now. Your work is kept as a draft.`));
}

// ------------------------------------------------------------------ Media
const mediaBox = $("#p-media");
export function renderMedia() {
  const all = { ...D.assets };
  mediaBox.replaceChildren();
  const up = el("input", { type: "file", multiple: true, accept: "video/*,image/*,audio/*", hidden: true });
  up.addEventListener("change", () => uploadFiles([...up.files]));
  mediaBox.append(el("div", { class: "row" }, el("button", { class: "btn primary small", onclick: () => up.click() }, "⬆ Import media"), up,
    el("span", { class: "muted small" }, "or drop files here")));
  const groups = { video: "Video", image: "Images", audio: "Sound & music" };
  for (const [kind, title] of Object.entries(groups)) {
    const list = Object.entries(all).filter(([, a]) => a.kind === kind);
    if (!list.length) continue;
    mediaBox.append(el("h4", {}, title));
    const grid = el("div", { class: kind === "audio" ? "alist" : "mgrid" });
    for (const [aid, a] of list) grid.append(mediaCard(aid, a));
    mediaBox.append(grid);
  }
  if (!Object.values(all).some((a) => a.src?.startsWith("music/")))
    mediaBox.append(el("p", { class: "muted small" }, "Your music library is empty: drop songs into Clipper's music folder, or import a sound here."));
}
function mediaCard(aid, a) {
  const add = (e) => { e.stopPropagation(); A.addAsset(aid, S.time); };
  const drag = { draggable: "true", ondragstart: (e) => e.dataTransfer.setData("application/x-clipper-asset", aid) };
  if (a.kind === "audio") {
    let au = null;
    return el("div", { class: "acard", ...drag }, el("button", { class: "ib", title: "Listen", onclick: (e) => { e.stopPropagation(); if (au && !au.paused) { au.pause(); return; } au = au || new Audio(assetUrl(aid)); au.play(); } }, "▶"),
      el("span", { class: "grow" }, a.name, el("span", { class: "muted small" }, ` ${fmt(a.duration)}${a.mood ? " · " + a.mood : ""}`)),
      el("button", { class: "btn small", onclick: add }, "+ Add"));
  }
  const thumb = a.kind === "video" ? el("video", { src: assetUrl(aid) + "#t=1", muted: true, preload: "metadata" }) : el("img", { src: assetUrl(aid), alt: "" });
  return el("div", { class: "mcard", title: "Drag onto the timeline, or click + to add at the playhead", ...drag }, thumb,
    el("div", { class: "mname" }, a.name), el("div", { class: "mbtns" },
      el("button", { class: "btn small", onclick: add }, "+ On top"),
      a.kind === "video" ? el("button", { class: "btn small", onclick: (e) => { e.stopPropagation(); A.addAsset(aid, S.time, null, { main: true }); } }, "+ Main") : null));
}
async function uploadFiles(files) {
  for (const f of files) {
    const fd = new FormData(); fd.append("file", f);
    const note = el("p", { class: "muted small" }, `Uploading ${f.name}…`);
    mediaBox.prepend(note);
    try {
      const r = await fetch(`/api/clips/${D.id}/upload`, { method: "POST", body: fd });
      const body = await r.json();
      if (!r.ok) throw new Error(body.detail || r.status);
      D.assets[body.id] = body.asset;
    } catch (err) { alert(`Couldn't add ${f.name}: ${err.message}`); }
    note.remove();
  }
  renderMedia();
}
mediaBox.addEventListener("dragover", (e) => { if (e.dataTransfer.types.includes("Files")) { e.preventDefault(); mediaBox.classList.add("drop"); } });
mediaBox.addEventListener("dragleave", () => mediaBox.classList.remove("drop"));
mediaBox.addEventListener("drop", (e) => { if (!e.dataTransfer.files.length) return; e.preventDefault(); mediaBox.classList.remove("drop"); uploadFiles([...e.dataTransfer.files]); });

// ------------------------------------------------------------------ Catalog
const catBox = $("#p-catalog");
export function renderCatalog() {
  const groups = {
    Titles: () => CAT.titles.map((p) => card(p.name, textSample(p.item), () => A.addText(p.item))),
    Captions: () => CAT.captions.map((p) => card(p.name, textSample({ ...p.style, text: "the WORD here" }), () => A.styleCaptions(p.style), "Restyle all captions")),
    Social: () => CAT.social.map((p) => card(p.name, el("span", { class: "big" }, { credit: "🎥", follow: "👉", likeshare: "❤️", progress: "▬", hook_title: "🪝", subscribe: "👇" }[p.id] || "★"), () => A.addSocial(p))),
    Stickers: () => CAT.emoji.map((code) => el("button", { class: "emo", title: "Add at the playhead", onclick: () => A.addEmoji(code) },
      el("img", { src: assetUrl("emoji:" + code), alt: code, loading: "lazy" }))),
    Shapes: () => Object.keys(CAT.shapes).map((name) => card(name, el("span", { class: "shp", style: { background: CAT.shapes[name].color, borderRadius: Math.min(16, CAT.shapes[name].radius) + "px" } }),
      () => A.addAsset(A.shapeAsset(name), S.time))),
    Transitions: () => Object.entries(CAT.transitions).filter(([k]) => k !== "none").map(([k, name]) => card(name, el("span", { class: "big" }, "⇄"), () => {
      const sel = S.selected && trackOf(S.selected.id)?.kind === "main" ? itemById(S.selected.id) : null;
      if (sel && mainTrack().items.indexOf(sel) > 0) A.setTransition(sel, k); else A.transitionAll(k);
    }, "On the selected cut, or every cut")),
    Effects: () => CAT.clip_effects.map((fx) => card(fx.name, el("span", { class: "big" }, "🎥"), () => A.clipEffect(fx), "On the selected clip")),
    Animations: () => Object.entries(CAT.anims).filter(([k]) => k !== "none").map(([k, name]) => card(name, el("span", { class: "big" }, "✨"), () => {
      const it = selectedItem();
      if (!it || !("anim_in" in it)) { alert("Select a text, sticker or image first."); return; }
      commit("animation"); it.anim_in = k; changed("props"); renderDesign();
    }, "Entrance of the selected item")),
  };
  catBox.replaceChildren();
  const bar = el("div", { class: "cattabs" });
  const body = el("div", { class: "catbody" });
  const show = (g) => { $$("button", bar).forEach((b) => b.classList.toggle("on", b.textContent === g)); body.replaceChildren(...groups[g]()); body.className = "catbody g-" + g.toLowerCase(); };
  Object.keys(groups).forEach((g) => bar.append(el("button", { class: "ctab", onclick: () => show(g) }, g)));
  catBox.append(bar, body);
  show("Titles");
}
function card(name, preview, onAdd, hint = "Add at the playhead") {
  return el("button", { class: "ccard", title: hint, onclick: onAdd }, el("div", { class: "cprev" }, preview), el("div", { class: "cname" }, name));
}
function textSample(st) {
  const s = el("span", { class: "tsample" }, (st.uppercase ? (st.text || "Aa").toUpperCase() : st.text || "Aa").replace("{title}", "Title").replace("{credit}", "@you").slice(0, 22));
  Object.assign(s.style, { fontFamily: fontCss(st.font), color: st.color, background: st.box ? st.box_color : "",
    WebkitTextStroke: st.outline && !st.box ? `${Math.min(3, st.outline / 2)}px ${st.outline_color}` : "", paintOrder: "stroke fill",
    padding: st.box ? "0 6px" : "" });
  if (st.effect === "highlight") { s.textContent = ""; s.append("the ", el("span", { style: { color: st.accent } }, "WORD"), " here"); }
  return s;
}

// ------------------------------------------------------------------ Captions
const capBox = $("#p-captions");
export function renderCaptions() {
  const t = tracks().find((x) => x.kind === "caption");
  const styleSel = select_("0", Object.fromEntries(CAT.captions.map((c, i) => [String(i), c.name])), () => {});
  const go = el("button", { class: "btn primary" }, t?.items.length ? "✨ Redo auto captions" : "✨ Auto captions");
  const status = el("p", { class: "muted small" }, "Listens to the clip on this PC and adds word-timed captions where each word lands after your cuts. Check the words: accents can confuse it.");
  go.addEventListener("click", async () => {
    if (t?.items.length && !confirm("Replace the captions you have?")) return;
    go.disabled = true; status.textContent = "Listening… about a minute (the first time also downloads the speech model).";
    try {
      const r = await fetch(`/api/clips/${D.id}/captions2`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project: S.project, style: CAT.captions[Number(styleSel.value)].style }) });
      const body = await r.json();
      if (!r.ok) throw new Error(body.detail || r.status);
      A.setCaptions(body.items);
      status.textContent = `Added ${body.items.length} captions.`;
    } catch (err) { status.textContent = "Couldn't make captions: " + err.message; }
    go.disabled = false;
  });
  const list = el("div", { class: "caplist" });
  (t?.items || []).forEach((it) => {
    const inp = el("input", { value: it.text });
    inp.addEventListener("focus", () => { select(it.id); seek(it.start + 0.01); });
    inp.addEventListener("input", () => { commitSoon("caption text"); it.text = inp.value; if (it.words && it.words.length !== it.text.split(/\s+/).filter(Boolean).length) it.words = null; changed("props"); });
    list.append(el("div", { class: "caprow" }, el("span", { class: "mono muted" }, fmt(it.start)), inp,
      el("button", { class: "ib", title: "Delete this caption", onclick: () => A.remove(it) }, "✕")));
  });
  capBox.replaceChildren(row("Style", styleSel), go, status, t?.items.length ? el("h4", {}, `Fix the words (${t.items.length})`) : null, list);
}

// ------------------------------------------------------------------ AI
export function renderAI() {
  const box = $("#p-ai");
  const ta = el("textarea", { rows: 4, placeholder: "e.g. Make the captions bigger and yellow, add a fire emoji when he laughs, fade to black at the end" });
  const go = el("button", { class: "btn primary" }, "Ask");
  const status = el("p", { class: "muted small" }, "Gemini changes the edit for you. Undo (Ctrl+Z) takes it back.");
  go.addEventListener("click", async () => {
    go.disabled = true; status.textContent = "Thinking…";
    try {
      const r = await fetch(`/api/clips/${D.id}/assist`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project: S.project, request: ta.value }) });
      const body = await r.json();
      if (!r.ok) throw new Error(body.detail || r.status);
      commit("AI change"); S.project = body.project; select(null); changed();
      status.textContent = "Done. Undo (Ctrl+Z) if you don't like it.";
    } catch (err) { status.textContent = err.message; }
    go.disabled = false;
  });
  box.replaceChildren(ta, go, status);
}

on("select", () => { renderDesign(); renderLayers(); });
on("change", (what) => {
  if (what !== "props" && what !== "canvas" && what !== "trim") { renderDesign(); renderLayers(); renderCaptions(); renderExport(); }
  else if (what === "trim" || what === "canvas") renderDesignSoon();
});
let soon = null;
function renderDesignSoon() { clearTimeout(soon); soon = setTimeout(() => { if (!document.activeElement?.closest("#p-design")) renderDesign(); }, 120); }
on("tick", () => { if (!S.playing) renderDesignSoon(); });
export { deep };
