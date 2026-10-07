"""The editor's Catalog: ready-made titles, caption styles, social overlays, stickers,
transitions, animations, clip effects and looks. Each preset is plain project data
(project.py), so the browser preview and the ffmpeg render treat it like anything else.

A preset with "items" adds several things at once (e.g. a follow card = a pill + text +
emoji); times are relative to where it's dropped, and "{credit}" / "{title}" are filled in.
"""

from pathlib import Path

from . import project as P

C = P.W / 2  # centre x


def _t(**kw) -> dict:
    base = {"font": "Poppins Black", "size": 90, "color": "#FFFFFF", "outline": 6, "outline_color": "#000000",
            "shadow": 2, "box": False, "box_color": "#000000", "uppercase": False, "align": "center",
            "effect": "none", "accent": "#FFE600", "anim_in": "pop", "anim_out": "none", "x": C, "y": 420,
            "rotation": 0, "opacity": 1, "kf": {}}
    base.update(kw)
    return base


TITLES = [
    {"id": "hook_bold", "name": "Bold hook", "item": _t(font="Anton", size=120, uppercase=True, outline=8,
                                                        anim_in="slide_up", anim_out="fade", text="WAIT FOR IT")},
    {"id": "hook_pop", "name": "Pop title", "item": _t(font="Luckiest Guy", size=110, color="#FFE600", outline=8,
                                                       anim_in="bounce", text="NO WAY 😱")},
    {"id": "hook_box", "name": "Boxed title", "item": _t(font="Poppins Black", size=72, color="#111111", box=True,
                                                         box_color="#FFFFFF", outline=0, shadow=0, anim_in="pop",
                                                         text="Watch till the end")},
    {"id": "marker", "name": "Marker note", "item": _t(font="Permanent Marker", size=96, color="#FF4FD8", outline=5,
                                                       anim_in="spin", rotation=-6, text="wait for it…")},
    {"id": "comic", "name": "Comic burst", "item": _t(font="Bangers", size=130, color="#FFFFFF", outline=10,
                                                      outline_color="#E1306C", anim_in="zoom", text="BOOM!")},
    {"id": "news", "name": "Breaking", "item": _t(font="Archivo Black", size=64, color="#FFFFFF", box=True,
                                                  box_color="#D7263D", outline=0, shadow=0, anim_in="slide_left",
                                                  uppercase=True, text="Breaking news")},
    {"id": "minimal", "name": "Minimal", "item": _t(font="Poppins Bold", size=62, outline=0, shadow=4,
                                                    anim_in="fade", anim_out="fade", text="Your text")},
    {"id": "mono", "name": "Mono stamp", "item": _t(font="Rubik Mono One", size=70, color="#39FF14", outline=4,
                                                    anim_in="drop", text="LEVEL UP")},
    {"id": "bebas", "name": "Tall headline", "item": _t(font="Bebas Neue", size=150, outline=6, anim_in="slide_down",
                                                        text="PART 2")},
]

CAPTIONS = [  # applied to every caption at once
    {"id": "cap_highlight", "name": "Word highlight", "style": _t(font="Poppins Black", size=84, effect="highlight",
                                                                  accent="#FFE600", anim_in="none", y=1380, outline=8)},
    {"id": "cap_green", "name": "Green sweep", "style": _t(font="Poppins Black", size=84, effect="highlight",
                                                           accent="#39FF14", anim_in="none", y=1380, outline=8)},
    {"id": "cap_words", "name": "Word by word", "style": _t(font="Poppins Black", size=88, effect="words",
                                                            anim_in="none", y=1380, outline=8)},
    {"id": "cap_impact", "name": "Big impact", "style": _t(font="Anton", size=120, uppercase=True, effect="highlight",
                                                           accent="#FF3B3B", anim_in="pop", y=1300, outline=10)},
    {"id": "cap_boxed", "name": "Boxed", "style": _t(font="Poppins Bold", size=66, box=True, box_color="#000000",
                                                     outline=0, shadow=0, effect="none", anim_in="fade", y=1400)},
    {"id": "cap_comic", "name": "Comic", "style": _t(font="Bangers", size=104, effect="highlight", accent="#00E5FF",
                                                     anim_in="bounce", y=1350, outline=9)},
    {"id": "cap_minimal", "name": "Clean", "style": _t(font="Poppins Bold", size=70, outline=0, shadow=5,
                                                       effect="none", anim_in="none", y=1420)},
    {"id": "cap_type", "name": "Typewriter", "style": _t(font="Bebas Neue", size=110, effect="typewriter",
                                                         anim_in="none", y=1360)},
]

