"""Server side of the editor: the clip's project (project.py), its media, uploads, the
timeline's frame strips and waveforms, drafts, and the AI "describe a change" helper."""

import json
import logging
import math
import re
import struct
import subprocess
import time
from pathlib import Path

from . import config, media, project as P, render2

log = logging.getLogger(__name__)

DRAFT = "project_draft.json"
UPLOADS = render2.UPLOAD_DIR
CACHE = "editor_cache"
MAX_UPLOAD = 400 * 1024 * 1024
KINDS = {".mp4": "video", ".mov": "video", ".webm": "video", ".mkv": "video", ".m4v": "video",
         ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".gif": "image",
         ".mp3": "audio", ".wav": "audio", ".m4a": "audio", ".aac": "audio", ".ogg": "audio", ".flac": "audio"}


class EditorError(ValueError):
    pass


# ------------------------------------------------------------------ assets

def _probe_asset(path: Path, kind: str, src: str, name: str) -> dict:
    asset = {"kind": kind, "src": src, "name": name, "duration": 0.0, "w": 0, "h": 0, "audio": False}
    if kind == "image":
        from PIL import Image
        with Image.open(path) as img:
            asset["w"], asset["h"] = img.size
        return asset
    info = media.probe(path)
    asset["duration"] = round(float(info["format"].get("duration") or 0), 3)
    for s in info["streams"]:
        if s.get("codec_type") == "video" and kind == "video" and not asset["w"]:
            asset["w"], asset["h"] = int(s.get("width") or 0), int(s.get("height") or 0)
        if s.get("codec_type") == "audio":
            asset["audio"] = True
    return asset


def _cached_probe(path: Path, kind: str, src: str, name: str, job_dir: Path) -> dict:
    cache_dir = job_dir / CACHE
    cache_dir.mkdir(exist_ok=True)
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", src)
    cache = cache_dir / f"probe_{key}.json"
    stamp = path.stat().st_mtime
    if cache.exists():
        saved = json.loads(cache.read_text(encoding="utf-8"))
        if saved.get("mtime") == stamp:
            return saved["asset"]
    asset = _probe_asset(path, kind, src, name)
    cache.write_text(json.dumps({"mtime": stamp, "asset": asset}), encoding="utf-8")
    return asset


def known_assets(job_dir: Path) -> dict:
    """Everything on disk the editor may use: the clip (albino), the original cut, uploads
    and the music library. id -> asset."""
    out = {}
    for aid, name, label in (("clip", "transformed.mp4", "This clip"), ("original", "highlight.mp4", "Original (before albino)")):
        if (job_dir / name).exists():
            out[aid] = _cached_probe(job_dir / name, "video", aid, label, job_dir)
    uploads = job_dir / UPLOADS
    if uploads.exists():
        for f in sorted(uploads.iterdir()):
            kind = KINDS.get(f.suffix.lower())
            if kind and f.is_file():
                try:
                    out[f"upload:{f.name}"] = _cached_probe(f, kind, f"upload/{f.name}", f.name, job_dir)
                except Exception as err:  # noqa: BLE001 - an unreadable upload is just skipped
                    log.warning("Couldn't read upload %s: %s", f.name, err)
    if config.MUSIC_DIR.exists():
        from . import music as music_lib
        for track in music_lib.list_tracks(config.MUSIC_DIR):
            path = config.MUSIC_DIR / track["file"]
            try:
                asset = _cached_probe(path, "audio", f"music/{track['file']}", track.get("title") or path.stem, job_dir)
            except Exception:  # noqa: BLE001
                continue
            asset["mood"] = track.get("mood", "")
            out[f"music:{track['file']}"] = asset
    return out


def clean(raw: dict, job_dir: Path) -> dict:
    """Validate a project from the browser. Files may only come from this clip's folder or
    the music library; emoji and shapes are made on demand."""
    known = known_assets(job_dir)
    raw = dict(raw or {})
    allowed = {}
    for aid, asset in (raw.get("assets") or {}).items():
        if aid in known:
            allowed[aid] = known[aid]
        elif isinstance(asset, dict) and str(asset.get("src", "")).startswith(("emoji/", "shape/")):
            allowed[aid] = asset
    raw["assets"] = allowed
    try:
        return P.clean(raw, known)
    except P.ProjectError as err:
        raise EditorError(str(err)) from err


