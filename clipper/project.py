"""Clipper's editor project (edit format 2): tracks of items on a timeline, like a
HyperFrames / CapCut-style editor, rendered by ffmpeg (render2.py).

project = {
  "version": 2,
  "look": {"filter": "none", "fade_in": 0.0, "fade_out": 0.0},
  "export": {"height": 1920, "fps": 30, "quality": "standard"},
  "ducking": true,                       # music dips while people talk
  "assets": {id: {"kind": "video"|"image"|"audio", "src": "clip"|"original"|"music/<f>"|"upload/<f>"|"emoji/<hex>"|"shape/<id>",
                  "name": str, "duration": s, "w": px, "h": px, "audio": bool}},
  "tracks": [ {"id", "kind": "main"|"overlay"|"text"|"caption"|"audio", "name", "hidden", "muted", "items": [...]} ]
}
The main track is magnetic: its clips play back to back (a transition overlaps two).
Items on other tracks have their own start. Later tracks draw on top.

Items
  main:    {id, asset, in, out, speed, volume, look, transition: {type, dur}, kf: {zoom, panx, pany}}
  overlay: {id, asset, start, dur, in, x, y, w, rotation, opacity, volume, fit: "free"|"fill",
            anim_in, anim_out, kf: {x, y, scale, rotation, opacity}}
  text:    {id, text, start, dur, x, y, font, size, color, outline, outline_color, shadow, box,
            box_color, bold, uppercase, align, effect, accent, words, anim_in, anim_out,
            kf: {x, y, scale, rotation, opacity}, auto}
  audio:   {id, asset, start, dur, in, volume, fade_in, fade_out, music, duck, kf: {volume}}

Keyframes are [[t, value], ...] with t in seconds from the item's own start; values change
in straight lines between them (the preview and the render both use pwl()/at()).
"""

import copy
import math
import re

W, H, FPS = 1080, 1920, 30
VERSION = 2

FILTERS = ("none", "warm", "cool", "vivid", "bw", "vintage", "cinematic", "faded", "bright")
TRANSITIONS = {  # editor name -> ffmpeg xfade transition
    "none": None, "fade": "fade", "dissolve": "dissolve", "fadeblack": "fadeblack", "fadewhite": "fadewhite",
    "wipeleft": "wipeleft", "wiperight": "wiperight", "wipeup": "wipeup", "wipedown": "wipedown",
    "slideleft": "slideleft", "slideright": "slideright", "slideup": "slideup", "slidedown": "slidedown",
    "circleopen": "circleopen", "circleclose": "circleclose", "radial": "radial", "pixelize": "pixelize",
    "zoomin": "zoomin", "smoothleft": "smoothleft", "smoothup": "smoothup", "hblur": "hblur",
    "squeezev": "squeezev", "distance": "distance",
}
ANIMS = ("none", "fade", "pop", "bounce", "zoom", "slide_up", "slide_down", "slide_left", "slide_right",
         "spin", "drop")
EFFECTS = ("none", "highlight", "words", "typewriter")
ALIGNS = ("center", "left", "right")
QUALITY = {"small": 24, "standard": 20, "high": 17}
HEIGHTS = (1920, 1280)  # 1080p, 720p
FPS_CHOICES = (24, 30, 60)
TRACK_KINDS = ("main", "overlay", "text", "caption", "audio")
MAX_ITEMS = 400
MAX_KEYS = 40

# Fonts: name in the editor -> (font family for libass, bold flag, file in static/fonts or None = Windows)
FONTS = {
    "Anton": ("Anton", False, "Anton-Regular.ttf"),
    "Bebas Neue": ("Bebas Neue", False, "BebasNeue-Regular.ttf"),
    "Archivo Black": ("Archivo Black", False, "ArchivoBlack-Regular.ttf"),
    "Bangers": ("Bangers", False, "Bangers-Regular.ttf"),
    "Luckiest Guy": ("Luckiest Guy", False, "LuckiestGuy-Regular.ttf"),
    "Permanent Marker": ("Permanent Marker", False, "PermanentMarker-Regular.ttf"),
    "Poppins Black": ("Poppins Black", False, "Poppins-Black.ttf"),
    "Poppins Bold": ("Poppins", True, "Poppins-Bold.ttf"),
    "Rubik Mono One": ("Rubik Mono One", False, "RubikMonoOne-Regular.ttf"),
    "Arial Black": ("Arial Black", False, None),
    "Arial": ("Arial", False, None),
    "Impact": ("Impact", False, None),
    "Segoe UI": ("Segoe UI", False, None),
}

