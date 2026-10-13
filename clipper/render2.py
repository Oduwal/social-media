"""Render an editor project (format 2, see project.py) with one ffmpeg run.

Picture: the main track's clips (trimmed, sped up / slowed down, punch-in zooms) joined by
cuts or xfade transitions -> the look -> each overlay (images, B-roll, stickers, emoji, shapes)
with position / size / rotation / opacity over time -> all text through libass (fonts from
static/fonts) -> fades -> export size. Sound: the clips' own audio, overlays' audio, and audio
items (music, sound effects) with volume keyframes and fades; music marked "duck" dips under
the rest; then loudness normalisation.

A 40-second clip renders in about a minute, against several minutes for a browser-based render.
"""

import json
import logging
import math
import shutil
from pathlib import Path

from . import config, media, project as P
from .render import FILTERS, _ass_color, _ass_time, _ass_text

log = logging.getLogger(__name__)

FONT_DIR = Path(__file__).resolve().parent / "static" / "fonts"
UPLOAD_DIR = "uploads"
EMOJI_DIR = config.DATA_DIR / "emoji"
SHAPE_DIR = "shapes"


class RenderError(RuntimeError):
    pass


# ------------------------------------------------------------------ assets

def asset_path(asset: dict, job_dir: Path) -> Path:
    src = asset["src"]
    if src == "clip":
        return job_dir / "transformed.mp4"
    if src == "original":
        return job_dir / "highlight.mp4"
    kind, _, name = src.partition("/")
    if kind == "music":
        return config.MUSIC_DIR / name
    if kind == "upload":
        return job_dir / UPLOAD_DIR / name
    if kind == "emoji":
        return emoji_png(name)
    if kind == "shape":
        return make_shape(job_dir, name, asset["w"], asset["h"], asset.get("color", "#FFFFFF"), asset.get("radius", 0))
    raise RenderError(f"Unknown asset source {src}")


def emoji_png(code: str) -> Path:
    """A 512 px colour emoji (Noto Emoji artwork, Apache 2.0), fetched once and kept;
    Twemoji (CC-BY 4.0, 72 px) if Google doesn't have it."""
    import requests

    code = code.lower()
    if not all(part and all(c in "0123456789abcdef" for c in part) for part in code.split("_")):
        raise RenderError(f"Bad emoji code {code}")
    path = EMOJI_DIR / f"{code}.png"
    if not path.exists():
        EMOJI_DIR.mkdir(parents=True, exist_ok=True)
        for url in (f"https://fonts.gstatic.com/s/e/notoemoji/latest/{code}/512.png",
                    f"https://raw.githubusercontent.com/jdecked/twemoji/main/assets/72x72/{code.replace('_', '-')}.png"):
            resp = requests.get(url, timeout=30)
            if resp.status_code == 200 and resp.content[:4] == b"\x89PNG":
                path.write_bytes(resp.content)
                break
        else:
            raise RenderError(f"Couldn't fetch emoji {code}")
    return path


