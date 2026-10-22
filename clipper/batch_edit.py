"""Batch edit: answer a few questions once, apply them to every selected clip.

The answers (the "spec") are saved on each clip (info["batch_edit"]) and turned into that
clip's normal edit when its video is rendered (worker.render_edit), so captions are
transcribed in the background and the result can still be opened in the editor afterwards.
"""

from pathlib import Path

from . import captions, config, media, project as P, render

# caption looks: what the "Captions" question offers -> the text fields they set
CAPTIONS = {
    "none": ("No captions", None),
    "highlight": ("Karaoke: the spoken word lights up",
                  {"style": "caption", "animation": "highlight", "size": 84}),
    "words": ("Word by word: each word pops in as it's said",
              {"style": "caption", "animation": "words", "size": 90}),
    "classic": ("Classic: white with a black outline",
                {"style": "classic", "animation": "fade", "size": 76}),
    "yellow": ("Yellow: bold yellow lines",
               {"style": "yellow", "animation": "pop", "size": 80}),
    "box": ("Boxed: text on a dark box",
            {"style": "pill", "animation": "fade", "size": 70}),
    "minimal": ("Minimal: small clean text",
                {"style": "minimal", "animation": "fade", "size": 60}),
}
CAPTION_POSITIONS = ("bottom", "middle", "top")
TITLE_SOURCES = {"none": "No title", "text": "The same title on every clip", "ai": "Each clip's own title"}
TITLE_STYLES = {
    "sticker": "White card", "highlighter": "Yellow marker", "red": "Red banner", "blue": "Blue banner",
    "pink": "Pink banner", "classic": "Bold outline", "yellow": "Yellow outline", "impact": "Tall impact",
    "comic": "Comic", "handwritten": "Handwritten", "cinema": "Cinema", "retro": "Retro pink",
    "neon": "Neon", "minimal": "Minimal", "pill": "Dark pill", "box": "Dark card",
}
TITLE_SIZES = {"impact": 96, "comic": 88, "handwritten": 84, "cinema": 110, "retro": 80}  # others: 64
TITLE_POSITIONS = ("top", "middle")
WATERMARK_POSITIONS = tuple(media.WATERMARK_POS)
MUSIC_CHOICES = {"keep": "Keep the clip's suggested music", "none": "No music"}
# whole-picture effect: one for every clip, or "mix" = each clip gets a different one in turn
MIX = "mix"
MIX_EFFECTS = ("grain", "glow", "vhs", "sharpen")
VIDEO_FX_CHOICES = {**P.VIDEO_FX, MIX: "Mix: a different one on each clip"}


def options() -> dict:
    return {
        "captions": {k: v[0] for k, v in CAPTIONS.items()},
        "captions_look": {k: v[1] for k, v in CAPTIONS.items() if v[1]},
        "caption_positions": CAPTION_POSITIONS,
        "title_sources": TITLE_SOURCES, "title_styles": TITLE_STYLES, "title_sizes": TITLE_SIZES, "title_positions": TITLE_POSITIONS,
        "filters": {k: label for k, (label, _) in render.FILTERS.items()},
        "video_fx": VIDEO_FX_CHOICES,
        "zooms": render.ZOOMS, "qualities": {k: v[0] for k, v in render.QUALITY.items()},
        "watermark_positions": WATERMARK_POSITIONS, "music": MUSIC_CHOICES,
    }


def _pick(value, allowed, default):
    return value if value in allowed else default


def _num(value, low, high, default):
    try:
        return min(high, max(low, float(value)))
    except (TypeError, ValueError):
        return default