# How the entrance / exit animations move things: [(seconds, value)] curves, multiplied into
# scale and opacity, added to x / y / rotation. Exits use the same curves backwards.
ANIM_DUR = {"fade": 0.3, "pop": 0.26, "bounce": 0.32, "zoom": 0.3, "slide_up": 0.3, "slide_down": 0.3,
            "slide_left": 0.3, "slide_right": 0.3, "spin": 0.4, "drop": 0.35}


def anim_curves(name: str) -> dict:
    """{prop: [(t, v), ...]} for an entrance, t from 0 to its duration; props are
    scale (x), opacity (x), dx, dy, rotation (+)."""
    d = ANIM_DUR.get(name, 0)
    if name == "fade":
        return {"opacity": [(0, 0), (d, 1)]}
    if name == "pop":
        return {"scale": [(0, 0.4), (0.16, 1.08), (d, 1)], "opacity": [(0, 0), (0.08, 1)]}
    if name == "bounce":
        return {"scale": [(0, 0.3), (0.12, 1.18), (0.2, 0.92), (0.27, 1.04), (d, 1)], "opacity": [(0, 0), (0.06, 1)]}
    if name == "zoom":
        return {"scale": [(0, 0.0), (d, 1)], "opacity": [(0, 0), (0.1, 1)]}
    if name == "spin":
        return {"rotation": [(0, -180), (d, 0)], "scale": [(0, 0.2), (d, 1)], "opacity": [(0, 0), (0.1, 1)]}
    if name == "drop":
        return {"dy": [(0, -260), (0.22, 20), (d, 0)], "opacity": [(0, 0), (0.1, 1)]}
    slides = {"slide_up": ("dy", 160), "slide_down": ("dy", -160), "slide_left": ("dx", 220), "slide_right": ("dx", -220)}
    if name in slides:
        prop, dist = slides[name]
        return {prop: [(0, dist), (d, 0)], "opacity": [(0, 0), (d * 0.6, 1)]}
    return {}


class ProjectError(ValueError):
    pass


# ----------------------------------------------------------------------- piecewise-linear

def at(points, t: float, default: float) -> float:
    """Value of a keyframe list at time t (clamped at the ends)."""
    if not points:
        return default
    if t <= points[0][0]:
        return points[0][1]
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t <= t1:
            return v0 if t1 == t0 else v0 + (v1 - v0) * (t - t0) / (t1 - t0)
    return points[-1][1]


PROPS = {"x": ("add", None), "y": ("add", None), "scale": ("mul", 1.0), "rotation": ("add", 0.0),
         "opacity": ("mul", 1.0)}


def motion(item: dict, base: dict) -> dict:
    """Each moving property of a text / overlay as absolute-time keyframes [(t, v)], with the
    item's keyframes and its entrance / exit animations folded in. Times are on the timeline."""
    start, dur = item["start"], item["dur"]
    kf = item.get("kf") or {}
    curves_in = anim_curves(item.get("anim_in", "none"))
    curves_out = anim_curves(item.get("anim_out", "none"))
    d_in = min(ANIM_DUR.get(item.get("anim_in", "none"), 0), dur / 2)
    d_out = min(ANIM_DUR.get(item.get("anim_out", "none"), 0), dur / 2)
    anim_key = {"x": "dx", "y": "dy", "scale": "scale", "rotation": "rotation", "opacity": "opacity"}
    out = {}
    for prop, (mode, neutral) in PROPS.items():
        keys = [(start + t, v) for t, v in kf.get(prop) or []]
        own = base[prop]
        a_in, a_out = curves_in.get(anim_key[prop]), curves_out.get(anim_key[prop])
        times = {start, start + dur} | {t for t, _ in keys}
        scale_in = d_in / ANIM_DUR[item["anim_in"]] if a_in else 1
        scale_out = d_out / ANIM_DUR[item["anim_out"]] if a_out else 1
        if a_in:
            times |= {start + t * scale_in for t, _ in a_in}
        if a_out:
            times |= {start + dur - t * scale_out for t, _ in a_out}
        if not keys and not a_in and not a_out:
            out[prop] = [(start, own)]
            continue
        neutral_v = 0.0 if mode == "add" else 1.0
        pts = []
        for t in sorted(times):
            v = at(keys, t, own)
            m = neutral_v
            if a_in and t <= start + d_in + 1e-9:
                m = at([(start + a * scale_in, b) for a, b in a_in], t, neutral_v)
            if a_out and t >= start + dur - d_out - 1e-9:
                mo = at([(start + dur - a * scale_out, b) for a, b in reversed(a_out)], t, neutral_v)
                m = m + mo if mode == "add" else m * mo
            pts.append((round(t, 4), round(v + m if mode == "add" else v * m, 4)))
        out[prop] = pts
    return out