def asset_file(job_dir: Path, asset_id: str) -> Path:
    asset = known_assets(job_dir).get(asset_id)
    if asset is None and asset_id.startswith("emoji:"):
        asset = P.clean_asset({"kind": "image", "src": "emoji/" + asset_id[6:]})
    if asset is None:
        raise EditorError("Unknown media")
    return render2.asset_path(asset, job_dir)


def upload(job_dir: Path, filename: str, data: bytes) -> tuple:
    """Save an upload into the clip's folder; returns (asset_id, asset)."""
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(filename or "upload").name).strip(" .")[:80] or "upload"
    kind = KINDS.get(Path(name).suffix.lower())
    if not kind:
        raise EditorError("Only videos, images and sound files can be added")
    if len(data) > MAX_UPLOAD:
        raise EditorError(f"That file is over {MAX_UPLOAD // (1024 * 1024)} MB")
    folder = job_dir / UPLOADS
    folder.mkdir(exist_ok=True)
    stem, suffix, n = Path(name).stem, Path(name).suffix, 1
    path = folder / name
    while path.exists():
        n += 1
        path = folder / f"{stem}-{n}{suffix}"
    path.write_bytes(data)
    try:
        asset = _probe_asset(path, kind, f"upload/{path.name}", path.name)
    except Exception as err:
        path.unlink(missing_ok=True)
        raise EditorError(f"Couldn't read that file: {err}") from err
    return f"upload:{path.name}", asset


# ------------------------------------------------------------------ the project

def load(job: dict, cfg: dict) -> tuple:
    """(project, draft_saved_at or None). A clip never opened in this editor gets one made
    from its earlier edit."""
    from . import worker

    job_dir = config.job_dir(job["video_id"])
    known = known_assets(job_dir)
    draft = job_dir / DRAFT
    if draft.exists():
        try:
            saved = json.loads(draft.read_text(encoding="utf-8"))
            return P.clean(saved["project"], known), saved.get("saved_at")
        except (ValueError, KeyError, P.ProjectError) as err:
            log.warning("[%s] ignoring a bad draft: %s", job["video_id"], err)
    edit = job.get("edit") or {}
    if edit.get("version") == P.VERSION:
        try:
            return P.clean(edit, known), None
        except P.ProjectError as err:
            log.warning("[%s] saved project no longer fits (%s); starting from the clip", job["video_id"], err)
    v1 = worker.current_edit(job, cfg)
    track = (v1.get("music") or {}).get("track")
    music_asset = (f"music:{track}", known[f"music:{track}"]) if track and f"music:{track}" in known else None
    return P.clean(P.from_v1(v1, known["clip"], music_asset), known), None


def save_draft(job_dir: Path, project: dict) -> float:
    stamp = time.time()
    tmp = job_dir / (DRAFT + ".tmp")
    tmp.write_text(json.dumps({"saved_at": stamp, "project": project}), encoding="utf-8")
    tmp.replace(job_dir / DRAFT)
    return stamp


def drop_draft(job_dir: Path) -> None:
    (job_dir / DRAFT).unlink(missing_ok=True)


# ------------------------------------------------------------------ timeline helpers

def filmstrip(job_dir: Path, asset_id: str) -> Path:
    """One small frame per second (or fewer for long files) side by side, for the timeline."""
    src = asset_file(job_dir, asset_id)
    asset = known_assets(job_dir).get(asset_id) or {}
    if asset.get("kind") != "video":
        raise EditorError("Only videos have a frame strip")
    cache = job_dir / CACHE
    cache.mkdir(exist_ok=True)
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", asset_id)
    out = cache / f"strip_{key}_{int(src.stat().st_mtime)}.jpg"
    if out.exists():
        return out
    dur = max(1.0, asset.get("duration") or media.duration(src))
    every = max(1.0, dur / 120)  # at most 120 frames
    count = int(math.ceil(dur / every))
    media.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-vf",
               f"fps=1/{every:.4f},scale=54:96:force_original_aspect_ratio=increase,crop=54:96,tile={count}x1",
               "-frames:v", "1", "-q:v", "5", out], timeout=600)
    (cache / f"strip_{key}.json").write_text(json.dumps({"every": every, "count": count}), encoding="utf-8")
    return out