def clean_spec(form) -> dict:
    """The user's answers from the batch-edit form (anything missing or odd falls back to a default)."""
    get = form.get
    spec = {
        "captions": _pick(get("captions"), CAPTIONS, "none"),
        "gemini_fix": bool(get("gemini_fix")),
        "ai_intro": bool(get("ai_intro")),                       # a spoken hook over the first seconds
        "intro_text": str(get("intro_text") or "").strip()[:120],  # empty = Gemini writes one per clip
        "caption_position": _pick(get("caption_position"), CAPTION_POSITIONS, "bottom"),
        "title_source": _pick(get("title_source"), TITLE_SOURCES, "none"),
        "title_text": str(get("title_text") or "").strip()[:120],
        "title_pov": bool(get("title_pov")),
        "title_style": _pick(get("title_style"), TITLE_STYLES, "sticker"),
        "title_position": _pick(get("title_position"), TITLE_POSITIONS, "top"),
        "title_seconds": _num(get("title_seconds"), 0, 600, 4.0),  # 0 = the whole clip
        "filter": _pick(get("filter"), render.FILTERS, "none"),
        "video_fx": _pick(get("video_fx"), VIDEO_FX_CHOICES, "none"),
        "vignette": round(_num(get("vignette"), 0.0, 1.0, 0.0), 2),   # 0 = none, 1 = darkest edges
        "zoom": _pick(get("zoom"), render.ZOOMS, "none"),
        "fade": bool(get("fade")),
        "music": _pick(get("music"), MUSIC_CHOICES, "keep"),
        "quality": _pick(get("quality"), render.QUALITY, "standard"),
        "watermark_text": str(get("watermark_text") or "").strip()[:40],
        "watermark_position": _pick(get("watermark_position"), WATERMARK_POSITIONS, "bottom-right"),
        "watermark_opacity": _num(get("watermark_opacity"), 0.05, 1.0, 0.35),
    }
    if spec["title_source"] == "text" and not spec["title_text"]:
        spec["title_source"] = "none"
    return spec


def spec_for_clip(spec: dict, index: int) -> dict:
    """The answers for the `index`-th clip of a batch: "mix" becomes one concrete effect, rotating
    through MIX_EFFECTS so neighbouring clips differ. Anything else is returned as it is."""
    if spec.get("video_fx") != MIX:
        return spec
    return {**spec, "video_fx": MIX_EFFECTS[index % len(MIX_EFFECTS)]}


def wants_project(spec: dict) -> bool:
    """Effects and the vignette are drawn by the timeline editor's renderer, so a batch that uses
    them (or an AI intro) is rendered as an editor project."""
    return bool(spec.get("ai_intro") or spec.get("vignette", 0) >= 0.02
                or spec.get("video_fx", "none") not in ("none", MIX))


def title_for(spec: dict, job: dict) -> str:
    if spec["title_source"] == "text":
        text = spec["title_text"]
    elif spec["title_source"] == "ai":
        text = str((job.get("info") or {}).get("title") or "").strip()
    else:
        return ""
    if text and spec["title_pov"] and not text.lower().startswith("pov"):
        text = "POV: " + text
    return text[:200]


INTRO_PAD = 0.25  # seconds of room after the spoken intro before the clip's own words come back


def build_edit(spec: dict, job: dict, job_dir: Path, cfg: dict, intro: dict = None) -> dict:
    """The clip's edit for these answers: whole clip, captions and title as asked. `intro` (from
    intro.make_intro) adds the hook as on-screen text and keeps captions out of its way."""
    base = job_dir / "transformed.mp4"
    duration = media.duration(base)
    tracks = render.music_names()
    track = job.get("music_track") if spec["music"] == "keep" and job.get("music_track") in tracks else None
    edit = render.default_edit(duration, track, job.get("music_volume"),
                               ducking=bool(cfg["music"]["ducking"]))
    edit.update(filter=spec["filter"], zoom=spec["zoom"], quality=spec["quality"],
                fade_in=0.4 if spec["fade"] else 0.0, fade_out=0.6 if spec["fade"] else 0.0)

    texts = []
    reference = (job.get("clip") or {}).get("spoken_words") or "" if spec.get("gemini_fix") else ""
    look = CAPTIONS[spec["captions"]][1]
    if look and media.has_audio(base):
        try:
            texts += [{**t, **look, "position": spec["caption_position"]}
                      for t in captions.captions_for(job_dir, 0.0, duration, reference, cfg)]
        except captions.CaptionError:
            pass  # a clip with nothing to caption still gets its other edits
    title = title_for(spec, job)
    if title:
        end = min(duration, spec["title_seconds"]) if spec["title_seconds"] else duration
        texts.append({"text": title, "start": 0.0, "end": max(end, 1.0), "style": spec["title_style"],
                      "position": spec["title_position"], "size": TITLE_SIZES.get(spec["title_style"], 64), "animation": "pop"})
    if intro:
        until = intro["duration"] + INTRO_PAD
        kept = []
        for t in texts:
            if t.get("auto") and t["start"] < until:   # the narrator is speaking: no captions yet
                if t["end"] - until < 0.3:
                    continue
                t = {**t, "start": round(until, 3), "words": None}
            kept.append(t)
        texts = kept
        place = "top" if spec["title_position"] == "middle" else "middle"   # not on top of the title
        texts.append({"text": intro["text"], "start": 0.0, "end": round(until, 3), "style": spec["title_style"],
                      "position": place, "size": TITLE_SIZES.get(spec["title_style"], 64), "animation": "pop"})
    edit["texts"] = texts
    return render.clean_edit(edit, duration, tracks)