def make_shape(job_dir: Path, shape_id: str, w: int, h: int, color: str, radius: int) -> Path:
    """A rounded rectangle as a PNG (shapes are drawn once and used like images)."""
    from PIL import Image, ImageDraw

    path = job_dir / SHAPE_DIR / f"{shape_id}_{w}x{h}_{color[1:]}_{radius}.png"
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", (max(1, w), max(1, h)), (0, 0, 0, 0))
    rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
    ImageDraw.Draw(img).rounded_rectangle((0, 0, w - 1, h - 1), radius=min(radius, w // 2, h // 2), fill=rgb + (255,))
    img.save(path)
    return path


# ------------------------------------------------------------------ text (libass)

def _bgr(hex_color: str) -> str:
    return f"&H{hex_color[5:7]}{hex_color[3:5]}{hex_color[1:3]}&"


def _alpha(opacity: float) -> str:
    return f"&H{int(round(255 * (1 - max(0.0, min(1.0, opacity))))):02X}&"


def _text_style(i: int, it: dict) -> str:
    family, bold, _ = P.FONTS[it["font"]]
    align = {"center": 5, "left": 4, "right": 6}[it["align"]]
    if it["box"]:
        box_alpha = int(round(255 * (1 - max(0.0, min(1.0, it.get("box_opacity", 1))))))
        border, outline, outline_color = 3, max(8, it["size"] // 6), _ass_color(it["box_color"], box_alpha)
    else:
        border, outline, outline_color = 1, it["outline"], _ass_color(it["outline_color"])
    return (f"Style: T{i},{family},{it['size']},{_ass_color(it['color'])},&H000000FF,{outline_color},"
            f"&H96000000,{-1 if bold else 0},0,0,0,100,100,0,0,{border},{outline},{it['shadow']},"
            f"{align},60,60,0,1")


def _windows(it: dict, mot: dict) -> list:
    """Split a text's time into pieces where every moving property changes in a straight
    line (so one \\move / \\t covers each piece), also at word changes for the highlight."""
    start, end = it["start"], it["start"] + it["dur"]
    cuts = {start, end}
    for pts in mot.values():
        cuts |= {t for t, _ in pts if start < t < end}
    if it["effect"] in ("highlight", "words"):
        cuts |= {start + w[0] for w in word_times(it) if start < start + w[0] < end}
    elif it["effect"] == "typewriter":
        cuts |= {start + t for t in _type_times(it) if start < start + t < end}
    cuts = sorted(cuts)
    return [(a, b) for a, b in zip(cuts, cuts[1:]) if b - a > 0.009]


def word_times(it: dict) -> list:
    """When each word starts / ends, relative to the text's start."""
    words = it["text"].split()
    if it.get("words") and len(it["words"]) == len(words):
        return [tuple(w) for w in it["words"]]
    step = it["dur"] / max(1, len(words))
    return [(i * step, (i + 1) * step) for i in range(len(words))]


def _shown(it: dict) -> str:
    return it["text"].upper() if it["uppercase"] else it["text"]


def _type_times(it: dict) -> list:
    """When each character appears, for the typewriter (over the first ~40%, at most 1.2 s)."""
    span = min(1.2, it["dur"] * 0.4)
    n = len(_shown(it))
    return [i * span / max(1, n) for i in range(n)]


HIDDEN = "{\\alpha&HFF&}"  # fill, outline and shadow all invisible: the text keeps its layout


def _body(it: dict, a: float, opacity: float = 1.0) -> str:
    """The text of one piece, with the effect applied as it is at time a. Words / letters
    not shown yet are drawn fully transparent, so the line doesn't shift as they appear."""
    text, rel = _shown(it), a - it["start"] + 1e-6
    show = "{\\alpha" + _alpha(opacity) + "}"
    if it["effect"] == "highlight":
        words, times = text.split(), word_times(it)
        cur = max([i for i, (s, _) in enumerate(times) if s <= rel] or [0])
        return " ".join(f"{{\\1c{_bgr(it['accent'])}}}{_ass_text(w)}{{\\1c{_bgr(it['color'])}}}" if j == cur
                        else _ass_text(w) for j, w in enumerate(words))
    if it["effect"] == "words":
        words, times = text.split(), word_times(it)
        shown = sum(1 for s, _ in times if s <= rel)
        head, tail = " ".join(words[:shown]), " ".join(words[shown:])
        return (_ass_text(head) + (" " if head and tail else "") + (HIDDEN + _ass_text(tail) + show if tail else ""))
    if it["effect"] == "typewriter":
        shown = sum(1 for t in _type_times(it) if t <= rel)
        head, tail = text[:shown], text[shown:]
        return _ass_text(head) + (HIDDEN + _ass_text(tail) + show if tail else "")
    return _ass_text(text)


def text_events(i: int, it: dict) -> list:
    base = {"x": it["x"], "y": it["y"], "scale": 1.0, "rotation": it["rotation"], "opacity": it["opacity"]}
    mot = P.motion(it, base)
    out = []
    for a, b in _windows(it, mot):
        dur_ms = int(round((b - a) * 1000))
        x0, y0 = P.at(mot["x"], a, it["x"]), P.at(mot["y"], a, it["y"])
        x1, y1 = P.at(mot["x"], b - 0.0005, it["x"]), P.at(mot["y"], b - 0.0005, it["y"])
        s0, s1 = P.at(mot["scale"], a, 1), P.at(mot["scale"], b - 0.0005, 1)
        r0, r1 = P.at(mot["rotation"], a, 0), P.at(mot["rotation"], b - 0.0005, 0)
        o0, o1 = P.at(mot["opacity"], a, 1), P.at(mot["opacity"], b - 0.0005, 1)
        tags = [f"\\move({x0:.1f},{y0:.1f},{x1:.1f},{y1:.1f},0,{dur_ms})" if (abs(x0 - x1) > 0.05 or abs(y0 - y1) > 0.05)
                else f"\\pos({x0:.1f},{y0:.1f})",
                f"\\fscx{s0 * 100:.1f}\\fscy{s0 * 100:.1f}\\frz{-r0:.2f}\\alpha{_alpha(o0)}"]
        body = _body(it, a, o0)
        changes = []
        if abs(s0 - s1) > 1e-4:
            changes.append(f"\\fscx{s1 * 100:.1f}\\fscy{s1 * 100:.1f}")
        if abs(r0 - r1) > 1e-4:
            changes.append(f"\\frz{-r1:.2f}")
        if abs(o0 - o1) > 1e-4 and HIDDEN not in body:
            # (with hidden words / letters an animated \alpha would un-hide them: the fade steps instead)
            changes.append(f"\\alpha{_alpha(o1)}")
        if changes:
            tags.append(f"\\t(0,{dur_ms},{''.join(changes)})")
        out.append(f"Dialogue: {i},{_ass_time(a)},{_ass_time(b)},T{i},,0,0,0,,{{{''.join(tags)}}}{body}")
    return out


def build_ass(texts: list) -> str:
    lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {P.W}", f"PlayResY: {P.H}", "WrapStyle: 0",
             "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
             "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
             "Alignment, MarginL, MarginR, MarginV, Encoding"]
    lines += [_text_style(i, it) for i, it in enumerate(texts)]
    lines += ["", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for i, it in enumerate(texts):
        lines += text_events(i, it)
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ the ffmpeg graph

def _atempo(speed: float) -> list:
    """atempo only takes 0.5-2.0 at a time."""
    steps, s = [], speed
    while s > 2.0:
        steps.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        steps.append("atempo=0.5")
        s /= 0.5
    steps.append(f"atempo={s:.5f}")
    return steps


def _zoom_filter(item: dict, length: float) -> str:
    kf = item.get("kf") or {}
    zoom = [(t, v) for t, v in kf.get("zoom") or []] or [(0, 1.0)]
    panx = [(t, v) for t, v in kf.get("panx") or []] or [(0, 0.0)]
    pany = [(t, v) for t, v in kf.get("pany") or []] or [(0, 0.0)]
    if len(zoom) == 1 and abs(zoom[0][1] - 1) < 1e-6 and panx == [(0, 0.0)] and pany == [(0, 0.0)]:
        return ""
    z, px, py = P.pwl_expr(zoom, "it"), P.pwl_expr(panx, "it"), P.pwl_expr(pany, "it")
    # zoompan re-centres every frame (a per-frame scale + crop drifts)
    return (f",zoompan=z='max(1,{z})':x='max(0,min(iw-iw/zoom,iw/2-iw/zoom/2-({px})/zoom))'"
            f":y='max(0,min(ih-ih/zoom,ih/2-ih/zoom/2-({py})/zoom))':d=1:s={P.W}x{P.H}:fps={P.FPS}")


class _Graph:
    def __init__(self):
        self.inputs, self.chains, self.n = [], [], 0

    def add_input(self, *args) -> int:
        self.inputs += [str(a) for a in args]
        self.n += 1
        return self.n - 1

    def chain(self, text: str):
        self.chains.append(text)


def build(project: dict, job_dir: Path, ass_name: str, music: bool = True) -> tuple:
    """(ffmpeg input args, filter_complex, total seconds)."""
    pj = project
    total = P.duration(pj)
    g = _Graph()
    tracks = {k: [t for t in pj["tracks"] if t["kind"] == k] for k in P.TRACK_KINDS}
    main_track = tracks["main"][0]

    # --- main track: one input per clip (seeking in the input is fast)
    vlabels, alabels, lay = [], [], P.layout(pj)
    for i, (item, start, length, _) in enumerate(lay):
        asset = pj["assets"][item["asset"]]
        path = asset_path(asset, job_dir)
        if asset["kind"] == "image":
            idx = g.add_input("-loop", "1", "-t", f"{length:.3f}", "-i", path)
        else:
            idx = g.add_input("-ss", f"{item['in']:.3f}", "-t", f"{item['out'] - item['in']:.3f}", "-i", path)
        look = FILTERS[item["look"]][1] if item.get("look", "none") != "none" else ""
        v = (f"[{idx}:v]setpts=(PTS-STARTPTS)/{item['speed']:.5f},fps={P.FPS},"
             f"scale={P.W}:{P.H}:force_original_aspect_ratio=increase,crop={P.W}:{P.H},setsar=1"
             f"{_zoom_filter(item, length)}{(',' + look) if look else ''},format=yuv420p,"
             f"trim=duration={length:.3f},settb=AVTB")
        if main_track["hidden"]:
            v = f"color=c=black:s={P.W}x{P.H}:r={P.FPS}:d={length:.3f},format=yuv420p,settb=AVTB"
        g.chain(f"{v}[mv{i}]")
        has_audio = asset["kind"] == "video" and asset["audio"] and not main_track["muted"] and item["volume"] > 0
        if has_audio:
            a = (f"[{idx}:a]asetpts=PTS-STARTPTS,{','.join(_atempo(item['speed']))},"
                 f"aresample=44100,aformat=channel_layouts=stereo,volume={item['volume']:.3f},"
                 f"atrim=duration={length:.3f},apad=whole_dur={length:.3f}")
        else:
            a = f"anullsrc=channel_layout=stereo:sample_rate=44100,atrim=duration={length:.3f}"
        g.chain(f"{a}[ma{i}]")
        vlabels.append(f"[mv{i}]")
        alabels.append(f"[ma{i}]")
    vcur, acur, cur_len = vlabels[0], alabels[0], lay[0][2]
    for i in range(1, len(lay)):
        item, _, length, d = lay[i]
        if d > 0:
            kind = P.TRANSITIONS[item["transition"]["type"]]
            g.chain(f"{vcur}{vlabels[i]}xfade=transition={kind}:duration={d:.3f}:offset={cur_len - d:.3f}[xv{i}]")
            g.chain(f"{acur}{alabels[i]}acrossfade=d={d:.3f}[xa{i}]")
            cur_len += length - d
        else:
            g.chain(f"{vcur}{vlabels[i]}concat=n=2:v=1:a=0[xv{i}]")
            g.chain(f"{acur}{alabels[i]}concat=n=2:v=0:a=1[xa{i}]")
            cur_len += length
        vcur, acur = f"[xv{i}]", f"[xa{i}]"
    look = FILTERS[pj["look"]["filter"]][1]
    g.chain(f"{vcur}{look if look else 'null'}[base]")
    vcur = "[base]"
    voice = [acur]

    # --- overlays, bottom to top
    k = 0
    for track in tracks["overlay"]:
        if track["hidden"]:
            continue
        for it in track["items"]:
            asset = pj["assets"][it["asset"]]
            path = asset_path(asset, job_dir)
            if asset["kind"] == "image":
                idx = g.add_input("-loop", "1", "-t", f"{it['dur']:.3f}", "-i", path)
            else:
                idx = g.add_input("-ss", f"{it['in']:.3f}", "-t", f"{it['dur']:.3f}", "-i", path)
            base = {"x": it["x"], "y": it["y"], "scale": 1.0, "rotation": it["rotation"], "opacity": it["opacity"]}
            mot = P.motion(it, base)
            s, e = it["start"], it["start"] + it["dur"]
            chain = [f"[{idx}:v]setpts=PTS-STARTPTS+{s:.3f}/TB,fps={P.FPS},format=rgba"]
            if it["fit"] == "fill":
                chain.append(f"scale={P.W}:{P.H}:force_original_aspect_ratio=increase,crop={P.W}:{P.H}")
                width_expr = None
            else:
                width_expr = f"{it['w']:.2f}*({P.pwl_expr(mot['scale'])})"
            if width_expr:
                # "width" scaling stretches sideways only (progress bars); otherwise keep the shape
                height = "ih" if it.get("scale_mode") == "width" else "max(8,trunc(ih*ow/iw/2)*2)"
                if len(mot["scale"]) == 1:
                    chain.append(f"scale=w={max(8, it['w'] * mot['scale'][0][1]):.0f}:h='{height}'")
                else:
                    chain.append(f"scale=w='max(8,trunc({width_expr}/2)*2)':h='{height}':eval=frame")
                    # A picture whose size changes every frame, with anything drawn after it,
                    # crashes ffmpeg (heap corruption): put it in a fixed-size see-through box
                    # as big as it ever gets, so every later step sees one size.
                    top = max(v for _, v in mot["scale"])
                    box_w = max(8, int(math.ceil(it["w"] * top / 2)) * 2) + 2
                    aspect = (asset["h"] / asset["w"]) if asset.get("w") and asset.get("h") else 1.0
                    box_h = (max(8, asset.get("h") or 8) if it.get("scale_mode") == "width"
                             else max(8, int(math.ceil(box_w * aspect / 2)) * 2) + 2)
                    chain.append(f"pad={box_w}:{box_h}:(ow-iw)/2:(oh-ih)/2:color=0x00000000:eval=frame")
            if len(mot["rotation"]) > 1 or abs(mot["rotation"][0][1]) > 1e-6:
                chain.append(f"rotate=a='({P.pwl_expr(mot['rotation'])})*PI/180':c=none"
                             f":ow='hypot(iw,ih)':oh='hypot(iw,ih)'")
            opacity = mot["opacity"]
            if len(opacity) > 1:
                chain.append(f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='alpha(X,Y)*({P.pwl_expr(opacity, 'T')})'")
            elif opacity[0][1] < 0.999:
                chain.append(f"colorchannelmixer=aa={opacity[0][1]:.4f}")
            g.chain(f"{','.join(chain)}[ov{k}]")
            x = P.pwl_expr(mot["x"]) if len(mot["x"]) > 1 else f"{mot['x'][0][1]:.2f}"
            y = P.pwl_expr(mot["y"]) if len(mot["y"]) > 1 else f"{mot['y'][0][1]:.2f}"
            g.chain(f"{vcur}[ov{k}]overlay=x='({x})-overlay_w/2':y='({y})-overlay_h/2':eval=frame"
                    f":enable='between(t,{s:.3f},{e:.3f})':eof_action=pass[ovout{k}]")
            vcur = f"[ovout{k}]"
            if asset["kind"] == "video" and asset["audio"] and it["volume"] > 0 and not track["muted"]:
                delay = int(round(s * 1000))
                g.chain(f"[{idx}:a]asetpts=PTS-STARTPTS,aresample=44100,aformat=channel_layouts=stereo,"
                        f"volume={it['volume']:.3f},adelay={delay}|{delay}[ova{k}]")
                voice.append(f"[ova{k}]")
            k += 1

    # --- text and captions
    if ass_name:
        g.chain(f"{vcur}subtitles={ass_name}:fontsdir=fonts[txt]")
        vcur = "[txt]"
    tail = []
    if pj["look"]["fade_in"]:
        tail.append(f"fade=t=in:st=0:d={pj['look']['fade_in']}")
    if pj["look"]["fade_out"]:
        tail.append(f"fade=t=out:st={max(0.0, total - pj['look']['fade_out']):.3f}:d={pj['look']['fade_out']}")
    height = pj["export"]["height"]
    if height != P.H:
        tail.append(f"scale={height * P.W // P.H // 2 * 2}:{height}:flags=lanczos")
    if pj["export"]["fps"] != P.FPS:
        tail.append(f"fps={pj['export']['fps']}")
    tail.append("format=yuv420p")
    g.chain(f"{vcur}{','.join(tail)}[v]")

    # --- sound
    music_labels, sfx_labels = [], []
    m = 0
    for track in tracks["audio"]:
        if track["muted"]:
            continue
        for it in track["items"]:
            if it.get("music") and not music:
                continue
            asset = pj["assets"][it["asset"]]
            path = asset_path(asset, job_dir)
            loop = asset["duration"] and it["in"] + it["dur"] > asset["duration"] + 0.05
            idx = g.add_input(*(["-stream_loop", "-1"] if loop else []), "-ss", f"{it['in']:.3f}",
                              "-t", f"{it['dur']:.3f}", "-i", path)
            chain = [f"[{idx}:a]asetpts=PTS-STARTPTS,aresample=44100,aformat=channel_layouts=stereo"]
            vol = (it.get("kf") or {}).get("volume")
            if vol:
                chain.append(f"volume=eval=frame:volume='{P.pwl_expr([(t, v) for t, v in vol])}'")
            else:
                chain.append(f"volume={it['volume']:.3f}")
            if it["fade_in"]:
                chain.append(f"afade=t=in:st=0:d={it['fade_in']}")
            if it["fade_out"]:
                chain.append(f"afade=t=out:st={max(0.0, it['dur'] - it['fade_out']):.3f}:d={it['fade_out']}")
            delay = int(round(it["start"] * 1000))
            chain.append(f"adelay={delay}|{delay}")
            g.chain(f"{','.join(chain)}[au{m}]")
            (music_labels if it.get("duck") and pj["ducking"] else sfx_labels).append(f"[au{m}]")
            m += 1
    if len(voice) > 1:
        g.chain(f"{''.join(voice)}amix=inputs={len(voice)}:duration=longest:normalize=0[voice]")
    else:
        g.chain(f"{voice[0]}anull[voice]")
    mix = ["[voicemix]"]
    if music_labels:
        g.chain("[voice]asplit=2[voicemix][sc]")
        if len(music_labels) > 1:
            g.chain(f"{''.join(music_labels)}amix=inputs={len(music_labels)}:duration=longest:normalize=0[musicbus]")
        else:
            g.chain(f"{music_labels[0]}anull[musicbus]")
        g.chain("[musicbus][sc]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[ducked]")
        mix.append("[ducked]")
    else:
        g.chain("[voice]anull[voicemix]")
    mix += sfx_labels
    tail = [f"apad=whole_dur={total:.3f}", f"atrim=duration={total:.3f}"]
    if pj["look"]["fade_in"]:
        tail.append(f"afade=t=in:st=0:d={pj['look']['fade_in']}")
    if pj["look"]["fade_out"]:
        tail.append(f"afade=t=out:st={max(0.0, total - pj['look']['fade_out']):.3f}:d={pj['look']['fade_out']}")
    tail.append("loudnorm=I=-14:TP=-1.5:LRA=11" if total >= 3.5 else "alimiter=limit=0.9")
    g.chain(f"{''.join(mix)}amix=inputs={len(mix)}:duration=longest:normalize=0,{','.join(tail)}[a]")
    return g.inputs, ";".join(g.chains), total


def _texts(project: dict) -> list:
    return [it for tr in project["tracks"] if tr["kind"] in ("text", "caption") and not tr["hidden"]
            for it in tr["items"]]


def render(project: dict, job_dir: Path, out_name: str = "final.mp4", music: bool = True) -> Path:
    """Render the project to job_dir/out_name (via a temp file, so a failed render never
    leaves a broken final video). `music=False` leaves out audio marked as music (TikTok)."""
    job_dir = Path(job_dir)
    texts = _texts(project)
    ass_name = None
    if texts:
        ass_name = "edit2.ass"
        (job_dir / ass_name).write_text(build_ass(texts), encoding="utf-8")
        fonts = job_dir / "fonts"  # subtitles= wants a plain relative fontsdir
        fonts.mkdir(exist_ok=True)
        for font in FONT_DIR.glob("*.ttf"):
            if not (fonts / font.name).exists():
                shutil.copyfile(font, fonts / font.name)
    inputs, graph, total = build(project, job_dir, ass_name, music=music)
    tmp = job_dir / f"render_tmp_{out_name}"
    crf = P.QUALITY[project["export"]["quality"]]
    graph_file = job_dir / "render2_graph.txt"  # long graphs overflow the Windows command line
    graph_file.write_text(graph, encoding="utf-8")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-/filter_complex", graph_file.name,
           "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
           "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-t", f"{total:.3f}", "-movflags", "+faststart",
           tmp.name]
    try:
        media.run(cmd, cwd=job_dir, timeout=3600)
        media.replace(tmp, job_dir / out_name)
    finally:
        tmp.unlink(missing_ok=True)
    return job_dir / out_name


def summary(project: dict) -> dict:
    """A short description for the clip page."""
    main = P.main_items(project)
    count = {k: sum(len(t["items"]) for t in project["tracks"] if t["kind"] == k) for k in P.TRACK_KINDS}
    return {"length": P.duration(project), "clips": len(main), "texts": count["text"],
            "captions": count["caption"], "overlays": count["overlay"], "sounds": count["audio"],
            "look": project["look"]["filter"]}


def dumps(project: dict) -> str:
    return json.dumps(project, separators=(",", ":"))


__all__ = ["render", "build", "build_ass", "asset_path", "emoji_png", "make_shape", "summary", "RenderError",
           "math"]
