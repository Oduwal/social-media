"""Batch edit: answer a few questions once, apply them to every selected clip.

The answers (the "spec") are saved on each clip (info["batch_edit"]) and turned into that
clip's normal edit when its video is rendered (worker.render_edit), so captions are
transcribed in the background and the result can still be opened in the editor afterwards.
"""

from pathlib import Path

from . import captions, config, media, render

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


def options() -> dict:
    return {
        "captions": {k: v[0] for k, v in CAPTIONS.items()},
        "captions_look": {k: v[1] for k, v in CAPTIONS.items() if v[1]},
        "caption_positions": CAPTION_POSITIONS,
        "title_sources": TITLE_SOURCES, "title_styles": TITLE_STYLES, "title_sizes": TITLE_SIZES, "title_positions": TITLE_POSITIONS,
        "filters": {k: label for k, (label, _) in render.FILTERS.items()},
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
        "caption_position": _pick(get("caption_position"), CAPTION_POSITIONS, "bottom"),
        "title_source": _pick(get("title_source"), TITLE_SOURCES, "none"),
        "title_text": str(get("title_text") or "").strip()[:120],
        "title_pov": bool(get("title_pov")),
        "title_style": _pick(get("title_style"), TITLE_STYLES, "sticker"),
        "title_position": _pick(get("title_position"), TITLE_POSITIONS, "top"),
        "title_seconds": _num(get("title_seconds"), 0, 600, 4.0),  # 0 = the whole clip
        "filter": _pick(get("filter"), render.FILTERS, "none"),
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


def build_edit(spec: dict, job: dict, job_dir: Path, cfg: dict) -> dict:
    """The clip's edit for these answers: whole clip, captions and title as asked."""
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
                      for t in captions.captions_for(job_dir, 0.0, duration, reference)]
        except captions.CaptionError:
            pass  # a clip with nothing to caption still gets its other edits
    title = title_for(spec, job)
    if title:
        end = min(duration, spec["title_seconds"]) if spec["title_seconds"] else duration
        texts.append({"text": title, "start": 0.0, "end": max(end, 1.0), "style": spec["title_style"],
                      "position": spec["title_position"], "size": TITLE_SIZES.get(spec["title_style"], 64), "animation": "pop"})
    edit["texts"] = texts
    return render.clean_edit(edit, duration, tracks)


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