def pwl_expr(points, var: str = "t") -> str:
    """An ffmpeg expression for a keyframe list (times absolute)."""
    if len(points) == 1 or all(abs(v - points[0][1]) < 1e-9 for _, v in points):
        return f"{points[0][1]:.4f}"
    expr = f"{points[-1][1]:.4f}"
    for (t0, v0), (t1, v1) in reversed(list(zip(points, points[1:]))):
        seg = f"{v0:.4f}" if t1 <= t0 else f"({v0:.4f}+({v1 - v0:.4f})*({var}-{t0:.4f})/{t1 - t0:.4f})"
        expr = f"if(lt({var},{t1:.4f}),{seg},{expr})"
    return f"if(lt({var},{points[0][0]:.4f}),{points[0][1]:.4f},{expr})"


# ----------------------------------------------------------------------- the main track

def main_items(project: dict) -> list:
    for track in project["tracks"]:
        if track["kind"] == "main":
            return track["items"]
    return []


def item_length(item: dict) -> float:
    return (item["out"] - item["in"]) / item["speed"]


def layout(project: dict) -> list:
    """[(item, timeline_start, length, transition_dur_into_it)] for the main track."""
    out, cursor = [], 0.0
    for i, item in enumerate(main_items(project)):
        length = item_length(item)
        trans = item.get("transition") or {}
        d = 0.0
        if i and TRANSITIONS.get(trans.get("type", "none")):
            d = min(trans.get("dur", 0.5), length / 2, out[-1][2] / 2)
        start = cursor - d
        out.append((item, start, length, d))
        cursor = start + length
    return out


def duration(project: dict) -> float:
    lay = layout(project)
    if lay:
        return round(lay[-1][1] + lay[-1][2], 3)
    ends = [it["start"] + it["dur"] for tr in project["tracks"] if tr["kind"] != "main" for it in tr["items"]]
    return round(max(ends, default=0.0), 3)


# ----------------------------------------------------------------------- validation

def _num(value, low, high, default):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(v) or math.isinf(v):
        return default
    return round(min(high, max(low, v)), 4)