EMOJI = ["1f525", "1f602", "1f631", "1f480", "1f440", "2764_fe0f", "1f4af", "1f62d", "1f923", "1f44f",
         "26a0_fe0f", "2728", "1f389", "1f60d", "1f914", "1f64f", "1f92f", "1f60e", "1f4b0", "1f451",
         "1f6a8", "1f44d", "1f447", "1f449", "2705", "274c", "1f91d", "1f3c6"]

SOCIAL = [
    {"id": "credit", "name": "Credit lower-third", "dur": 4, "items": [
        {"track": "text", **_t(text="🎥 {credit}", font="Poppins Bold", size=46, box=True, box_color="#111111",
                               outline=0, shadow=0, y=1660, anim_in="slide_up", anim_out="fade"), "start": 0, "dur": 4}]},
    {"id": "follow", "name": "Follow for part 2", "dur": 3, "items": [
        {"track": "overlay", "asset": "shape:pill", "start": 0, "dur": 3, "x": C, "y": 1500, "w": 760,
         "anim_in": "pop", "anim_out": "fade"},
        {"track": "text", **_t(text="Follow for part 2 👉", font="Poppins Black", size=58, color="#111111",
                               outline=0, shadow=0, y=1500, anim_in="pop", anim_out="fade"), "start": 0.05, "dur": 2.95}]},
    {"id": "likeshare", "name": "Like & share", "dur": 3, "items": [
        {"track": "overlay", "asset": "emoji:2764_fe0f", "start": 0, "dur": 3, "x": 330, "y": 1520, "w": 170,
         "anim_in": "bounce", "anim_out": "fade"},
        {"track": "text", **_t(text="Like & share", font="Luckiest Guy", size=84, color="#FFFFFF", outline=7,
                               x=620, y=1520, anim_in="pop", anim_out="fade"), "start": 0.1, "dur": 2.9}]},
    {"id": "progress", "name": "Progress bar", "whole": True, "items": [
        {"track": "overlay", "asset": "shape:bar", "start": 0, "x": C, "y": 1900, "w": P.W, "scale_mode": "width",
         "kf": {"scale": [[0, 0.01], ["end", 1]]}}]},
    {"id": "hook_title", "name": "Hook from title", "dur": 3, "items": [
        {"track": "text", **_t(text="{title}", font="Anton", size=104, uppercase=True, outline=8, y=360,
                               anim_in="slide_up", anim_out="fade"), "start": 0, "dur": 3}]},
    {"id": "subscribe", "name": "Comment below", "dur": 3, "items": [
        {"track": "overlay", "asset": "emoji:1f447", "start": 0, "dur": 3, "x": C, "y": 1640, "w": 150,
         "anim_in": "drop", "kf": {"y": [[0.4, 1640], [0.9, 1610], [1.4, 1640], [1.9, 1610], [2.4, 1640]]}},
        {"track": "text", **_t(text="Tell me in the comments", font="Poppins Black", size=60, y=1480,
                               anim_in="pop", anim_out="fade"), "start": 0, "dur": 3}]},
]

SHAPES = {"pill": {"w": 760, "h": 130, "color": "#FFFFFF", "radius": 65},
          "bar": {"w": P.W, "h": 14, "color": "#FFE600", "radius": 7},
          "box": {"w": 900, "h": 300, "color": "#111111", "radius": 36}}

