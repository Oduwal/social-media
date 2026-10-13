"""Render a clip's edit (from the timeline editor) to final.mp4 with ffmpeg.

An edit is plain JSON, stored per clip:
{
  "trim":   {"start": 0.0, "end": 21.3},          # seconds of transformed.mp4 to keep
  "filter": "none",                               # colour look, see FILTERS
  "zoom":   "none",                               # slow camera move, see ZOOMS
  "fade_in": 0.0, "fade_out": 0.0,                # seconds, picture + sound
  "voice_volume": 1.0,
  "music": {"track": "beat.mp3" | null, "volume": 0.18, "offset": 0.0,
            "ducking": true, "fade_in": 0.5, "fade_out": 1.0},
  "texts": [{"text": "...", "start": 0.0, "end": 3.0, "style": "classic",
             "position": "top", "size": 72, "color": "#FFFFFF", "animation": "pop",
             "accent": "#FFE600",                  # highlight colour (animation "highlight")
             "words": [[0.0, 0.4], ...] | null,     # when each word is spoken (auto captions)
             "auto": false}]                       # made by Auto captions (replaced on a re-run)
}
Text becomes an .ass subtitle file (libass does the styling); everything else is
one -filter_complex. Looks use curves/eq/hue only: colorbalance, colortemperature
and vignette are 2-6x slower at 1080x1920.
"""

import copy
import re
import shutil
from pathlib import Path

from . import config, media

W, H, FPS = 1080, 1920, 30

FILTERS = {
    "none": ("None", ""),
    "warm": ("Warm", "curves=r='0/0 0.5/0.56 1/1':b='0/0 0.5/0.44 1/0.95'"),
    "cool": ("Cool", "curves=r='0/0 0.5/0.45 1/0.96':b='0/0 0.5/0.56 1/1'"),
    "vivid": ("Vivid", "eq=saturation=1.35:contrast=1.08"),
    "bw": ("Black & white", "hue=s=0,eq=contrast=1.1"),
    "vintage": ("Vintage", "curves=preset=vintage"),
    "cinematic": ("Cinematic", "curves=r='0/0.03 0.5/0.53 1/0.97':g='0/0 0.5/0.5 1/0.95':"
                                "b='0/0.08 0.5/0.46 1/0.9',eq=contrast=1.08:saturation=0.9"),
    "faded": ("Faded", "curves=all='0/0.08 1/0.92',eq=saturation=0.8"),
    "bright": ("Bright", "eq=brightness=0.05:contrast=1.05:saturation=1.1"),
}
ZOOMS = {"none": "None", "slow_in": "Slow zoom in", "slow_out": "Slow zoom out"}
# render quality: (label, x264 preset, crf (lower = sharper, bigger file), audio kbps)
QUALITY = {
    "draft": ("Draft (fast, small file)", "veryfast", 26, 128),
    "standard": ("Standard", "veryfast", 20, 192),
    "high": ("High (slower, sharper)", "slow", 16, 256),
    "max": ("Max (slowest, biggest file)", "slower", 13, 320),
}
ZOOM_AMOUNT = 0.12

