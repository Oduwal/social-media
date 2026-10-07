"""HyperFrames Studio as Clipper's video editor (https://github.com/heygen-com/hyperframes).

Each clip gets a HyperFrames project in data/jobs/<id>/studio/<project>/, written from the
clip's Clipper edit (trim, look, zoom, fades, voice and music volume with ducking, texts).
You edit it in Studio's timeline, which runs locally and is shown inside the dashboard;
"Use this version" renders it to final.mp4 with HyperFrames (headless Chrome + FFmpeg).
Your Studio changes are kept: Clipper only rewrites the project when you ask to start over.
The TikTok version (when you pick a trending sound) is the same project without the music.

One Studio server runs at a time, for the clip being edited.
"""

import html
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import requests

from . import config, media, render

log = logging.getLogger(__name__)

VERSION = "0.8.140"  # pinned: the composition rules below were written against this release
STATE_PATH = config.DATA_DIR / "studio_server.json"
CLIP_FILE = "clip.mp4"
MUSIC_DIR = "assets/music"
TIKTOK_PAGE = "index_tiktok.html"
_lock = threading.Lock()

# Clipper's text styles as CSS: (font stack, outline as text-shadow, box background)
# (single quotes only: these go inside a style="..." attribute)
_FONTS = {"Arial Black": "'Arial Black', Arial, sans-serif", "Arial": "Arial, sans-serif",
          "Segoe UI Semibold": "'Segoe UI Semibold', 'Segoe UI', sans-serif"}
_TOP = {"top": "top: 260px;", "middle": "top: 50%; transform: translateY(-50%);", "bottom": "bottom: 420px;"}


class StudioError(RuntimeError):
    pass


def port(cfg: dict) -> int:
    return int((cfg.get("studio") or {}).get("port") or 3020)


def project_dir(job_dir: Path) -> Path:
    # the folder name is the project's name in Studio; one per clip keeps their settings apart
    return job_dir / "studio" / f"clip-{job_dir.name}"


def exists(job_dir: Path) -> bool:
    return (project_dir(job_dir) / "index.html").exists()


# ------------------------------------------------------------------ the project

def _link(src: Path, dst: Path) -> None:
    """dst becomes the same file as src (a hard link: no extra disk space), or a copy."""
    if dst.exists():
        try:
            if os.path.samefile(src, dst):
                return
        except OSError:
            pass
        dst.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def link_media(job_dir: Path) -> Path:
    """Put the clip and the music library into the project (refreshed if the clip was redone)."""
    proj = project_dir(job_dir)
    _link(job_dir / "transformed.mp4", proj / CLIP_FILE)
    if config.MUSIC_DIR.exists():
        for track in render.music_names():
            _link(config.MUSIC_DIR / track, proj / MUSIC_DIR / track)
    return proj


def _speech(clip: Path, start: float, end: float) -> list:
    """(start, end) of talking within [start, end], relative to start: the gaps between silences."""
    quiet = [(max(a, start), min(b, end)) for a, b in media.silences(clip) if b > start and a < end]
    spans, cursor = [], start
    for a, b in sorted(quiet):
        if a - cursor > 0.15:
            spans.append((cursor - start, a - start))
        cursor = max(cursor, b)
    if end - cursor > 0.15:
        spans.append((cursor - start, end - start))
    return spans


def _music_lane(edit: dict, length: float, speech: list) -> dict:
    """Volume over time for the music: fades in/out, and dips under the voice (ducking)."""
    music = edit["music"]
    vol, low = music["volume"], music["volume"] * 0.35
    level = (lambda t: low if music["ducking"] and any(a <= t <= b for a, b in speech) else vol)
    points = {0.0: 0.0 if music["fade_in"] else level(0.0)}
    if music["ducking"]:
        for a, b in speech[:60]:
            for t, v in ((a - 0.2, vol), (a, low), (b, low), (b + 0.3, vol)):
                if 0 < t < length:
                    points[round(t, 3)] = v
    if music["fade_in"]:
        points[round(music["fade_in"], 3)] = level(music["fade_in"])
    if music["fade_out"]:
        points[round(length - music["fade_out"], 3)] = level(length - music["fade_out"])
        points[round(length, 3)] = 0.0
    lane = [{"t": t, "v": round(v, 4)} for t, v in sorted(points.items()) if 0 <= t <= length]
    return {"version": 1, "lanes": [{"target": "volume", "points": lane}]}


def _text_css(text: dict) -> str:
    font, colour, outline, width, border, shadow, bold = render.TEXT_STYLES[text["style"]]
    css = [f"position: absolute; left: 60px; right: 60px; {_TOP[text['position']]}",
           "text-align: center; line-height: 1.1;",
           f"font-family: {_FONTS.get(font, font)}; font-size: {text['size']}px;",
           f"font-weight: {900 if bold else 600}; color: {text['color']};"]
    if border == 3:  # box style
        css.append(f"background: {outline}; padding: 10px 24px; border-radius: 12px;"
                   "width: fit-content; margin: 0 auto;")
    elif width:
        css.append(f"-webkit-text-stroke: {max(1, width // 2)}px {outline}; paint-order: stroke fill;")
    if shadow:
        css.append(f"text-shadow: 0 {shadow * 2}px {shadow * 6}px rgba(0, 0, 0, 0.7);")
    return " ".join(css)