def strip_info(job_dir: Path, asset_id: str) -> dict:
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", asset_id)
    path = job_dir / CACHE / f"strip_{key}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def peaks(job_dir: Path, asset_id: str, per_second: int = 40) -> dict:
    """Loudness peaks (0-1), per_second of them, for drawing waveforms."""
    src = asset_file(job_dir, asset_id)
    cache = job_dir / CACHE
    cache.mkdir(exist_ok=True)
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", asset_id)
    out = cache / f"peaks_{key}_{int(src.stat().st_mtime)}.json"
    if out.exists():
        return json.loads(out.read_text(encoding="utf-8"))
    rate = 2000
    proc = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", str(rate),
                           "-f", "s16le", "-"], capture_output=True, timeout=600)
    raw = proc.stdout
    samples = struct.unpack(f"<{len(raw) // 2}h", raw[: len(raw) // 2 * 2]) if raw else ()
    step = rate // per_second
    values = [max((abs(v) for v in samples[i:i + step]), default=0) / 32768 for i in range(0, len(samples), step)]
    top = max(values, default=0) or 1
    data = {"per_second": per_second, "peaks": [round(min(1.0, v / top), 3) for v in values]}
    out.write_text(json.dumps(data), encoding="utf-8")
    return data


# ------------------------------------------------------------------ captions on the timeline

def caption_items(job_dir: Path, project: dict, style: dict, reference: str = "") -> list:
    """Word-timed captions for the edited timeline: each spoken word of the clip is placed
    where its moment lands after cuts and speed changes, then grouped into short captions."""
    from . import captions

    words = []
    for item, start, length, _ in P.layout(project):
        src = project["assets"].get(item["asset"], {}).get("src")
        if src not in ("clip", "original"):  # only the clip's own sound is transcribed
            continue
        for w in captions.transcribe(job_dir, reference):
            if w["start"] >= item["in"] and w["end"] <= item["out"] + 0.05:
                to_t = (lambda s, it=item, st=start: st + (s - it["in"]) / it["speed"])
                words.append({"text": w["text"], "start": round(to_t(w["start"]), 3),
                              "end": round(min(to_t(w["end"]), start + length), 3)})
    words.sort(key=lambda w: w["start"])
    total = P.duration(project)
    groups = captions.group(words)
    items = []
    for i, chunk in enumerate(groups):
        start, end = chunk[0]["start"], chunk[-1]["end"]
        nxt = groups[i + 1][0]["start"] if i + 1 < len(groups) else total
        if nxt - end < 0.4:
            end = nxt
        end = min(max(end, start + 0.4), total)
        item = {**{k: v for k, v in style.items() if k not in ("id", "text", "start", "dur")},
                "text": " ".join(w["text"] for w in chunk), "start": round(start, 3), "dur": round(end - start, 3),
                "words": [[round(w["start"] - start, 3), round(min(w["end"], end) - start, 3)] for w in chunk],
                "auto": True}
        items.append(item)
    return items


# ------------------------------------------------------------------ AI helper

ASSIST_PROMPT = """You edit short vertical videos (1080x1920) in a timeline editor. Below is the
project as JSON (format described after it) and a change the user asked for. Reply with the
WHOLE updated project as JSON, changing only what the request needs. Keep every id, asset and
time you don't need to change. Times are seconds. Coordinates are pixels in a 1080x1920 frame,
x/y being the centre of an item. Keyframes are [[seconds from the item's start, value], ...].

Format: {format}

Allowed values: fonts {fonts}; animations {anims}; text effects {effects}; transitions
{transitions}; looks {looks}.

Request: {request}

Project:
{project}"""


def assist(project: dict, request: str, job_dir: Path, cfg: dict) -> dict:
    """Ask Gemini to apply a plain-language change; the answer is validated like any save."""
    from google.genai import types

    from . import gemini

    if not request.strip():
        raise EditorError("Say what you'd like to change")
    prompt = ASSIST_PROMPT.format(
        format=P.__doc__, fonts=list(P.FONTS), anims=list(P.ANIMS), effects=list(P.EFFECTS),
        transitions=list(P.TRANSITIONS), looks=list(P.FILTERS), request=request.strip()[:1000],
        project=json.dumps(project, separators=(",", ":")))
    client = gemini.make_client(cfg)
    resp = client.models.generate_content(
        model=cfg["gemini"]["model"], contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.2))
    try:
        answer = json.loads(resp.text)
    except (TypeError, ValueError) as err:
        raise EditorError("The AI's answer wasn't a project; try saying it differently") from err
    answer["assets"] = project["assets"]  # it may not add files
    return clean(answer, job_dir)