def build_project(spec: dict, job: dict, job_dir: Path, cfg: dict) -> tuple:
    """(editor project, intro info or None) for a batch with an AI intro, an effect or a vignette. A
    project, not a plain edit, so the voice, the hook, the effect and everything else open in the
    editor for you to change or add images to."""
    from . import editor2, intro as intro_mod

    made = intro_mod.make_intro(job, job_dir, cfg, spec.get("intro_text", "")) if spec.get("ai_intro") else None
    v1 = build_edit(spec, job, job_dir, cfg, intro=made)
    known = editor2.known_assets(job_dir)
    track = v1["music"]["track"]
    music_asset = (f"music:{track}", known[f"music:{track}"]) if track and f"music:{track}" in known else None
    proj = P.from_v1(v1, known["clip"], music_asset)
    proj["look"]["effect"] = spec.get("video_fx") if spec.get("video_fx") in P.VIDEO_FX else "none"
    proj["look"]["vignette"] = spec.get("vignette", 0.0)
    if not made:
        return editor2.clean(proj, job_dir), None

    aid = f"upload:{made['file']}"
    proj["assets"][aid] = known[aid]
    proj["tracks"].append({"id": "intro", "kind": "audio", "name": "Intro voice", "items": [
        {"asset": aid, "start": 0, "dur": made["duration"], "in": 0, "volume": 1.3, "fade_in": 0, "fade_out": 0.05,
         "music": False, "duck": False}]})

    # the clip's own sound sits low under the narrator, then comes back: split the clip there
    main = proj["tracks"][0]["items"][0]
    length = main["out"] - main["in"]
    cut = round(min(made["duration"] + INTRO_PAD, length - 1.0), 3)
    if cut > 0.5:
        zoom = (main.get("kf") or {}).get("zoom")
        mid = P.at(zoom, cut, 1.0) if zoom else None
        first = {**main, "id": "c1", "out": round(main["in"] + cut, 3), "volume": round(main["volume"] * 0.18, 3)}
        second = {**main, "id": "c2", "in": round(main["in"] + cut, 3)}
        if zoom:
            first["kf"] = {**main["kf"], "zoom": [[0, zoom[0][1]], [cut, mid]]}
            second["kf"] = {**main["kf"], "zoom": [[0, mid], [round(length - cut, 3), zoom[-1][1]]]}
        proj["tracks"][0]["items"] = [first, second]
    return editor2.clean(proj, job_dir), made


def watermark_cfg(spec: dict, cfg: dict) -> dict:
    """cfg with the watermark replaced by this batch's answer (empty text = none on these clips)."""
    text = spec.get("watermark_text") or ""
    return {**cfg, "watermark": {**cfg["watermark"], "enabled": bool(text), "text": text,
                                 "position": spec.get("watermark_position", "bottom-right"),
                                 "opacity": spec.get("watermark_opacity", 0.35)}}


# ------------------------------------------------------------------ saved looks

def presets() -> dict:
    """{name: spec} saved from the form (re-cleaned, so an old preset never breaks the page)."""
    saved = config.ui_get().get("batch_presets") or {}
    return {name: clean_spec(spec) for name, spec in saved.items() if isinstance(spec, dict)}


def last_used() -> dict:
    return clean_spec(config.ui_get().get("batch_last") or {})


def save_preset(name: str, spec: dict) -> str:
    name = " ".join(str(name or "").split())[:40]
    if name:
        config.ui_set(batch_presets={**presets(), name: spec})
    return name


def delete_preset(name: str) -> None:
    kept = {k: v for k, v in presets().items() if k != name}
    config.ui_set(batch_presets=kept)