def build_html(edit: dict, clip_seconds: float, speech: list, project_name: str) -> str:
    """A HyperFrames composition (1080x1920) equivalent to Clipper's edit."""
    start, end = edit["trim"]["start"], edit["trim"]["end"]
    length = round(end - start, 3)
    css_filter = render.preview_filters().get(edit["filter"], "none")
    voice_lane = None
    if edit["fade_in"] or edit["fade_out"]:
        pts = [{"t": 0, "v": 0 if edit["fade_in"] else 1}]
        if edit["fade_in"]:
            pts.append({"t": edit["fade_in"], "v": 1})
        if edit["fade_out"]:
            pts += [{"t": round(length - edit["fade_out"], 3), "v": 1}, {"t": length, "v": 0}]
        voice_lane = {"version": 1, "lanes": [{"target": "volume", "points": pts}]}

    clips = [
        f'<video id="a-roll" class="clip" src="{CLIP_FILE}" playsinline data-has-audio="true" '
        f'data-start="0" data-media-start="{start:.3f}" data-duration="{length}" data-track-index="0" '
        f'data-volume="{edit["voice_volume"]}"'
        + (f" data-automation='{json.dumps(voice_lane)}'" if voice_lane else "")
        + f' style="position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; '
          f'filter: {css_filter}"></video>'
    ]
    track = edit["music"]["track"]
    if track:
        lane = _music_lane(edit, length, speech)
        clips.append(
            f'<audio id="music" class="clip" data-clipper="music" src="{MUSIC_DIR}/{html.escape(track)}" '
            f'data-start="0" data-media-start="{edit["music"]["offset"]:.3f}" data-duration="{length}" '
            f'data-track-index="10" data-volume="{edit["music"]["volume"]}" '
            f"data-automation='{json.dumps(lane)}'></audio>")
    tweens = []
    for i, text in enumerate(edit["texts"], start=1):
        clips.append(
            f'<div id="text-{i}" class="clip" data-start="{text["start"]:.3f}" '
            f'data-duration="{round(text["end"] - text["start"], 3)}" data-track-index="1" '
            f'style="{html.escape(_text_css(text))}">{html.escape(text["text"])}</div>')
        if text["animation"] == "pop":
            tweens.append(f'tl.from("#text-{i}", {{scale: 0.6, opacity: 0, duration: 0.25, '
                          f'ease: "back.out(2)"}}, {text["start"]:.3f});')
        elif text["animation"] == "fade":
            tweens.append(f'tl.from("#text-{i}", {{opacity: 0, duration: 0.3}}, {text["start"]:.3f});')
    if edit["zoom"] != "none":
        big = 1 + render.ZOOM_AMOUNT
        a, b = (1, big) if edit["zoom"] == "slow_in" else (big, 1)
        tweens.append(f'tl.fromTo("#a-roll", {{scale: {a}}}, {{scale: {b}, duration: {length}, ease: "none"}}, 0);')
    if edit["fade_in"]:
        tweens.append(f'tl.from("#a-roll", {{opacity: 0, duration: {edit["fade_in"]}}}, 0);')
    if edit["fade_out"]:
        tweens.append(f'tl.to("#a-roll", {{opacity: 0, duration: {edit["fade_out"]}}}, '
                      f'{length - edit["fade_out"]:.3f});')

    body = "\n      ".join(clips)
    script = "\n      ".join(tweens)
    return f"""<!doctype html>
<html lang="en" data-resolution="portrait">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=1080, height=1920" />
    <title>{html.escape(project_name)}</title>
    <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
    <style>
      * {{ margin: 0; padding: 0; box-sizing: border-box; }}
      html, body {{ width: 1080px; height: 1920px; overflow: hidden; background: #000; }}
      body {{ font-family: Arial, sans-serif; }}
    </style>
  </head>
  <body>
    <div id="root" data-composition-id="main" data-start="0" data-duration="{length}"
         data-width="1080" data-height="1920">
      {body}
    </div>
    <script>
      window.__timelines = window.__timelines || {{}};
      const tl = gsap.timeline({{ paused: true }});
      {script}
      window.__timelines["main"] = tl;
    </script>
  </body>
</html>
"""