# name: (font, colour, outline colour, outline px, border style 1=outline 3=box, shadow, bold)
TEXT_STYLES = {
    "classic": ("Arial Black", "#FFFFFF", "#000000", 6, 1, 2, True),
    "yellow": ("Arial Black", "#FFE600", "#000000", 6, 1, 2, True),
    "box": ("Arial", "#FFFFFF", "#000000", 14, 3, 0, True),
    "minimal": ("Segoe UI Semibold", "#FFFFFF", "#000000", 0, 1, 3, False),
    "neon": ("Arial Black", "#FFFFFF", "#FF2BD6", 5, 1, 0, True),
    "caption": ("Arial Black", "#FFFFFF", "#000000", 9, 1, 3, True),   # heavy outline, for captions
    "pill": ("Arial Black", "#FFFFFF", "#111111", 18, 3, 0, True),     # white on a dark pill
    # more title designs (cards in colour, and fonts shipped in static/fonts)
    "sticker": ("Arial Black", "#111111", "#FFFFFF", 16, 3, 0, True),      # black on a white card
    "highlighter": ("Arial Black", "#111111", "#FFE600", 16, 3, 0, True),  # black on yellow marker
    "red": ("Arial Black", "#FFFFFF", "#E5173F", 16, 3, 0, True),
    "blue": ("Arial Black", "#FFFFFF", "#1E6BFF", 16, 3, 0, True),
    "pink": ("Arial Black", "#FFFFFF", "#FF2BD6", 16, 3, 0, True),
    "impact": ("Anton", "#FFFFFF", "#000000", 8, 1, 3, False),             # tall, heavy
    "comic": ("Bangers", "#FFE600", "#000000", 8, 1, 3, False),
    "handwritten": ("Permanent Marker", "#FFFFFF", "#000000", 6, 1, 2, False),
    "cinema": ("Bebas Neue", "#FFFFFF", "#000000", 0, 1, 4, False),        # clean, tall, no outline
    "retro": ("Luckiest Guy", "#FFFFFF", "#FF2BD6", 7, 1, 3, False),
}
TRANSLUCENT_CARDS = {"box", "pill"}  # the other cards are solid
FONT_DIR = Path(__file__).resolve().parent / "static" / "fonts"
POSITIONS = {"top": (8, 260), "middle": (5, 0), "bottom": (2, 420)}  # ASS alignment, margin from edge
# Short-form caption effects (in the spirit of HyperFrames' caption styles), done by libass so
# a render still takes seconds: words = each word appears as it's spoken, highlight = the word
# being spoken lights up in the accent colour, typewriter = letters type on.
ANIMATIONS = ("none", "fade", "pop", "bounce", "slide", "typewriter", "words", "highlight")
WORD_ANIMATIONS = ("words", "highlight")
MAX_TEXTS = 150  # auto captions make one text per 1-3 words


class EditError(ValueError):
    pass


# ----------------------------------------------------------------- edit model

def default_edit(duration: float, music_track: str = None, music_volume: float = None,
                 ducking: bool = True) -> dict:
    return {
        "trim": {"start": 0.0, "end": round(duration, 3)},
        "filter": "none", "zoom": "none", "fade_in": 0.0, "fade_out": 0.0, "voice_volume": 1.0,
        "quality": "standard",
        "music": {"track": music_track, "volume": 0.18 if music_volume is None else music_volume,
                  "offset": 0.0, "ducking": ducking, "fade_in": 0.5, "fade_out": 1.0},
        "texts": [],
    }


def _num(value, low, high, default):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    if value != value:  # NaN
        return default
    return round(min(high, max(low, value)), 3)


def _color(value, default):
    value = str(value or "").strip()
    return value.upper() if re.fullmatch(r"#[0-9a-fA-F]{6}", value) else default


