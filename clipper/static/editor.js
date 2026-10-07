// Timeline editor. Holds the edit as JSON, previews it live on top of the base
// clip (CSS filters, HTML text, an <audio> for music), and POSTs it to be
// rendered by ffmpeg. Text times are relative to the trimmed clip's start.
(() => {
  const D = JSON.parse(document.getElementById("editor-data").textContent);
  const O = D.options;
  const total = D.duration;
  const edit = JSON.parse(JSON.stringify(D.edit));
  const $ = (id) => document.getElementById(id);
  const vid = $("vid"), music = $("music"), stage = $("stage"), layer = $("textlayer"), shade = $("shade");
  let selected = -1;
  let dirty = false;

  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const len = () => edit.trim.end - edit.trim.start;
  const round = (v) => Math.round(v * 1000) / 1000;
  const fmt = (s) => {
    s = Math.max(0, s);
    const m = Math.floor(s / 60);
    return `${m}:${(s % 60).toFixed(1).padStart(4, "0")}`;
  };
  function markDirty() {
    dirty = true;
    $("dirty").textContent = "Unsaved changes";
  }
  window.addEventListener("beforeunload", (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });

  // ------------------------------------------------------------- panels
  document.querySelectorAll("#paneltabs .tab").forEach((tab) => tab.addEventListener("click", (e) => {
    e.preventDefault();
    document.querySelectorAll("#paneltabs .tab").forEach((t) => t.classList.toggle("active", t === tab));
    document.querySelectorAll(".panel").forEach((p) => { p.hidden = p.dataset.panel !== tab.dataset.panel; });
  }));

  const ranges = [];
  function bindRange(id, get, set, digits = 1) {
    const el = $(id);
    const out = document.querySelector(`output[data-for="${id}"]`);
    const sync = () => { el.value = get(); if (out) out.textContent = Number(el.value).toFixed(digits); };
    el.addEventListener("input", () => {
      set(Number(el.value));
      if (out) out.textContent = Number(el.value).toFixed(digits);
      markDirty();
      redraw();
    });
    ranges.push(sync);
    sync();
    return el;
  }
  const syncAll = () => ranges.forEach((s) => s());

  // ---- look
  const filterBox = $("filters");
  Object.entries(O.filters).forEach(([key, label]) => {
    const b = document.createElement("button");
    b.className = "swatch";
    b.dataset.key = key;
    b.innerHTML = `<span class="sample" style="filter:${O.css_filters[key]}"></span>${label}`;
    b.addEventListener("click", () => { edit.filter = key; markDirty(); redraw(); });
    filterBox.appendChild(b);
  });
  const zoom = $("zoom");
  Object.entries(O.zooms).forEach(([key, label]) => zoom.add(new Option(label, key)));
  zoom.value = edit.zoom;
  zoom.addEventListener("change", () => { edit.zoom = zoom.value; markDirty(); redraw(); });
  bindRange("fade_in", () => edit.fade_in, (v) => { edit.fade_in = v; });
  bindRange("fade_out", () => edit.fade_out, (v) => { edit.fade_out = v; });
  // the render clamps fades to half the clip; keep the sliders (and the preview) honest
  function fitFades() {
    const half = len() / 2;
    [["fade_in", 3, edit, "fade_in"], ["fade_out", 3, edit, "fade_out"],
     ["music_fade_in", 5, edit.music, "fade_in"], ["music_fade_out", 5, edit.music, "fade_out"]]
      .forEach(([id, cap, obj, key]) => {
        const max = Math.max(0, Math.min(cap, half));
        $(id).max = max;
        if (obj[key] > max) obj[key] = round(max);
      });
  }
  bindRange("voice_volume", () => edit.voice_volume, (v) => { edit.voice_volume = v; }, 2);
  $("setin").addEventListener("click", () => setTrim(vid.currentTime, edit.trim.end));
  $("setout").addEventListener("click", () => setTrim(edit.trim.start, vid.currentTime));

  // ---- music
  const track = $("track");
  track.add(new Option("— no music —", ""));
  D.tracks.forEach((t) => track.add(new Option(t.title + (t.mood ? ` (${t.mood})` : ""), t.file)));
  if (edit.music.track && !D.tracks.some((t) => t.file === edit.music.track)) edit.music.track = null;
  track.value = edit.music.track || "";
  if (D.suggestions.mood) $("moodhint").textContent = `Gemini suggests a ${D.suggestions.mood} track for this clip.`;
  track.addEventListener("change", () => {
    edit.music.track = track.value || null;
    loadMusic();
    markDirty();
    redraw();
  });
  bindRange("music_volume", () => edit.music.volume, (v) => { edit.music.volume = v; }, 2);
  const offset = bindRange("music_offset", () => edit.music.offset, (v) => { edit.music.offset = v; });
  bindRange("music_fade_in", () => edit.music.fade_in, (v) => { edit.music.fade_in = v; });
  bindRange("music_fade_out", () => edit.music.fade_out, (v) => { edit.music.fade_out = v; });
  const ducking = $("ducking");
  ducking.checked = edit.music.ducking;
  ducking.addEventListener("change", () => { edit.music.ducking = ducking.checked; markDirty(); });
  let soloing = false;
  $("trackpreview").addEventListener("click", () => {
    if (!edit.music.track) return;
    vid.pause();
    soloing = !soloing;
    if (soloing) { music.currentTime = edit.music.offset; music.volume = 1; music.play().catch(() => {}); }
    else music.pause();
  });
  function loadMusic() {
    music.pause();
    soloing = false;
    if (edit.music.track) {
      music.src = "/music/" + encodeURIComponent(edit.music.track);
      music.load();
    } else {
      music.removeAttribute("src");
    }
  }
  music.addEventListener("loadedmetadata", () => {
    if (isFinite(music.duration)) {
      offset.max = Math.max(1, Math.floor(music.duration - 1));
      // a start point past the end of this song would silently wrap around
      if (edit.music.offset > Number(offset.max)) { edit.music.offset = Number(offset.max); markDirty(); }
    }
    syncAll();
    redraw();
  });

  // ---- text
  const styleSel = $("t_style"), posSel = $("t_position"), animSel = $("t_animation");
  Object.keys(O.styles).forEach((s) => styleSel.add(new Option(s, s)));
  O.positions.forEach((p) => posSel.add(new Option(p, p)));
  O.animations.forEach((a) => animSel.add(new Option(a, a)));
  const cur = () => edit.texts[selected];
  const WORD_ANIMS = ["words", "highlight"];
  // style changes go to the selected text, or to every auto caption when that box is ticked
  const targets = () => (!cur() ? [] : cur().auto && $("t_all").checked ? edit.texts.filter((t) => t.auto) : [cur()]);
  const shifted = (words, by) => (words ? words.map(([a, b]) => [round(a + by), round(b + by)]) : null);
  // when each word is spoken: its own timings (auto captions) or spread evenly, like the render
  function wordTimes(t) {
    const words = t.text.split(/\s+/).filter(Boolean);
    if (t.words && t.words.length === words.length) return t.words;
    const step = (t.end - t.start) / Math.max(1, words.length);
    return words.map((_, i) => [t.start + i * step, t.start + (i + 1) * step]);
  }

  function addText(text, start, end) {
    if (edit.texts.length >= O.max_texts) { alert(`Up to ${O.max_texts} texts.`); return; }
    start = clamp(start, 0, Math.max(0, len() - 0.5));
    edit.texts.push({ text, start: round(start), end: round(clamp(end, start + 0.5, len())), style: "classic",
                      position: "top", size: 72, color: O.styles.classic.color, animation: "pop",
                      accent: "#FFE600", words: null, auto: false });
    select(edit.texts.length - 1);
    markDirty();
  }
  $("addtext").addEventListener("click", () => {
    const rel = clamp(vid.currentTime - edit.trim.start, 0, len());
    addText("Your text", rel, rel + 3);
    $("t_text").focus();
    $("t_text").select();
  });
  $("addtitle").addEventListener("click", () => addText((D.suggestions.title || "Wait for it…").toUpperCase(), 0, 3));

  function select(i) {
    selected = i;
    $("texteditor").hidden = i < 0;
    if (i >= 0) {
      const t = cur();
      $("t_text").value = t.text;
      styleSel.value = t.style;
      posSel.value = t.position;
      animSel.value = t.animation;
      $("t_color").value = t.color.toLowerCase();
      $("t_accent").value = (t.accent || "#FFE600").toLowerCase();
      $("t_allwrap").hidden = !t.auto;
      $("t_start").max = $("t_end").max = len();
      syncAll();
      document.querySelector('#paneltabs .tab[data-panel="text"]').click();
    }
    redraw();
  }
  $("t_text").addEventListener("input", () => {
    if (!cur()) return;
    cur().text = $("t_text").value;
    // fixing a misheard word keeps the timings; adding or removing words spreads them evenly
    if (cur().words && cur().words.length !== cur().text.split(/\s+/).filter(Boolean).length) cur().words = null;
    markDirty(); redraw();
  });
  const setAll = (fn) => { targets().forEach(fn); markDirty(); redraw(); };
  styleSel.addEventListener("change", () => {
    setAll((t) => { t.style = styleSel.value; t.color = O.styles[styleSel.value].color; });
    if (cur()) $("t_color").value = cur().color.toLowerCase();
  });
  posSel.addEventListener("change", () => setAll((t) => { t.position = posSel.value; }));
  animSel.addEventListener("change", () => setAll((t) => { t.animation = animSel.value; }));
  $("t_color").addEventListener("input", () => setAll((t) => { t.color = $("t_color").value.toUpperCase(); }));
  $("t_accent").addEventListener("input", () => setAll((t) => { t.accent = $("t_accent").value.toUpperCase(); }));
  bindRange("t_size", () => (cur() ? cur().size : 72), (v) => { targets().forEach((t) => { t.size = v; }); }, 0);
  bindRange("t_start", () => (cur() ? cur().start : 0), (v) => {
    if (cur()) { cur().start = round(Math.min(v, cur().end - 0.2)); }
  });
  bindRange("t_end", () => (cur() ? cur().end : 0), (v) => {
    if (cur()) { cur().end = round(Math.max(v, cur().start + 0.2)); }
  });
  $("t_here").addEventListener("click", () => {
    if (!cur()) return;
    const span = cur().end - cur().start, was = cur().start;
    cur().start = round(clamp(vid.currentTime - edit.trim.start, 0, len() - 0.2));
    cur().end = round(Math.min(len(), cur().start + span));
    cur().words = shifted(cur().words, cur().start - was);
    syncAll(); markDirty(); redraw();
  });
  $("t_delete").addEventListener("click", () => {
    if (!cur()) return;
    edit.texts.splice(selected, 1);
    select(-1);
    markDirty();
  });

  // ---- auto captions: HyperFrames' speech-to-text on this PC, grouped into short captions
  const capBtn = $("autocaps"), capStatus = $("capstatus");
  capBtn.addEventListener("click", async () => {
    const had = edit.texts.filter((t) => t.auto).length;
    if (had && !confirm(`Replace the ${had} auto caption${had === 1 ? "" : "s"} you have with fresh ones?`)) return;
    capBtn.disabled = true;
    capStatus.textContent = "Listening to the clip… about a minute (the very first time it also downloads the speech model, a few more minutes).";
    try {
      const resp = await fetch(`/api/clips/${D.id}/captions`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ trim: edit.trim }),
      });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(body.detail || `HTTP ${resp.status}`);
      const keep = edit.texts.filter((t) => !t.auto);
      const room = O.max_texts - keep.length;
      edit.texts = keep.concat(body.texts.slice(0, Math.max(0, room)));
      capStatus.textContent = body.texts.length
        ? `Added ${Math.min(body.texts.length, room)} captions. Click one to fix a word or change the look of all of them.`
        : "No speech found in this part of the clip.";
      select(-1);
      markDirty();
    } catch (err) {
      capStatus.textContent = "Couldn't make captions: " + err.message;
    } finally {
      capBtn.disabled = false;
    }
  });

  // ------------------------------------------------------------- trimming
  const snapshot = () => ({ trim: { ...edit.trim }, texts: edit.texts.map((t) => ({ ...t })) });

  // `base` is the state when the drag started: text positions are always recomputed
  // from it, never from the previous drag step, so dragging back and forth is lossless.
  // Text that ends up outside the trimmed clip is kept (and hidden), not squashed; the
  // server drops it only if it's still outside when you save.
  function setTrim(start, end, base = snapshot()) {
    start = clamp(start, 0, total - 1);
    end = clamp(end, start + 1, total);
    const shift = start - base.trim.start;
    edit.trim = { start: round(start), end: round(end) };
    // keep text stuck to the same moment of footage when the start moves
    edit.texts.forEach((t, i) => {
      t.start = round(base.texts[i].start - shift);
      t.end = round(base.texts[i].end - shift);
      t.words = shifted(base.texts[i].words, -shift);
    });
    $("t_start").max = $("t_end").max = len();
    fitFades();
    syncAll();
    markDirty();
    redraw();
  }

  // ------------------------------------------------------------- timeline
  const timeline = $("timeline");
  const laneVideo = $("lane_video"), laneText = $("lane_text"), laneMusic = $("lane_music");
  const pps = () => laneVideo.clientWidth / total;           // pixels per second
  const laneLeft = () => laneVideo.getBoundingClientRect().left;
  const timeAt = (clientX) => clamp((clientX - laneLeft()) / pps(), 0, total);

  function drawRuler() {
    const ruler = $("ruler");
    ruler.innerHTML = "";
    ruler.style.marginLeft = "0";
    ruler.style.marginLeft = laneVideo.offsetLeft - ruler.offsetLeft + "px";
    ruler.style.width = laneVideo.clientWidth + "px";
    const step = total > 120 ? 10 : total > 40 ? 5 : total > 15 ? 2 : 1;
    for (let s = 0; s <= total; s += step) {
      const tick = document.createElement("span");
      tick.style.left = s * pps() + "px";
      tick.textContent = fmt(s).replace(/\.0$/, "");
      ruler.appendChild(tick);
    }
  }

  function drawTimeline() {
    const vbar = $("vbar");
    vbar.style.left = edit.trim.start * pps() + "px";
    vbar.style.width = len() * pps() + "px";
    $("vlabel").textContent = `${len().toFixed(1)} s`;
    laneText.innerHTML = "";
    edit.texts.forEach((t, i) => {
      const block = document.createElement("div");
      block.className = "tblock" + (i === selected ? " sel" : "");
      block.style.left = (edit.trim.start + t.start) * pps() + "px";
      block.style.width = Math.max(6, (t.end - t.start) * pps()) + "px";
      block.innerHTML = `<span class="handle left" data-edge="start"></span><span class="tlabel"></span><span class="handle right" data-edge="end"></span>`;
      block.querySelector(".tlabel").textContent = t.text;
      block.dataset.index = i;
      laneText.appendChild(block);
    });
    laneMusic.innerHTML = "";
    if (edit.music.track) {
      const m = document.createElement("div");
      m.className = "mblock";
      m.style.left = edit.trim.start * pps() + "px";
      m.style.width = len() * pps() + "px";
      m.textContent = `♪ ${edit.music.track} · from ${fmt(edit.music.offset)}`;
      laneMusic.appendChild(m);
    }
  }

  // dragging: trim handles, text blocks (move / resize), playhead (seek)
  let drag = null;
  timeline.addEventListener("pointerdown", (e) => {
    const handle = e.target.closest("[data-handle]");
    const block = e.target.closest(".tblock");
    if (handle) {
      drag = { kind: "trim", which: handle.dataset.handle, base: snapshot() };
    } else if (block) {
      const i = Number(block.dataset.index);
      if (i !== selected) select(i);
      const t = edit.texts[i];
      const edge = e.target.dataset.edge;
      drag = { kind: edge ? "edge" : "move", edge, i, x: e.clientX, start: t.start, end: t.end, words: t.words };
    } else {
      drag = { kind: "seek" };
      seek(timeAt(e.clientX));
    }
    timeline.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  timeline.addEventListener("pointermove", (e) => {
    if (!drag) return;
    if (drag.kind === "seek") { seek(timeAt(e.clientX)); return; }
    if (drag.kind === "trim") {
      const t = timeAt(e.clientX);
      if (drag.which === "in") setTrim(t, drag.base.trim.end, drag.base);
      else setTrim(drag.base.trim.start, t, drag.base);
      seek(drag.which === "in" ? edit.trim.start : edit.trim.end - 0.05);
      return;
    }
    const t = edit.texts[drag.i];
    const delta = (e.clientX - drag.x) / pps();
    if (drag.kind === "move") {
      const span = drag.end - drag.start;
      t.start = round(clamp(drag.start + delta, 0, len() - span));
      t.end = round(t.start + span);
      t.words = shifted(drag.words, t.start - drag.start);
    } else if (drag.edge === "start") {
      t.start = round(clamp(drag.start + delta, 0, t.end - 0.2));
    } else {
      t.end = round(clamp(drag.end + delta, t.start + 0.2, len()));
    }
    syncAll();
    markDirty();
    redraw();
  });
  const endDrag = () => { drag = null; };
  timeline.addEventListener("pointerup", endDrag);
  timeline.addEventListener("pointercancel", endDrag);

  function seek(t) {
    vid.currentTime = clamp(t, 0, total - 0.01);
    syncMusic(true);
  }

  // ------------------------------------------------------------- preview
  const scale = () => stage.clientHeight / 1920;
  const POS_MARGIN = { top: 260, bottom: 420 };

  function textStyle(el, t) {
    const s = O.styles[t.style];
    const k = scale();
    el.style.fontFamily = `"${s.font}", "Arial Black", Arial, sans-serif`;
    el.style.fontWeight = t.style === "minimal" ? "600" : "900";
    el.style.fontSize = t.size * k + "px";
    el.style.color = t.color;
    el.style.top = el.style.bottom = "";
    el.style.transform = "";
    if (t.position === "top") el.style.top = POS_MARGIN.top * k + "px";
    else if (t.position === "bottom") el.style.bottom = POS_MARGIN.bottom * k + "px";
    else { el.style.top = "50%"; el.style.transform = "translateY(-50%)"; }
    const w = Math.max(1, s.width * k);
    el.style.background = "";
    el.style.webkitTextStroke = "";
    el.style.textShadow = "";
    if (s.box) {
      el.style.background = "rgba(0,0,0,.75)";
      el.style.padding = `${6 * k}px ${14 * k}px`;
    } else if (s.width) {
      el.style.webkitTextStroke = `${w * 2}px ${s.outline}`;
      el.style.paintOrder = "stroke fill";
    }
    if (t.style === "neon") el.style.textShadow = `0 0 ${10 * k}px ${s.outline}, 0 0 ${20 * k}px ${s.outline}`;
    else if (s.shadow) el.style.textShadow = `${s.shadow * k}px ${s.shadow * k}px ${3 * k}px rgba(0,0,0,.6)`;
  }

  let textEls = [];
  function drawTexts() {
    layer.innerHTML = "";
    textEls = edit.texts.map((t, i) => {
      const wrap = document.createElement("div");
      wrap.className = "tline";
      const el = document.createElement("span");
      el.className = "ttext" + (i === selected ? " sel" : "");
      let words = null;
      if (WORD_ANIMS.includes(t.animation)) {  // one span per word, lit or shown as it's spoken
        words = t.text.split(/\s+/).filter(Boolean).map((w, j) => {
          const s = document.createElement("span");
          s.className = "w";
          s.textContent = w;
          if (j) el.appendChild(document.createTextNode(" "));
          el.appendChild(s);
          return s;
        });
      } else {
        el.textContent = t.text;
      }
      textStyle(el, t);
      wrap.appendChild(el);
      wrap.style.top = el.style.top;
      wrap.style.bottom = el.style.bottom;
      wrap.style.transform = el.style.transform;
      el.style.top = el.style.bottom = el.style.transform = "";
      wrap.addEventListener("click", () => select(i));
      wrap.style.display = "none";  // shown by tick() while its time is on screen
      layer.appendChild(wrap);
      return { wrap, el, words, visible: false };
    });
  }

  // live preview of the text effects (the render does them with libass)
  function animateText(txt, item, rel) {
    const anim = txt.animation;
    if (item.words) {
      const times = wordTimes(txt);
      item.words.forEach((s, j) => {
        const [a] = times[j] || [txt.start];
        const next = times[j + 1] ? times[j + 1][0] : txt.end;
        if (anim === "words") s.classList.toggle("hide", rel < a);
        else {
          const hot = rel >= (j ? a : txt.start) && rel < next;
          s.classList.toggle("hot", hot);
          s.style.color = hot ? (txt.accent || "#FFE600") : "";
        }
      });
    } else if (anim === "typewriter") {
      const dur = Math.min(1.2, (txt.end - txt.start) * 0.4);
      const n = Math.ceil(txt.text.length * clamp((rel - txt.start) / dur, 0, 1));
      if (item.el.textContent.length !== n) item.el.textContent = txt.text.slice(0, n);
    }
    if (anim === "fade") item.el.style.opacity = clamp(Math.min((rel - txt.start) / 0.25, (txt.end - rel) / 0.25), 0, 1);
    else item.el.style.opacity = "";
  }

  function redraw() {
    document.querySelectorAll("#filters .swatch").forEach((b) => b.classList.toggle("active", b.dataset.key === edit.filter));
    vid.style.filter = O.css_filters[edit.filter] || "none";
    drawTexts();
    drawTimeline();
    tick(true);
  }

  function syncMusic(force) {
    if (!edit.music.track || !music.src || soloing) return;
    const rel = clamp(vid.currentTime - edit.trim.start, 0, len());
    let target = edit.music.offset + rel;
    if (isFinite(music.duration) && music.duration > 0) target %= music.duration;
    if (force || Math.abs(music.currentTime - target) > 0.25) music.currentTime = target;
  }

  function tick(once) {
    const t = vid.currentTime;
    if (!vid.paused && t >= edit.trim.end - 0.04) { vid.currentTime = edit.trim.start; syncMusic(true); }
    const rel = clamp(t - edit.trim.start, 0, len());
    const L = len();
    const Z = O.zoom_amount;
    const z = edit.zoom === "slow_in" ? 1 + Z * rel / L : edit.zoom === "slow_out" ? 1 + Z - Z * rel / L : 1;
    vid.style.transform = `scale(${z})`;

    let dark = 0;
    if (edit.fade_in && rel < edit.fade_in) dark = 1 - rel / edit.fade_in;
    if (edit.fade_out && rel > L - edit.fade_out) dark = Math.max(dark, (rel - (L - edit.fade_out)) / edit.fade_out);
    const outside = t < edit.trim.start || t > edit.trim.end;
    shade.style.opacity = outside ? 0.65 : dark;
    vid.volume = clamp(edit.voice_volume * (1 - dark), 0, 1);

    edit.texts.forEach((txt, i) => {
      const item = textEls[i];
      if (!item) return;
      const on = !outside && rel >= txt.start && rel < txt.end;
      if (on !== item.visible) {
        item.visible = on;
        item.wrap.style.display = on ? "" : "none";
        const entrance = { pop: "pop", bounce: "bounce", slide: "slide" }[txt.animation];
        if (on && entrance) { item.el.classList.remove(entrance); void item.el.offsetWidth; item.el.classList.add(entrance); }
      }
      if (on) animateText(txt, item, rel);
    });

    if (edit.music.track && music.src && !soloing) {
      const m = edit.music;
      let f = 1;
      if (m.fade_in && rel < m.fade_in) f = rel / m.fade_in;
      if (m.fade_out && rel > L - m.fade_out) f = Math.min(f, (L - rel) / m.fade_out);
      music.volume = clamp(m.volume * f * (1 - dark), 0, 1);
      if (!vid.paused && !outside) { if (music.paused) { syncMusic(true); music.play().catch(() => {}); } else syncMusic(false); }
      else if (!music.paused) music.pause();
    }

    $("playhead").style.left = laneVideo.offsetLeft + t * pps() + "px";
    $("clock").textContent = `${fmt(rel)} / ${fmt(L)}`;
    if (!once) requestAnimationFrame(() => tick(false));
  }

  // ------------------------------------------------------------- transport
  const playBtn = $("play");
  function togglePlay() {
    soloing = false;
    if (vid.paused) {
      if (vid.currentTime < edit.trim.start || vid.currentTime >= edit.trim.end - 0.05) vid.currentTime = edit.trim.start;
      vid.play();
    } else vid.pause();
  }
  playBtn.addEventListener("click", togglePlay);
  vid.addEventListener("play", () => { playBtn.textContent = "❚❚"; });
  vid.addEventListener("pause", () => { playBtn.textContent = "▶"; if (!soloing) music.pause(); });
  stage.addEventListener("click", (e) => { if (e.target === vid || e.target === shade || e.target === layer) togglePlay(); });
  document.addEventListener("keydown", (e) => {
    if (e.code === "Space" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) { e.preventDefault(); togglePlay(); }
  });

  // ------------------------------------------------------------- save
  const save = $("save");
  if (save) save.addEventListener("click", async () => {
    save.disabled = true;
    save.textContent = "Saving…";
    try {
      const resp = await fetch(`/api/clips/${D.id}/edit`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(edit),
      });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(body.detail || `HTTP ${resp.status}`);
      dirty = false;
      location.href = body.redirect;
    } catch (err) {
      alert("Couldn't save: " + err.message);
      save.disabled = false;
      save.textContent = "Save & render";
    }
  });

  // ------------------------------------------------------------- start
  vid.addEventListener("loadedmetadata", () => { vid.currentTime = edit.trim.start; redraw(); drawRuler(); });
  window.addEventListener("resize", () => { drawRuler(); redraw(); });
  loadMusic();
  fitFades();
  syncAll();
  select(-1);
  drawRuler();
  redraw();
  tick(false);
})();