def _color(value, default):
    return value.upper() if isinstance(value, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", value) else default


def _choice(value, choices, default):
    return value if value in choices else default


def _keys(raw, low, high, length):
    if not isinstance(raw, list):
        return []
    pts = []
    for pair in raw[:MAX_KEYS]:
        try:
            t, v = float(pair[0]), float(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isfinite(t) and math.isfinite(v) and t >= 0:
            pts.append([t, min(high, max(low, v))])
    pts.sort(key=lambda p: p[0])
    # keys past the end (the item got shorter, e.g. a transition shortened the video): keep the
    # value the motion has when it ends, rather than dropping the key and freezing the motion
    if pts and pts[-1][0] > length + 1e-6:
        inside = [p for p in pts if p[0] <= length + 1e-6]
        end_value = at([tuple(p) for p in pts], length, pts[-1][1])
        pts = inside + [[length, end_value]]
    pts = [[round(t, 4), round(v, 4)] for t, v in pts]
    dedup = []
    for p in pts:
        if dedup and abs(dedup[-1][0] - p[0]) < 1e-4:
            dedup[-1] = p
        else:
            dedup.append(p)
    return dedup


def _id(raw, used: set) -> str:
    base = re.sub(r"[^A-Za-z0-9_-]", "", str(raw or ""))[:24] or "i"
    name, n = base, 1
    while name in used:
        n += 1
        name = f"{base}_{n}"
    used.add(name)
    return name


def clean_asset(raw: dict) -> dict:
    kind = _choice(raw.get("kind"), ("video", "image", "audio"), None)
    src = str(raw.get("src") or "")
    if not kind or not re.fullmatch(r"(clip|original|(music|upload|emoji|shape)/[^/\\]{1,120})", src) or ".." in src:
        raise ProjectError(f"Bad asset: {src!r}")
    asset = {"kind": kind, "src": src, "name": str(raw.get("name") or src)[:120],
             "duration": _num(raw.get("duration"), 0, 36000, 0.0),
             "w": int(_num(raw.get("w"), 0, 10000, 0)), "h": int(_num(raw.get("h"), 0, 10000, 0)),
             "audio": bool(raw.get("audio"))}
    if src.startswith("shape/"):  # drawn on demand: a rounded rectangle
        if kind != "image" or not re.fullmatch(r"shape/[A-Za-z0-9_-]{1,40}", src):
            raise ProjectError(f"Bad shape: {src!r}")
        asset.update(w=max(2, min(asset["w"] or 200, 4000)), h=max(2, min(asset["h"] or 200, 4000)),
                     color=_color(raw.get("color"), "#FFFFFF"), radius=int(_num(raw.get("radius"), 0, 2000, 0)))
    if src.startswith("emoji/") and not re.fullmatch(r"emoji/[0-9a-f]{2,8}(_[0-9a-f]{2,8}){0,6}", src):
        raise ProjectError(f"Bad emoji: {src!r}")
    return asset


def clean(raw: dict, known_assets: dict = None) -> dict:
    """Validate a project from the browser: whitelist every choice, clamp every number, and
    refuse anything the renderer couldn't do. `known_assets` (id -> asset) is what exists on
    disk; assets the browser sends are only kept if they're among them."""
    if not isinstance(raw, dict):
        raise ProjectError("The project must be an object")
    known = known_assets if known_assets is not None else {}
    assets = {}
    for aid, asset in (raw.get("assets") or {}).items():
        if aid in known:
            assets[aid] = known[aid]
        else:
            try:
                assets[str(aid)[:40]] = clean_asset(asset or {})
            except ProjectError:
                continue
    look = raw.get("look") or {}
    out = {
        "version": VERSION,
        "look": {"filter": _choice(look.get("filter"), FILTERS, "none"),
                 "fade_in": _num(look.get("fade_in"), 0, 3, 0.0), "fade_out": _num(look.get("fade_out"), 0, 3, 0.0)},
        "export": {"height": int(_choice(int(_num((raw.get("export") or {}).get("height"), 0, 4000, 1920)), HEIGHTS, 1920)),
                   "fps": int(_choice(int(_num((raw.get("export") or {}).get("fps"), 0, 120, 30)), FPS_CHOICES, 30)),
                   "quality": _choice((raw.get("export") or {}).get("quality"), QUALITY, "standard")},
        "ducking": bool(raw.get("ducking", True)),
        "markers": sorted({round(_num(m, 0, 36000, 0.0), 3) for m in (raw.get("markers") or [])[:500]
                           if isinstance(m, (int, float))}),
        "assets": assets,
        "tracks": [],
    }
    used_ids, count = set(), 0
    tracks = [t for t in (raw.get("tracks") or []) if isinstance(t, dict)]
    mains = [t for t in tracks if t.get("kind") == "main"]
    if len(mains) != 1:
        raise ProjectError("A project needs exactly one main video track")
    for track in [mains[0]] + [t for t in tracks if t.get("kind") != "main"]:
        kind = _choice(track.get("kind"), TRACK_KINDS, None)
        if not kind:
            continue
        items = []
        for raw_item in track.get("items") or []:
            if not isinstance(raw_item, dict) or count >= MAX_ITEMS:
                continue
            item = _clean_item(kind, raw_item, assets, used_ids)
            if item:
                items.append(item)
                count += 1
        if kind != "main":
            items.sort(key=lambda it: it["start"])
        out["tracks"].append({"id": _id(track.get("id"), used_ids), "kind": kind,
                              "name": str(track.get("name") or kind.title())[:40],
                              "hidden": bool(track.get("hidden")), "muted": bool(track.get("muted")),
                              "items": items})
    if not out["tracks"][0]["items"]:
        raise ProjectError("The main track needs at least one clip")
    if duration(out) < 0.5:
        raise ProjectError("The video must be at least half a second long")
    total = duration(out)
    for track in out["tracks"][1:]:  # nothing may start after the video ends
        track["items"] = [it for it in track["items"] if it["start"] < total - 0.05]
        for it in track["items"]:
            it["dur"] = round(min(it["dur"], total - it["start"]), 4)
    return out


def _motion_props(raw: dict, length: float) -> dict:
    kf = raw.get("kf") or {}
    return {"kf": {k: _keys(kf.get(k), lo, hi, length) for k, (lo, hi) in
                   {"x": (-W, 2 * W), "y": (-H, 2 * H), "scale": (0, 10), "rotation": (-3600, 3600),
                    "opacity": (0, 1)}.items() if kf.get(k)},
            "anim_in": _choice(raw.get("anim_in"), ANIMS, "none"),
            "anim_out": _choice(raw.get("anim_out"), ANIMS, "none"),
            "x": _num(raw.get("x"), -W, 2 * W, W / 2), "y": _num(raw.get("y"), -H, 2 * H, H / 2),
            "rotation": _num(raw.get("rotation"), -3600, 3600, 0.0),
            "opacity": _num(raw.get("opacity"), 0, 1, 1.0)}


def _clean_item(kind: str, raw: dict, assets: dict, used: set):
    iid = _id(raw.get("id"), used)
    if kind == "main":
        asset = assets.get(raw.get("asset"))
        if not asset or asset["kind"] not in ("video", "image"):
            return None
        limit = asset["duration"] if asset["kind"] == "video" and asset["duration"] else 3600
        a = _num(raw.get("in"), 0, limit, 0.0)
        b = _num(raw.get("out"), 0, limit, limit if asset["kind"] == "video" else a + 3)
        if b - a < 0.1:
            return None
        speed = _num(raw.get("speed"), 0.25, 4, 1.0) if asset["kind"] == "video" else 1.0
        trans = raw.get("transition") or {}
        length = (b - a) / speed
        kf = raw.get("kf") or {}
        return {"id": iid, "asset": raw["asset"], "in": a, "out": b, "speed": speed,
                "volume": _num(raw.get("volume"), 0, 3, 1.0), "look": _choice(raw.get("look"), FILTERS, "none"),
                "transition": {"type": _choice(trans.get("type"), TRANSITIONS, "none"),
                               "dur": _num(trans.get("dur"), 0.1, 2, 0.5)},
                "kf": {k: _keys(kf.get(k), lo, hi, length) for k, (lo, hi) in
                       {"zoom": (1, 4), "panx": (-W, W), "pany": (-H, H)}.items() if kf.get(k)}}
    start = _num(raw.get("start"), 0, 36000, 0.0)
    dur = _num(raw.get("dur"), 0.1, 36000, 3.0)
    if kind == "overlay":
        asset = assets.get(raw.get("asset"))
        if not asset or asset["kind"] not in ("video", "image"):
            return None
        if asset["kind"] == "video" and asset["duration"]:
            a = _num(raw.get("in"), 0, asset["duration"], 0.0)
            dur = min(dur, asset["duration"] - a)
        else:
            a = 0.0
        if dur < 0.1:
            return None
        return {"id": iid, "asset": raw["asset"], "start": start, "dur": dur, "in": a,
                "w": _num(raw.get("w"), 8, 4 * W, 400), "volume": _num(raw.get("volume"), 0, 3, 0.0),
                "fit": _choice(raw.get("fit"), ("free", "fill"), "free"),
                "scale_mode": _choice(raw.get("scale_mode"), ("uniform", "width"), "uniform"),
                **_motion_props(raw, dur)}
    if kind in ("text", "caption"):
        text = str(raw.get("text") or "").strip()[:300]
        if not text:
            return None
        words = raw.get("words")
        word_count = len(text.split())
        clean_words = None
        if isinstance(words, list) and len(words) == word_count:
            clean_words = []
            for pair in words:
                try:
                    clean_words.append([round(min(max(float(pair[0]), 0), dur), 4),
                                        round(min(max(float(pair[1]), 0), dur), 4)])
                except (TypeError, ValueError, IndexError):
                    clean_words = None
                    break
            if clean_words and any(b[0] < a[0] for a, b in zip(clean_words, clean_words[1:])):
                clean_words = None
        return {"id": iid, "text": text, "start": start, "dur": dur,
                "font": _choice(raw.get("font"), FONTS, "Poppins Black"),
                "size": int(_num(raw.get("size"), 12, 400, 80)),
                "color": _color(raw.get("color"), "#FFFFFF"),
                "outline": int(_num(raw.get("outline"), 0, 30, 6)),
                "outline_color": _color(raw.get("outline_color"), "#000000"),
                "shadow": int(_num(raw.get("shadow"), 0, 20, 2)),
                "box": bool(raw.get("box")), "box_color": _color(raw.get("box_color"), "#000000"),
                "uppercase": bool(raw.get("uppercase")), "align": _choice(raw.get("align"), ALIGNS, "center"),
                "effect": _choice(raw.get("effect"), EFFECTS, "none"),
                "accent": _color(raw.get("accent"), "#FFE600"), "words": clean_words,
                "auto": bool(raw.get("auto")), **_motion_props(raw, dur)}
    if kind == "audio":
        asset = assets.get(raw.get("asset"))
        if not asset or asset["kind"] not in ("audio", "video") or (asset["kind"] == "video" and not asset["audio"]):
            return None
        a = _num(raw.get("in"), 0, max(0.0, asset["duration"] - 0.1) if asset["duration"] else 36000, 0.0)
        kf = raw.get("kf") or {}
        return {"id": iid, "asset": raw["asset"], "start": start, "dur": dur, "in": a,
                "volume": _num(raw.get("volume"), 0, 3, 1.0),
                "fade_in": _num(raw.get("fade_in"), 0, min(10, dur / 2), 0.0),
                "fade_out": _num(raw.get("fade_out"), 0, min(10, dur / 2), 0.0),
                "music": bool(raw.get("music")), "duck": bool(raw.get("duck", raw.get("music"))),
                "kf": {"volume": _keys(kf.get("volume"), 0, 3, dur)} if kf.get("volume") else {}}
    return None


# ----------------------------------------------------------------------- from / to edit format 1

V1_POSITIONS = {"top": 260, "middle": H // 2, "bottom": H - 420}
V1_FONTS = {"classic": "Arial Black", "yellow": "Arial Black", "box": "Arial", "minimal": "Segoe UI",
            "neon": "Arial Black", "caption": "Arial Black", "pill": "Arial Black"}
V1_ANIM = {"pop": ("pop", "none"), "fade": ("fade", "fade"), "bounce": ("bounce", "none"),
           "slide": ("slide_up", "fade")}


def from_v1(edit: dict, clip_asset: dict, music_asset: tuple = None) -> dict:
    """Turn a format-1 edit (one trimmed clip, music, texts) into a project."""
    trim = edit["trim"]
    zoom = edit.get("zoom", "none")
    length = trim["end"] - trim["start"]
    kf = {}
    if zoom == "slow_in":
        kf["zoom"] = [[0, 1.0], [round(length, 3), 1.12]]
    elif zoom == "slow_out":
        kf["zoom"] = [[0, 1.12], [round(length, 3), 1.0]]
    texts = []
    for t in edit.get("texts") or []:
        y = V1_POSITIONS.get(t.get("position"), 260)
        anim_in, anim_out = V1_ANIM.get(t.get("animation"), ("none", "none"))
        effect = t.get("animation") if t.get("animation") in ("highlight", "words", "typewriter") else "none"
        style = t.get("style", "classic")
        texts.append({"text": t["text"], "start": t["start"], "dur": round(t["end"] - t["start"], 4),
                      "x": W / 2, "y": y + (0 if t.get("position") == "middle" else
                                            (t.get("size", 72) * 0.6 if t.get("position") == "top" else -t.get("size", 72) * 0.6)),
                      "font": V1_FONTS.get(style, "Arial Black"), "size": int(t.get("size", 72)),
                      "color": t.get("color", "#FFFFFF"), "outline": 0 if style in ("box", "pill", "minimal") else 6,
                      "box": style in ("box", "pill"), "box_color": "#111111", "shadow": 2,
                      "effect": effect, "accent": t.get("accent", "#FFE600"), "words": t.get("words"),
                      "anim_in": anim_in, "anim_out": anim_out, "auto": bool(t.get("auto"))})
    assets = {"clip": clip_asset}
    tracks = [{"id": "main", "kind": "main", "name": "Video", "items": [
        {"id": "c1", "asset": "clip", "in": trim["start"], "out": trim["end"], "speed": 1.0,
         "volume": edit.get("voice_volume", 1.0), "look": "none", "kf": kf}]}]
    tracks.append({"id": "texts", "kind": "text", "name": "Text",
                   "items": [t for t in texts if not t["auto"]]})
    if any(t["auto"] for t in texts):
        tracks.append({"id": "captions", "kind": "caption", "name": "Captions",
                       "items": [t for t in texts if t["auto"]]})
    music = edit.get("music") or {}
    audio_items = []
    if music.get("track") and music_asset:
        aid, asset = music_asset
        assets[aid] = asset
        audio_items.append({"asset": aid, "start": 0, "dur": round(length, 4), "in": music.get("offset", 0),
                            "volume": music.get("volume", 0.18), "fade_in": music.get("fade_in", 0.5),
                            "fade_out": music.get("fade_out", 1.0), "music": True, "duck": music.get("ducking", True)})
    tracks.append({"id": "music", "kind": "audio", "name": "Music", "items": audio_items})
    return {"version": VERSION,
            "look": {"filter": edit.get("filter", "none"), "fade_in": edit.get("fade_in", 0),
                     "fade_out": edit.get("fade_out", 0)},
            "export": {"height": 1920, "fps": 30, "quality": "standard"},
            "ducking": music.get("ducking", True), "assets": assets, "tracks": tracks}


def to_v1(project: dict, clip_duration: float) -> dict:
    """The closest format-1 edit, for the tools that only understand that (Shotcut, the
    HyperFrames Studio export): first clip's trim, the first music item, and the texts."""
    first = main_items(project)[0]
    edit = {
        "trim": {"start": first["in"], "end": min(first["in"] + duration(project), clip_duration)},
        "filter": project["look"]["filter"], "zoom": "none",
        "fade_in": project["look"]["fade_in"], "fade_out": project["look"]["fade_out"],
        "voice_volume": first.get("volume", 1.0),
        "music": {"track": None, "volume": 0.18, "offset": 0.0, "ducking": project.get("ducking", True),
                  "fade_in": 0.5, "fade_out": 1.0},
        "texts": [],
    }
    for track in project["tracks"]:
        if track["kind"] == "audio":
            for it in track["items"]:
                src = project["assets"].get(it["asset"], {}).get("src", "")
                if it.get("music") and src.startswith("music/") and edit["music"]["track"] is None:
                    edit["music"].update(track=src[6:], volume=min(1.0, it["volume"]), offset=it["in"],
                                         fade_in=it["fade_in"], fade_out=it["fade_out"])
        if track["kind"] in ("text", "caption"):
            for it in track["items"]:
                pos = "top" if it["y"] < H * 0.35 else "bottom" if it["y"] > H * 0.65 else "middle"
                edit["texts"].append({"text": it["text"], "start": it["start"], "end": it["start"] + it["dur"],
                                      "style": "box" if it["box"] else "classic", "position": pos,
                                      "size": max(24, min(160, int(it["size"]))), "color": it["color"],
                                      "animation": it["effect"] if it["effect"] != "none" else "pop",
                                      "accent": it["accent"], "words": None, "auto": it.get("auto", False)})
    return edit


def deep(project: dict) -> dict:
    return copy.deepcopy(project)