def clean_edit(raw: dict, duration: float, tracks: set) -> dict:
    """Validate an edit coming from the browser: clamp numbers, whitelist choices,
    and refuse anything that would make an invalid or empty render."""
    if not isinstance(raw, dict):
        raise EditError("Edit must be an object")
    out = default_edit(duration)
    trim = raw.get("trim") or {}
    start = _num(trim.get("start"), 0, duration, 0.0)
    end = _num(trim.get("end"), 0, duration, duration)
    if end - start < 1.0:
        raise EditError("The clip must be at least 1 second long")
    out["trim"] = {"start": start, "end": end}
    length = end - start

    out["filter"] = raw.get("filter") if raw.get("filter") in FILTERS else "none"
    out["zoom"] = raw.get("zoom") if raw.get("zoom") in ZOOMS else "none"
    out["fade_in"] = _num(raw.get("fade_in"), 0, min(3, length / 2), 0.0)
    out["fade_out"] = _num(raw.get("fade_out"), 0, min(3, length / 2), 0.0)
    out["voice_volume"] = _num(raw.get("voice_volume"), 0, 2, 1.0)
    out["quality"] = raw.get("quality") if raw.get("quality") in QUALITY else "standard"

    music = raw.get("music") or {}
    track = music.get("track") or None
    if track is not None and track not in tracks:
        raise EditError(f"Unknown music track: {track}")
    out["music"] = {
        "track": track,
        "volume": _num(music.get("volume"), 0, 1, 0.18),
        "offset": _num(music.get("offset"), 0, 3600, 0.0),
        "ducking": bool(music.get("ducking", True)),
        "fade_in": _num(music.get("fade_in"), 0, min(5, length / 2), 0.5),
        "fade_out": _num(music.get("fade_out"), 0, min(5, length / 2), 1.0),
    }

    texts = []
    for item in (raw.get("texts") or [])[:MAX_TEXTS]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()[:200]
        if not text:
            continue
        raw_start = _num(item.get("start"), -3600, 3600, 0.0)
        raw_end = _num(item.get("end"), -3600, 3600, length)
        if raw_end <= 0 or raw_start >= length:
            continue  # trimmed away entirely: drop it rather than squash it into a flash
        t_start = _num(raw_start, 0, length, 0.0)
        t_end = _num(raw_end, 0, length, length)
        if t_end - t_start < 0.2:
            continue
        style = item.get("style") if item.get("style") in TEXT_STYLES else "classic"
        texts.append({
            "text": text, "start": t_start, "end": t_end, "style": style,
            "position": item.get("position") if item.get("position") in POSITIONS else "top",
            "size": int(_num(item.get("size"), 24, 160, 72)),
            "color": _color(item.get("color"), TEXT_STYLES[style][1]),
            "animation": item.get("animation") if item.get("animation") in ANIMATIONS else "pop",
            "accent": _color(item.get("accent"), "#FFE600"),
            "words": _clean_words(item.get("words"), text, t_start, t_end),
            "auto": bool(item.get("auto")),
        })
    out["texts"] = texts
    return out


def _clean_words(words, text: str, start: float, end: float):
    """[[start, end], ...] per word of `text` (same count), inside the text's own window,
    in order; None when they don't fit the text any more (e.g. it was retyped)."""
    if not isinstance(words, list) or len(words) != len(text.split()):
        return None
    out = []
    for pair in words:
        try:
            a, b = float(pair[0]), float(pair[1])
        except (TypeError, ValueError, IndexError):
            return None
        a = min(max(a, start), end)
        b = min(max(b, a), end)
        if out and a < out[-1][0]:
            return None
        out.append([round(a, 3), round(b, 3)])
    return out


def word_times(item: dict) -> list:
    """When each word of a text starts and ends: its own timings, or spread evenly."""
    words = item["text"].split()
    if item.get("words") and len(item["words"]) == len(words):
        return [tuple(w) for w in item["words"]]
    step = (item["end"] - item["start"]) / max(1, len(words))
    return [(item["start"] + i * step, item["start"] + (i + 1) * step) for i in range(len(words))]


def fit_edit(edit: dict, duration: float, tracks: set) -> dict:
    """Make a saved edit fit what exists now: the clip may have been re-transformed to a
    slightly different length, or its music file removed from music/ since it was saved."""
    edit = {**edit, "music": {**(edit.get("music") or {})}, "trim": {**(edit.get("trim") or {})}}
    if edit["music"].get("track") not in tracks:
        edit["music"]["track"] = None
    start = float(edit["trim"].get("start", 0.0))
    end = float(edit["trim"].get("end", duration))
    edit["trim"] = {"start": min(start, max(0.0, duration - 1)), "end": min(end, duration)}
    if edit["trim"]["end"] - edit["trim"]["start"] < 1:
        edit["trim"] = {"start": 0.0, "end": duration}
    return clean_edit(edit, duration, tracks)


# ------------------------------------------------------------------- captions