def write_project(job_dir: Path, edit: dict, overwrite: bool = False) -> Path:
    """Create the clip's Studio project from its Clipper edit. An existing project (your
    Studio work) is kept unless `overwrite`; its media is refreshed either way."""
    proj = project_dir(job_dir)
    proj.mkdir(parents=True, exist_ok=True)
    link_media(job_dir)
    page = proj / "index.html"
    if overwrite or not page.exists():
        clip = job_dir / "transformed.mp4"
        speech = _speech(clip, edit["trim"]["start"], edit["trim"]["end"]) \
            if edit["music"]["track"] and edit["music"]["ducking"] else []
        page.write_text(build_html(edit, media.duration(clip), speech, proj.name), encoding="utf-8")
    meta = {
        "hyperframes.json": {"$schema": "https://hyperframes.heygen.com/schema/hyperframes.json",
                             "paths": {"blocks": "compositions", "components": "compositions/components",
                                       "assets": "assets"}, "media": {"autoProxy": True}},
        "meta.json": {"id": proj.name, "name": proj.name},
        "package.json": {"name": proj.name, "private": True, "type": "module",
                         "scripts": {"dev": f"npx --yes hyperframes@{VERSION} preview",
                                     "render": f"npx --yes hyperframes@{VERSION} render"}},
    }
    for name, content in meta.items():
        if not (proj / name).exists():
            (proj / name).write_text(json.dumps(content, indent=2), encoding="utf-8")
    return proj


def has_music(proj: Path) -> bool:
    return 'data-clipper="music"' in (proj / "index.html").read_text(encoding="utf-8")


def without_music(page: str) -> str:
    """The composition with Clipper's music removed (for TikTok, where you add a trending sound)."""
    return re.sub(r'<audio\b[^>]*data-clipper="music"[^>]*>\s*</audio>', "", page)


# ------------------------------------------------------------------ the CLI

def _npx() -> str:
    path = shutil.which("npx") or shutil.which("npx.cmd")
    if not path:
        raise StudioError("Node.js isn't installed (needed for HyperFrames). Install it with:  "
                          "winget install OpenJS.NodeJS.LTS")
    return path


def _run(args: list, timeout: float, cwd: Path = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "HYPERFRAMES_NO_TELEMETRY": "1", "HYPERFRAMES_SKIP_SKILLS": "1"}
    result = subprocess.run([_npx(), "--yes", f"hyperframes@{VERSION}", *[str(a) for a in args]],
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=timeout, cwd=cwd, env=env)
    if result.returncode != 0:
        noise = re.compile(r"^npm notice.*$", re.M)  # npx's own chatter
        said = "\n".join(t for t in (noise.sub("", result.stdout or "").strip(),
                                     noise.sub("", result.stderr or "").strip()) if t)
        raise StudioError(f"HyperFrames failed: {said[-1200:]}")
    return result


def _serving(cfg: dict, proj: Path) -> bool:
    try:
        projects = requests.get(f"http://127.0.0.1:{port(cfg)}/api/projects", timeout=2).json()["projects"]
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return False
    return any(Path(p.get("dir", "")).resolve() == proj.resolve() for p in projects)


def studio_url(cfg: dict, proj: Path) -> str:
    return f"http://localhost:{port(cfg)}/#project/{proj.name}"


def open_studio(cfg: dict, proj: Path) -> str:
    """Make sure Studio is running for this project (stopping the previous clip's) and
    return its address."""
    with _lock:
        if _serving(cfg, proj):
            return studio_url(cfg, proj)
        try:
            previous = json.loads(STATE_PATH.read_text(encoding="utf-8")).get("dir")
        except (OSError, ValueError):
            previous = None
        if previous and Path(previous).exists():
            try:
                _run(["preview", previous, "--stop"], timeout=60)
            except (StudioError, subprocess.TimeoutExpired) as err:
                log.info("Couldn't stop the previous Studio (%s)", err)
        _run(["preview", proj, "--background", "--no-open", "--port", port(cfg)], timeout=300)
        STATE_PATH.write_text(json.dumps({"dir": str(proj), "port": port(cfg)}), encoding="utf-8")
        for _ in range(60):
            if _serving(cfg, proj):
                return studio_url(cfg, proj)
            time.sleep(1)
        raise StudioError(f"Studio didn't start on port {port(cfg)} (is something else using it? "
                          f"set studio: port in config.yaml)")


def check(proj: Path) -> str:
    """HyperFrames' own validation; returns its report (raises if it found errors)."""
    return _run(["check", proj], timeout=300).stdout


def render_project(job_dir: Path, out_name: str = "final.mp4", music: bool = True) -> Path:
    """Render the clip's Studio project to job_dir/out_name (via a temp file)."""
    proj = project_dir(job_dir)
    if not (proj / "index.html").exists():
        raise StudioError("This clip has no Studio edit yet: open it in the editor first")
    link_media(job_dir)  # the clip may have been redone since
    page = "index.html"
    if not music:
        (proj / TIKTOK_PAGE).write_text(without_music((proj / "index.html").read_text(encoding="utf-8")),
                                        encoding="utf-8")
        page = TIKTOK_PAGE
    out = job_dir / out_name
    tmp = job_dir / f"render_tmp_{out_name}"
    tmp.unlink(missing_ok=True)
    try:
        _run(["render", proj, "-c", page, "-o", tmp.resolve(), "--quality", "looks", "--quiet"], timeout=3600)
        if media.duration(tmp) < 1:
            raise StudioError("HyperFrames made an empty video")
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)
        if not music:
            (proj / TIKTOK_PAGE).unlink(missing_ok=True)
    return out