CLIP_EFFECTS = [  # keyframes on a main-track clip (t "end" = the clip's length)
    {"id": "punch", "name": "Punch-in", "kf": {"zoom": [[0, 1.0], [0.25, 1.3], ["end", 1.3]]}},
    {"id": "slow_in", "name": "Slow zoom in", "kf": {"zoom": [[0, 1.0], ["end", 1.15]]}},
    {"id": "slow_out", "name": "Slow zoom out", "kf": {"zoom": [[0, 1.15], ["end", 1.0]]}},
    {"id": "push_left", "name": "Pan across", "kf": {"zoom": [[0, 1.2], ["end", 1.2]], "panx": [[0, 90], ["end", -90]]}},
    {"id": "shake", "name": "Shake", "kf": {"zoom": [[0, 1.08], ["end", 1.08]],
                                            "panx": [[t / 20, (12 if i % 2 else -12)] for i, t in enumerate(range(0, 11))],
                                            "pany": [[t / 20, (-8 if i % 3 else 8)] for i, t in enumerate(range(0, 11))]}},
    {"id": "none", "name": "No effect", "kf": {}},
]

LOOKS = {"none": "None", "warm": "Warm", "cool": "Cool", "vivid": "Vivid", "bw": "Black & white",
         "vintage": "Vintage", "cinematic": "Cinematic", "faded": "Faded", "bright": "Bright"}
TRANSITION_NAMES = {k: k.replace("left", " left").replace("right", " right").replace("up", " up")
                    .replace("down", " down").replace("open", " open").replace("close", " close").strip().title()
                    for k in P.TRANSITIONS}
ANIM_NAMES = {a: a.replace("_", " ").title() for a in P.ANIMS}


WINDOWS_FONTS = {"Arial Black": "ariblk.ttf", "Arial": "arial.ttf", "Impact": "impact.ttf", "Segoe UI": "segoeui.ttf"}
_metrics = {}


def font_metrics() -> dict:
    """name -> {"scale": CSS px per text-size unit, "line": line height per unit}. libass sizes
    a font by its Windows line height (usWinAscent + usWinDescent; measured to match within
    0.3%), browsers by its em: this keeps the preview the same size as the render."""
    if not _metrics:
        from fontTools.ttLib import TTFont

        for name, (_, _, file) in P.FONTS.items():
            path = (Path(__file__).parent / "static" / "fonts" / file) if file \
                else Path(r"C:\Windows\Fonts") / WINDOWS_FONTS.get(name, "")
            scale, asc_pct, desc_pct = 0.75, 107.0, 27.0
            try:
                font = TTFont(path, lazy=True)
                os2, upm = font["OS/2"], font["head"].unitsPerEm
                scale = upm / (os2.usWinAscent + os2.usWinDescent)
                asc_pct, desc_pct = 100 * os2.usWinAscent / upm, 100 * os2.usWinDescent / upm
            except Exception:  # noqa: BLE001 - a missing system font: typical values
                pass
            # ascent/descent overrides make the browser put the baseline where libass does
            _metrics[name] = {"scale": round(scale, 4), "ascent_pct": round(asc_pct, 2),
                              "descent_pct": round(desc_pct, 2)}
    return _metrics


def as_dict() -> dict:
    from .render import preview_filters
    return {"titles": TITLES, "captions": CAPTIONS, "emoji": EMOJI, "social": SOCIAL, "shapes": SHAPES,
            "clip_effects": CLIP_EFFECTS, "looks": LOOKS, "css_looks": preview_filters(),
            "transitions": TRANSITION_NAMES, "anims": ANIM_NAMES, "effects": list(P.EFFECTS),
            "fonts": {name: {"file": f[2], "bold": f[1], **font_metrics()[name]} for name, f in P.FONTS.items()},
            "anim_curves": {a: P.anim_curves(a) for a in P.ANIMS}, "anim_dur": P.ANIM_DUR,
            "canvas": {"w": P.W, "h": P.H, "fps": P.FPS}, "quality": list(P.QUALITY),
            "heights": list(P.HEIGHTS), "fps": list(P.FPS_CHOICES)}