def _ass_color(hex_color: str, alpha: int = 0) -> str:
    """#RRGGBB -> &HAABBGGRR (ASS is blue-green-red, alpha 00 = opaque)."""
    r, g, b = hex_color[1:3], hex_color[3:5], hex_color[5:7]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _ass_time(seconds: float) -> str:
    cs = int(round(seconds * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _ass_text(text: str) -> str:
    # braces open override blocks and backslashes start tags in ASS
    text = text.replace("\\", "/").replace("{", "(").replace("}", ")")
    return re.sub(r"\r?\n", r"\\N", text)


def _ass_bgr(hex_color: str) -> str:
    return f"&H{hex_color[5:7]}{hex_color[3:5]}{hex_color[1:3]}&"


def _anchor(position: str) -> tuple:
    """Where an aligned line sits, for a move tag: (x, y)."""
    align, margin = POSITIONS[position]
    return W // 2, {8: margin, 2: H - margin, 5: H // 2}[align]


def _cs(seconds: float) -> int:
    return max(0, int(round(seconds * 100)))


def _events(layer: int, item: dict) -> list:
    """The Dialogue lines for one text (usually one; 'highlight' makes one per word)."""
    align, margin = POSITIONS[item["position"]]
    base = [f"\\an{align}", f"\\fs{item['size']}", f"\\1c{_ass_bgr(item['color'])}"]
    if item["style"] == "neon":
        base.append("\\blur4")
    anim, start, end = item["animation"], item["start"], item["end"]

    def line(a, b, tags, body):
        return (f"Dialogue: {layer},{_ass_time(a)},{_ass_time(b)},{item['style']},,0,0,{margin},,"
                f"{{{''.join(base + tags)}}}{body}")

    if anim == "highlight":  # the whole line, with the word being spoken in the accent colour
        words, times = item["text"].split(), word_times(item)
        out = []
        for i, (a, b) in enumerate(times):
            b = times[i + 1][0] if i + 1 < len(times) else end  # no gaps: the line never blinks
            if b - a < 0.01:
                continue
            body = " ".join((f"{{\\1c{_ass_bgr(item['accent'])}\\fscx108\\fscy108}}{_ass_text(w)}"
                             f"{{\\1c{_ass_bgr(item['color'])}\\fscx100\\fscy100}}") if j == i else _ass_text(w)
                            for j, w in enumerate(words))
            out.append(line(a if i else start, b, [], body))
        return out
    if anim in ("words", "typewriter"):
        # One line per reveal: what's shown so far, then the rest fully transparent (fill,
        # outline and shadow), so the line keeps its place and nothing shows as a box.
        text = item["text"]
        if anim == "words":
            words = text.split()
            reveals = [a for a, _ in word_times(item)]
            parts = [(" ".join(words[:n]), " ".join(words[n:])) for n in range(1, len(words) + 1)]
        else:  # letters type on over the first ~40% (at most 1.2 s)
            step = min(1.2, (end - start) * 0.4) / max(1, len(text))
            reveals = [start + i * step for i in range(len(text))]
            parts = [(text[:n], text[n:]) for n in range(1, len(text) + 1)]
        out = []
        for n, (shown, rest) in enumerate(parts):
            a = start if n == 0 else reveals[n]
            b = reveals[n + 1] if n + 1 < len(reveals) else end
            if b - a < 0.01:
                continue
            sep = " " if anim == "words" and rest else ""
            body = _ass_text(shown) + (f"{sep}{{\\alpha&HFF&}}{_ass_text(rest)}" if rest else "")
            out.append(line(a, b, [], body))
        return out
    tags = []
    if anim == "fade":
        tags.append("\\fad(250,250)")
    elif anim == "pop":
        tags.append("\\fscx40\\fscy40\\t(0,160,\\fscx108\\fscy108)\\t(160,240,\\fscx100\\fscy100)\\fad(0,150)")
    elif anim == "bounce":
        tags.append("\\fscx30\\fscy30\\t(0,120,\\fscx118\\fscy118)\\t(120,200,\\fscx92\\fscy92)"
                    "\\t(200,270,\\fscx100\\fscy100)\\fad(0,150)")
    elif anim == "slide":
        x, y = _anchor(item["position"])
        tags.append(f"\\move({x},{y + 140},{x},{y},0,260)\\fad(200,150)")
    return [line(start, end, tags, _ass_text(item["text"]))]


def build_ass(texts: list) -> str:
    lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
             "WrapStyle: 0", "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
             "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
             "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"]
    for name, (font, colour, outline, width, border, shadow, bold) in TEXT_STYLES.items():
        outline_ass = _ass_color(outline, 0x40 if name in TRANSLUCENT_CARDS else 0)
        lines.append(f"Style: {name},{font},72,{_ass_color(colour)},&H000000FF,{outline_ass},"
                     f"&H64000000,{-1 if bold else 0},0,0,0,100,100,0,0,{border},{width},{shadow},"
                     f"2,70,70,200,1")
    lines += ["", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for layer, item in enumerate(texts):
        lines += _events(layer, item)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------- render

def build_command(edit: dict, base: Path, out: Path, music_path: Path = None,
                  ass_name: str = None, base_has_audio: bool = True) -> list:
    """The ffmpeg command line. Run it with cwd = the folder holding the .ass file,
    so the subtitles filter gets a plain relative path (Windows drive colons in a
    filter argument are an escaping minefield)."""
    start, end = edit["trim"]["start"], edit["trim"]["end"]
    length = end - start
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(base)]
    inputs = 1
    voice_in = "0:a"
    if not base_has_audio:
        cmd += ["-f", "lavfi", "-t", f"{length:.3f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
        voice_in = f"{inputs}:a"
        inputs += 1
    music_in = None
    if music_path is not None:
        cmd += ["-stream_loop", "-1", "-ss", f"{edit['music']['offset']:.3f}", "-i", str(music_path)]
        music_in = f"{inputs}:a"
        inputs += 1

    # picture
    video = [f"scale={W}:{H}:force_original_aspect_ratio=decrease",
             f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black", "setsar=1", f"fps={FPS}"]
    if edit["zoom"] != "none":
        # zoompan re-centres on every frame (a per-frame scale + crop drifts to a corner)
        zoom = f"1+{ZOOM_AMOUNT}*it/{length:.3f}" if edit["zoom"] == "slow_in" \
            else f"1+{ZOOM_AMOUNT}-{ZOOM_AMOUNT}*it/{length:.3f}"
        video.append(f"zoompan=z='{zoom}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s={W}x{H}:fps={FPS}")
    if FILTERS[edit["filter"]][1]:
        video.append(FILTERS[edit["filter"]][1])
    if ass_name:
        video.append(f"subtitles={ass_name}:fontsdir=fonts")
    if edit["fade_in"]:
        video.append(f"fade=t=in:st=0:d={edit['fade_in']}")
    if edit["fade_out"]:
        video.append(f"fade=t=out:st={length - edit['fade_out']:.3f}:d={edit['fade_out']}")
    video.append("format=yuv420p")
    graph = [f"[0:v]{','.join(video)}[v]",
             f"[{voice_in}]aresample=44100,aformat=channel_layouts=stereo,volume={edit['voice_volume']}[voice0]"]

    # sound
    if music_in:
        m = edit["music"]
        music = ["aresample=44100", "aformat=channel_layouts=stereo", f"volume={m['volume']}"]
        if m["fade_in"]:
            music.append(f"afade=t=in:st=0:d={m['fade_in']}")
        if m["fade_out"]:
            music.append(f"afade=t=out:st={max(0.0, length - m['fade_out']):.3f}:d={m['fade_out']}")
        graph.append(f"[{music_in}]{','.join(music)}[m]")
        if m["ducking"]:
            graph += ["[voice0]asplit=2[voice][sc]",
                      "[m][sc]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[duck]",
                      "[voice][duck]amix=inputs=2:duration=first:normalize=0[mixed]"]
        else:
            graph.append("[voice0][m]amix=inputs=2:duration=first:normalize=0[mixed]")
        mixed = "[mixed]"
    else:
        mixed = "[voice0]"
    tail = []
    if edit["fade_in"]:
        tail.append(f"afade=t=in:st=0:d={edit['fade_in']}")
    if edit["fade_out"]:
        tail.append(f"afade=t=out:st={length - edit['fade_out']:.3f}:d={edit['fade_out']}")
    # loudnorm outputs NaN on short stretches of pure silence, which makes the AAC
    # encoder abort: only normalise when something is audible and long enough.
    audible = (base_has_audio and edit["voice_volume"] > 0) or bool(music_in and edit["music"]["volume"] > 0)
    if not audible:
        tail.append("anull")
    elif length < 3.5:
        tail.append("alimiter=limit=0.9")
    else:
        tail.append("loudnorm=I=-14:TP=-1.5:LRA=11")
    graph.append(f"{mixed}{','.join(tail)}[a]")

    quality = QUALITY[edit.get("quality") if edit.get("quality") in QUALITY else "standard"]
    cmd += ["-filter_complex", ";".join(graph), "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", quality[1], "-crf", str(quality[2]),
            "-c:a", "aac", "-b:a", f"{quality[3]}k", "-ar", "44100",
            "-t", f"{length:.3f}", "-movflags", "+faststart", str(out)]
    return cmd


def render(edit: dict, job_dir: Path, base_name: str = "transformed.mp4", out_name: str = "final.mp4",
           music_dir: Path = None) -> Path:
    """Render job_dir/base_name through the edit into job_dir/out_name. Writes to a
    temp file first, so a failed render never leaves a broken final.mp4 behind."""
    from . import music as music_lib

    edit = copy.deepcopy(edit)
    base = job_dir / base_name
    track = edit["music"].get("track")
    music_path = music_lib.track_path(track, music_dir) if track else None
    ass_name = None
    if edit["texts"]:
        ass_name = "edit.ass"
        (job_dir / ass_name).write_text(build_ass(edit["texts"]), encoding="utf-8")
        fonts = job_dir / "fonts"  # subtitles= wants a plain relative fontsdir
        fonts.mkdir(exist_ok=True)
        for font in FONT_DIR.glob("*.ttf"):
            if not (fonts / font.name).exists():
                shutil.copyfile(font, fonts / font.name)
    tmp = job_dir / f"render_tmp_{out_name}"
    cmd = build_command(edit, base.resolve(), tmp.name, music_path, ass_name,
                        base_has_audio=media.has_audio(base))
    media.run(cmd, cwd=job_dir)
    media.replace(tmp, job_dir / out_name)
    return job_dir / out_name


def preview_filters() -> dict:
    """CSS approximations of each look, for the editor's live preview."""
    return {
        "none": "none", "warm": "sepia(0.25) saturate(1.15) hue-rotate(-8deg)",
        "cool": "saturate(1.05) hue-rotate(12deg) brightness(1.02)",
        "vivid": "saturate(1.35) contrast(1.08)", "bw": "grayscale(1) contrast(1.1)",
        "vintage": "sepia(0.45) contrast(0.9) brightness(1.05) saturate(0.85)",
        "cinematic": "contrast(1.08) saturate(0.9) sepia(0.12) hue-rotate(-5deg)",
        "faded": "contrast(0.84) brightness(1.06) saturate(0.8)",
        "bright": "brightness(1.08) contrast(1.05) saturate(1.1)",
    }


def options() -> dict:
    """Everything the editor UI offers, in one place."""
    return {
        "filters": {k: label for k, (label, _) in FILTERS.items()},
        "css_filters": preview_filters(),
        "zooms": ZOOMS, "zoom_amount": ZOOM_AMOUNT, "qualities": {k: v[0] for k, v in QUALITY.items()},
        "styles": {k: {"font": f, "color": c, "outline": o, "width": w, "box": b == 3, "shadow": s, "bold": bold}
                   for k, (f, c, o, w, b, s, bold) in TEXT_STYLES.items()},
        "positions": list(POSITIONS), "animations": list(ANIMATIONS), "max_texts": MAX_TEXTS,
    }


def music_names() -> set:
    from . import music as music_lib
    return {t["file"] for t in music_lib.list_tracks(config.MUSIC_DIR)}
