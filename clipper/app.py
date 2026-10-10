"""Local dashboard: http://127.0.0.1:8000

/            videos found on the creator's profile (or pasted links)
/library     every highlight clip: preview, pick for the albino transform, post
/jobs/<id>   one video (its clips) or one clip (music, captions, approve)
"""

import asyncio
import math
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import agentrouter, captions, catalog, cli_lock, config, db, downloader, editor2, gemini, media, monitor, music
from . import platforms, project, render, render2, shotcut, studio
from . import flow_robot, transform_adapter, trending, worker

HERE = Path(__file__).resolve().parent
MEDIA_FILES = {"source.mp4": "video/mp4", "highlight.mp4": "video/mp4", "transformed.mp4": "video/mp4",
               "final.mp4": "video/mp4", "thumb.jpg": "image/jpeg", "look.jpg": "image/jpeg",
               "look_source.jpg": "image/jpeg"}
JOB_ID = re.compile(r"^\d+(-\d+)?$")


def editable(job: dict) -> bool:
    """A clip can be (re)edited while it waits for approval, and after a failed render
    (so an edit or Shotcut project that won't render can be fixed)."""
    return job["kind"] == "clip" and (
        job["state"] == "READY" or (job["state"] == "FAILED" and job.get("failed_step") == "mix"))

# library tabs -> clip states
TABS = {
    "preview": ("To review", ["PREVIEW", "LOOK"]),
    "working": ("In progress", ["NEW", "CUT", "TRANSFORMED", "APPROVED", "POSTING"]),
    "ready": ("Ready to post", ["READY"]),
    "posted": ("Posted", ["POSTED"]),
    "failed": ("Failed", ["FAILED"]),
    "discarded": ("Discarded", ["REJECTED"]),
}

app = FastAPI(title="Clipper")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")

# set by __main__ so buttons can nudge the background threads
hooks = {"wake_worker": lambda: None, "poll_now": lambda: None}


def _ago(ts):
    if not ts:
        return "—"
    delta = time.time() - ts
    if delta < 0:
        delta = -delta
        return f"in {int(delta // 60)} min" if delta >= 60 else f"in {int(delta)} s"
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)} min ago"
    if delta < 86400:
        return f"{int(delta // 3600)} h ago"
    return f"{int(delta // 86400)} d ago"


def clip_length(job: dict) -> float:
    rng = job.get("cut_range") or job.get("clip") or {}
    start, end = rng.get("start", rng.get("start_s", 0)), rng.get("end", rng.get("end_s", 0))
    return max(0.0, end - start)


def omni_edits(job: dict) -> int:
    """Paid Omni edits the transform will make: one per started 10 s segment
    (a final sliver under 1 s isn't sent)."""
    length = clip_length(job)
    return max(1, math.ceil(length / 10 - 0.1)) if length else 0


def asset(name: str) -> str:
    """/static/<name>?v=<last modified>. Browsers cache static files and may keep using an
    old copy after an update; a new address per version forces the fresh one."""
    path = HERE / "static" / name
    return f"/static/{name}?v={int(path.stat().st_mtime) if path.exists() else 0}"


templates.env.globals["asset"] = asset


@app.middleware("http")
async def revalidate_editor_modules(request: Request, call_next):
    """The editor's JavaScript modules import each other by plain path, so they can't carry
    the ?v= stamp: make browsers check for a newer copy every time (cheap: 304 when unchanged)."""
    response = await call_next(request)
    if request.url.path.startswith(("/static/ed2/", "/static/fonts/")):
        response.headers["Cache-Control"] = "no-cache"
    return response
templates.env.filters["ago"] = _ago
templates.env.filters["cliplen"] = clip_length
templates.env.filters["edits"] = omni_edits
templates.env.filters["clock"] = lambda ts: time.strftime("%H:%M", time.localtime(ts)) if ts else ""


def _back(video_id: str = None) -> RedirectResponse:
    return RedirectResponse(f"/jobs/{video_id}" if video_id else "/", status_code=303)


def _redirect(url: str) -> RedirectResponse:
    # only ever redirect within this site
    return RedirectResponse(url if url and url.startswith("/") and not url.startswith("//") else "/library",
                            status_code=303)


def _job_or_404(video_id: str) -> dict:
    job = db.get(video_id)
    if job is None:
        raise HTTPException(404, "No such job")
    return job


@app.get("/")
def index(request: Request, show_skipped: bool = False):
    cfg = config.load()
    videos = db.list_jobs(include_skipped=show_skipped)
    for video in videos:
        video["counts"] = db.clip_counts(video["video_id"])
    totals = db.clip_counts()
    busy = any(v["state"] in db.BUSY_STATES and not v["hold"] for v in videos)
    return templates.TemplateResponse(request, "index.html", {
        "videos": videos, "cfg": cfg, "busy": busy, "show_skipped": show_skipped, "totals": totals,
        "monitor": db.kv_get("monitor_status", {}) or {},
        "sec_uid": db.kv_get(monitor.sec_uid_key(cfg["creator"]["handle"])) if cfg["creator"]["handle"] else None,
        "has_key": bool(config.secret("UPLOAD_POST_API_KEY")),
        "tracks": music.list_tracks(),
    })


def progress(video: str = None) -> dict:
    """What's still being worked on, for the live progress bar on the library and video pages."""
    counts = db.clip_counts(video)
    total = sum(counts.values())
    cutting = counts.get("NEW", 0)
    if video:
        job = db.get(video)
        finding = int(bool(job and job["state"] in ("DETECTED", "DOWNLOADED", "HIGHLIGHTED") and not job["hold"]))
    else:
        finding = sum(1 for v in db.list_jobs() if v["state"] in ("DETECTED", "DOWNLOADED", "HIGHLIGHTED")
                      and not v["hold"])
    working = sum(counts.get(s, 0) for s in ("CUT", "TRANSFORMED", "APPROVED", "POSTING"))
    signature = f"{sorted(counts.items())}|{finding}"
    return {"total": total, "cut": total - cutting, "cutting": cutting, "finding": finding,
            "working": working, "signature": signature}


@app.get("/api/progress")
def progress_api(video: str = None):
    if video and not JOB_ID.match(video):
        raise HTTPException(404)
    return progress(video)


@app.get("/library")
def library(request: Request, tab: str = "preview", video: str = None):
    if tab not in TABS:
        tab = "preview"
    clips = db.list_clips(parent_id=video, states=TABS[tab][1])
    counts = db.clip_counts(video)
    return templates.TemplateResponse(request, "library.html", {
        "clips": clips, "tab": tab, "tabs": {k: (label, sum(counts.get(s, 0) for s in states))
                                             for k, (label, states) in TABS.items()},
        "video": db.get(video) if video else None, "cfg": config.load(),
        "progress": progress(video), "progress_video": video or "",
    })


@app.post("/jobs")
def add_job(url: str = Form(...)):
    try:
        video_id, canonical = downloader.resolve_video_id(url)
    except Exception as err:  # noqa: BLE001
        raise HTTPException(400, str(err)) from err
    if not db.insert_if_new(video_id, canonical, source="manual"):
        job = db.get(video_id)
        if job["state"] == "SKIPPED":
            db.update(video_id, state="DETECTED", hold=False)
    hooks["wake_worker"]()
    return _back(video_id)


@app.get("/jobs/{video_id}")
def job_page(request: Request, video_id: str):
    job = _job_or_404(video_id)
    cfg = config.load()
    if job["kind"] == "video":
        clips = db.list_clips(parent_id=video_id)
        return templates.TemplateResponse(request, "video.html", {
            "job": job, "clips": clips, "cfg": cfg, "cli_busy": cli_lock.running(video_id),
            "progress": progress(video_id), "progress_video": video_id,
        })
    job_dir = config.job_dir(video_id)
    files = {name: (job_dir / name).exists() for name in MEDIA_FILES}
    return templates.TemplateResponse(request, "clip.html", {
        "job": job, "clip": job.get("clip") or {}, "parent": db.get(job["parent_id"]),
        "files": files, "cfg": cfg, "tracks": music.list_tracks(), "platforms": config.PLATFORMS,
        "mtime": int(max(((job_dir / n).stat().st_mtime for n in MEDIA_FILES if files[n]), default=0)),
        "shotcut": {"installed": shotcut.find(cfg) is not None, "hint": shotcut.INSTALL_HINT,
                    "project": (job_dir / "shotcut" / shotcut.PROJECT).exists()},
        "editable": editable(job), "cli_busy": cli_lock.running(job["parent_id"]),
        "edit_summary": render2.summary(job["edit"]) if (job.get("edit") or {}).get("version") == project.VERSION
        else None,
        "sound": job.get("sound") or {}, "flag": trending.flag,
        "via_upload_post": via_upload_post(cfg),
        "charts": trending.cached_charts(cfg) if via_upload_post(cfg) else {},
        "web_chart": {} if via_upload_post(cfg) else trending.cached_web_chart(),
        "trending_ready": bool(cfg["trending"]["enabled"])
        and (not via_upload_post(cfg) or bool(config.secret("UPLOAD_POST_API_KEY"))),
        "connections": {p: platforms.module(p).status(cfg) for p in config.PLATFORMS}
        if not via_upload_post(cfg) else {},
    })


@app.get("/media/{video_id}/{name}")
def media_file(video_id: str, name: str):
    if name not in MEDIA_FILES or not JOB_ID.match(video_id):
        raise HTTPException(404)
    path = config.JOBS_DIR / video_id / name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type=MEDIA_FILES[name])


@app.get("/music/{name}")
def music_file(name: str):
    try:
        return FileResponse(music.track_path(name))
    except FileNotFoundError as err:
        raise HTTPException(404) from err


@app.post("/clips/pick")
async def pick_clips(request: Request):
    """The user ticked clips in PREVIEW: queue them for the albino transform
    (mode=albino) or to be finished without it (mode=original)."""
    form = await request.form()
    mode = form.get("mode")
    if mode not in ("albino", "original", "discard"):
        raise HTTPException(400, "Unknown action")
    ids = [i for i in form.getlist("ids") if JOB_ID.match(i)]
    if not ids:
        raise HTTPException(400, "Tick at least one clip")
    for clip_id in ids:
        if mode == "discard":
            db.transition(clip_id, "PREVIEW", state="REJECTED")
            continue
        fields = {"state": "CUT", "transform_mode": mode, "hold": False}
        if f"target_{clip_id}" in form:  # the clip page sends who to change along with the pick
            fields["target"] = transform_adapter.clean_target(str(form.get(f"target_{clip_id}")))
        db.transition(clip_id, "PREVIEW", **fields)
    hooks["wake_worker"]()
    return _redirect(str(form.get("back") or "/library"))


@app.post("/clips/delete")
async def delete_clips(request: Request):
    """Permanently delete the ticked clips (their row and files). Not the Approve/post path."""
    form = await request.form()
    ids = [i for i in form.getlist("ids") if JOB_ID.match(i)]
    removed = 0
    for clip_id in ids:
        if db.delete(clip_id).get("video_id"):
            removed += 1
            folder = (config.JOBS_DIR / clip_id).resolve()
            if config.JOBS_DIR.resolve() in folder.parents:
                shutil.rmtree(folder, ignore_errors=True)
    return _redirect(str(form.get("back") or "/library"))


def via_upload_post(cfg: dict) -> bool:
    return cfg["posting"]["service"] == "upload_post"


def _sound_clip(video_id: str) -> dict:
    job = _job_or_404(video_id)
    if not editable(job):
        raise HTTPException(409, "The TikTok sound can only be changed while the clip waits to be posted")
    return job


@app.post("/jobs/{video_id}/sound/suggest")
def sound_suggest(video_id: str):
    """Ask Gemini (again) for trending-sound picks. Runs in the background (~30 s)."""
    job = _sound_clip(video_id)
    sound = job.get("sound") or {}
    if sound.get("status") == "working" and time.time() - (sound.get("started") or 0) < 300:
        return _back(video_id)
    cfg = config.load()
    if via_upload_post(cfg):
        trending.charts(cfg, refresh=True)  # "ask again" should use this week's chart, not a cached one
    threading.Thread(target=worker.refresh_sound_suggestions, args=(video_id, cfg), daemon=True).start()
    return _back(video_id)


@app.post("/jobs/{video_id}/sound/choose")
def sound_choose(video_id: str, track_id: str = Form(...)):
    """Use a sound: one of Gemini's picks, or any track from the charts."""
    job = _sound_clip(video_id)
    cfg = config.load()
    sound = job.get("sound") or {}
    track = next((s for s in sound.get("suggestions") or [] if s["id"] == track_id), None)
    if track is None:
        track = trending.find_track(track_id, trending.cached_charts(cfg))
    if track is None:
        track = next((t for t in trending.cached_web_chart().get("tracks") or [] if t["id"] == track_id), None)
    if track is None:
        raise HTTPException(404, "That sound isn't in the trending charts any more")
    previous = sound.get("chosen") or {}
    chosen = {**track, "start_s": track.get("start_s", 0.0),
              "volume": previous.get("volume", cfg["trending"]["sound_volume"]),
              "original_volume": previous.get("original_volume", cfg["trending"]["original_volume"])}
    db.update(video_id, sound={**sound, "chosen": chosen})
    return _back(video_id)


@app.post("/jobs/{video_id}/sound/settings")
def sound_settings(video_id: str, volume: int = Form(35), original_volume: int = Form(100),
                   start_s: float = Form(0.0)):
    job = _sound_clip(video_id)
    sound = job.get("sound") or {}
    if not sound.get("chosen"):
        raise HTTPException(409, "Choose a sound first")
    chosen = {**sound["chosen"], "volume": max(0, min(100, volume)),
              "original_volume": max(0, min(100, original_volume)), "start_s": max(0.0, round(start_s, 1))}
    db.update(video_id, sound={**sound, "chosen": chosen})
    return _back(video_id)


@app.post("/jobs/{video_id}/sound/clear")
def sound_clear(video_id: str):
    job = _sound_clip(video_id)
    db.update(video_id, sound={**(job.get("sound") or {}), "chosen": None})
    return _back(video_id)


@app.get("/trending/preview/{track_id}")
def trending_preview(track_id: str):
    if not re.fullmatch(r"[\w-]{1,64}", track_id):
        raise HTTPException(404)
    track = trending.find_track(track_id, trending.cached_charts())
    cached = trending.folder() / "previews" / f"{track_id}.mp3"
    try:
        path = cached if cached.exists() else trending.preview_file(track or {"id": track_id})
    except Exception as err:  # noqa: BLE001
        raise HTTPException(404, f"No preview: {err}") from err
    return FileResponse(path, media_type="audio/mpeg")


def _set_look(job: dict, status: str) -> None:
    info = job.get("info") or {}
    look = {**(info.get("look") or {}), "status": status, "decided_at": time.time()}
    _move(job["video_id"], "LOOK", state="CUT", info={**info, "look": look})
    hooks["wake_worker"]()


@app.post("/jobs/{video_id}/look/approve")
def look_approve(video_id: str):
    """Use this look: every piece is now edited to match it."""
    _set_look(_job_or_404(video_id), "approved")
    return _back(video_id)


@app.post("/jobs/{video_id}/look/redo")
def look_redo(video_id: str):
    """Make a different look (one more Omni edit)."""
    job = _job_or_404(video_id)
    job_dir = config.job_dir(video_id)
    (job_dir / "look.jpg").unlink(missing_ok=True)
    shutil.rmtree(job_dir / "look_work", ignore_errors=True)
    _set_look(job, "redo")
    return _back(video_id)


@app.post("/jobs/{video_id}/look/skip")
def look_skip(video_id: str):
    """Go ahead without a reference look (just the stricter wording)."""
    _set_look(_job_or_404(video_id), "skipped")
    return _back(video_id)


@app.post("/jobs/{video_id}/target")
def save_target(video_id: str, target: str = Form("")):
    """Who to make albino when the clip shows several people. Takes effect on the next
    transform; it doesn't change a clip that is already transformed."""
    job = _job_or_404(video_id)
    if job["kind"] != "clip" or job["state"] in ("CUT", "APPROVED", "POSTING", "POSTED"):
        raise HTTPException(409, f"Can't change who to target while the clip is {job['state']}")
    db.update(video_id, target=transform_adapter.clean_target(target))
    return _back(video_id)


@app.get("/jobs/{video_id}/edit")
def edit_page(request: Request, video_id: str):
    """The video editor: Clipper's timeline editor (default), the simple one, or HyperFrames Studio."""
    choice = config.load().get("editor") or "clipper"
    if choice == "studio":
        return studio_page(request, video_id)
    if choice == "simple":
        return editor(request, video_id)
    return editor2_page(request, video_id)


def _clip_dir(video_id: str) -> tuple:
    job = _job_or_404(video_id)
    job_dir = config.job_dir(video_id)
    if job["kind"] != "clip" or not (job_dir / "transformed.mp4").exists():
        raise HTTPException(409, "This clip isn't ready to edit yet")
    return job, job_dir


@app.get("/jobs/{video_id}/editor")
def editor2_page(request: Request, video_id: str):
    job, job_dir = _clip_dir(video_id)
    cfg = config.load()
    proj, draft_at = editor2.load(job, cfg)
    info = job.get("info") or {}
    return templates.TemplateResponse(request, "editor2.html", {
        "job": job,
        "editor_data": {
            "id": video_id, "project": proj, "draft_saved_at": draft_at,
            "assets": editor2.known_assets(job_dir), "catalog": catalog.as_dict(),
            "title": info.get("title", ""), "credit": cfg["creator"].get("credit", ""),
            "editable": editable(job), "state": job["state"],
            "made_with": (info.get("render_from") or "editor"),
        },
    })


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError as err:
        raise HTTPException(400, "Expected JSON") from err
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected a JSON object")
    return body


@app.post("/api/clips/{video_id}/draft")
async def editor2_draft(request: Request, video_id: str):
    """Autosave: keep the work in progress (nothing is rendered)."""
    _, job_dir = _clip_dir(video_id)
    try:
        proj = editor2.clean(await _json(request), job_dir)
    except editor2.EditorError as err:
        raise HTTPException(400, str(err)) from err
    return {"saved_at": editor2.save_draft(job_dir, proj)}


@app.post("/api/clips/{video_id}/project")
async def editor2_save(request: Request, video_id: str):
    """Save the project as the clip's edit and queue the render (ffmpeg, about a minute)."""
    job, job_dir = _clip_dir(video_id)
    if not editable(job):
        raise HTTPException(409, f"Can't change the final video while the clip is {job['state']}")
    try:
        proj = editor2.clean(await _json(request), job_dir)
    except editor2.EditorError as err:
        raise HTTPException(400, str(err)) from err
    v1 = project.to_v1(proj, media.duration(job_dir / "transformed.mp4"))
    info = {**(job.get("info") or {}), "render_from": "editor"}
    if not db.transition(video_id, job["state"], state="TRANSFORMED", edit=proj, info=info, failed_step=None,
                         error=None, music_track=v1["music"]["track"], music_volume=v1["music"]["volume"]):
        raise HTTPException(409, "The clip changed while you were editing; reload the editor")
    editor2.drop_draft(job_dir)
    (job_dir / "final_tiktok.mp4").unlink(missing_ok=True)
    hooks["wake_worker"]()
    return {"ok": True, "redirect": f"/jobs/{video_id}"}


@app.post("/api/clips/{video_id}/discard_draft")
def editor2_discard(video_id: str):
    _, job_dir = _clip_dir(video_id)
    editor2.drop_draft(job_dir)
    return {"ok": True}


@app.post("/api/clips/{video_id}/upload")
async def editor2_upload(request: Request, video_id: str):
    _, job_dir = _clip_dir(video_id)
    form = await request.form()
    file = form.get("file")
    if file is None or not hasattr(file, "read"):
        raise HTTPException(400, "No file")
    data = await file.read()
    try:
        aid, asset = await asyncio.to_thread(editor2.upload, job_dir, file.filename, data)
    except editor2.EditorError as err:
        raise HTTPException(400, str(err)) from err
    return {"id": aid, "asset": asset}


@app.get("/api/clips/{video_id}/asset/{asset_id:path}")
def editor2_asset(video_id: str, asset_id: str):
    _, job_dir = _clip_dir(video_id)
    try:
        path = editor2.asset_file(job_dir, asset_id)
    except (editor2.EditorError, render2.RenderError, project.ProjectError) as err:
        raise HTTPException(404, str(err)) from err
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/api/clips/{video_id}/strip/{asset_id:path}")
def editor2_strip(video_id: str, asset_id: str):
    _, job_dir = _clip_dir(video_id)
    try:
        path = editor2.filmstrip(job_dir, asset_id)
    except (editor2.EditorError, media.MediaError) as err:
        raise HTTPException(404, str(err)) from err
    info = editor2.strip_info(job_dir, asset_id)
    return FileResponse(path, media_type="image/jpeg",
                        headers={"X-Every": str(info.get("every", 1)), "X-Count": str(info.get("count", 0)),
                                 "Cache-Control": "max-age=86400"})


@app.get("/api/clips/{video_id}/peaks/{asset_id:path}")
def editor2_peaks(video_id: str, asset_id: str):
    _, job_dir = _clip_dir(video_id)
    try:
        return editor2.peaks(job_dir, asset_id)
    except (editor2.EditorError, subprocess.SubprocessError) as err:
        raise HTTPException(404, str(err)) from err


@app.post("/api/clips/{video_id}/captions2")
async def editor2_captions(request: Request, video_id: str):
    """Word-timed captions placed on the edited timeline (on-device speech-to-text)."""
    _, job_dir = _clip_dir(video_id)
    body = await _json(request)
    try:
        proj = editor2.clean(body.get("project") or {}, job_dir)
        style = body.get("style") if isinstance(body.get("style"), dict) else catalog.CAPTIONS[0]["style"]
        items = await asyncio.to_thread(editor2.caption_items, job_dir, proj, style)
    except (editor2.EditorError, captions.CaptionError, studio.StudioError, media.MediaError) as err:
        raise HTTPException(502, str(err)) from err
    return {"items": items}


@app.post("/api/clips/{video_id}/assist")
async def editor2_assist(request: Request, video_id: str):
    """'Describe a change': Gemini edits the project; the answer is checked like any save."""
    _, job_dir = _clip_dir(video_id)
    body = await _json(request)
    try:
        proj = editor2.clean(body.get("project") or {}, job_dir)
        new = await asyncio.to_thread(editor2.assist, proj, str(body.get("request") or ""), job_dir, config.load())
    except editor2.EditorError as err:
        raise HTTPException(400, str(err)) from err
    except Exception as err:  # noqa: BLE001 - Gemini unavailable etc.
        raise HTTPException(502, f"The AI helper couldn't answer: {str(err)[:300]}") from err
    return {"project": new}


@app.get("/jobs/{video_id}/studio")
def studio_page(request: Request, video_id: str):
    """HyperFrames Studio's timeline for this clip, inside the dashboard (slower renders)."""
    cfg = config.load()
    job = _job_or_404(video_id)
    job_dir = config.job_dir(video_id)
    if job["kind"] != "clip" or not (job_dir / "transformed.mp4").exists():
        raise HTTPException(409, "This clip isn't ready to edit yet")
    error, url = "", ""
    try:
        proj = studio.write_project(job_dir, worker.current_edit(job, cfg))  # keeps your Studio work
        url = studio.open_studio(cfg, proj)
    except (studio.StudioError, OSError, subprocess.TimeoutExpired) as err:
        error = str(err)
    return templates.TemplateResponse(request, "studio.html", {
        "job": job, "url": url, "error": error, "editable": editable(job),
        "in_use": (job.get("info") or {}).get("render_from") == "studio",
    })


@app.post("/jobs/{video_id}/studio/use")
def studio_use(video_id: str):
    """Render the clip's Studio edit to final.mp4 (in the worker; takes a minute or two)."""
    job = _editable_clip(video_id)
    if not studio.exists(config.job_dir(video_id)):
        raise HTTPException(409, "Open the clip in the editor first")
    info = {**(job.get("info") or {}), "render_from": "studio"}
    _move(video_id, job["state"], state="TRANSFORMED", info=info, failed_step=None, error=None)
    for name in ("final_tiktok.mp4",):
        (config.job_dir(video_id) / name).unlink(missing_ok=True)
    hooks["wake_worker"]()
    return _back(video_id)


@app.post("/jobs/{video_id}/studio/fresh")
def studio_fresh(video_id: str):
    """Throw away the Studio changes and rebuild the project from Clipper's edit."""
    job = _job_or_404(video_id)
    job_dir = config.job_dir(video_id)
    studio.write_project(job_dir, worker.current_edit(job, config.load()), overwrite=True)
    return RedirectResponse(f"/jobs/{video_id}/studio", status_code=303)


@app.get("/jobs/{video_id}/edit/simple")
def editor(request: Request, video_id: str):
    job = _job_or_404(video_id)
    base = config.job_dir(video_id) / "transformed.mp4"
    if job["kind"] != "clip" or not base.exists():
        raise HTTPException(409, "This clip isn't ready to edit yet")
    cfg = config.load()
    edit = worker.current_edit(job, cfg)  # stale music / length already fixed up
    clip = job.get("clip") or {}
    return templates.TemplateResponse(request, "editor.html", {
        "job": job, "clip": clip, "cfg": cfg,
        "editor_data": {
            "id": video_id, "edit": edit, "duration": media.duration(base), "options": render.options(),
            "tracks": music.list_tracks(),
            "base_url": f"/media/{video_id}/transformed.mp4?v={int(base.stat().st_mtime)}",
            "suggestions": {"title": (job.get("info") or {}).get("title", ""),
                            "caption": clip.get("caption", ""), "mood": clip.get("music_mood", "")},
            "editable": editable(job),
            "state": job["state"],
        },
    })


@app.post("/api/clips/{video_id}/captions")
async def auto_captions(request: Request, video_id: str):
    """Word-timed captions for the trimmed clip (on-device speech-to-text; a minute or two)."""
    job = _job_or_404(video_id)
    job_dir = config.job_dir(video_id)
    if job["kind"] != "clip" or not (job_dir / "transformed.mp4").exists():
        raise HTTPException(409, "This clip isn't ready to edit yet")
    body = await request.json()
    trim = body.get("trim") or {}
    try:
        start = float(trim.get("start", 0))
        end = float(trim.get("end", media.duration(job_dir / "transformed.mp4")))
    except (TypeError, ValueError) as err:
        raise HTTPException(400, "Bad trim") from err
    try:
        texts = await asyncio.to_thread(captions.captions_for, job_dir, start, end)
    except (captions.CaptionError, studio.StudioError, media.MediaError) as err:
        raise HTTPException(502, str(err)) from err
    return {"texts": texts}


@app.post("/api/clips/{video_id}/edit")
async def save_edit(request: Request, video_id: str):
    """Save the editor's timeline and queue a render; the worker re-renders final.mp4."""
    job = _job_or_404(video_id)
    if not editable(job):
        raise HTTPException(409, f"Can't edit while the clip is {job['state']}")
    base = config.job_dir(video_id) / "transformed.mp4"
    try:
        edit = render.clean_edit(await request.json(), media.duration(base), render.music_names())
    except (render.EditError, ValueError) as err:
        raise HTTPException(400, str(err)) from err
    # saving here makes the dashboard edit the source of final.mp4 again (not Shotcut)
    info = {**(job.get("info") or {}), "render_from": "editor"}
    if not db.transition(video_id, job["state"], state="TRANSFORMED", edit=edit, info=info, failed_step=None,
                         error=None, music_track=edit["music"]["track"], music_volume=edit["music"]["volume"]):
        raise HTTPException(409, "The clip changed while you were editing; reload the editor")
    hooks["wake_worker"]()
    return {"ok": True, "redirect": f"/jobs/{video_id}"}


def _editable_clip(video_id: str) -> dict:
    job = _job_or_404(video_id)
    if not editable(job):
        raise HTTPException(409, "Only available while the clip is waiting to be posted")
    return job


@app.post("/jobs/{video_id}/shotcut/open")
def shotcut_open(video_id: str, fresh: bool = Form(False)):
    """Write the Shotcut project (unless one exists, so earlier work is kept) and launch Shotcut."""
    job = _editable_clip(video_id)
    job_dir = config.job_dir(video_id)
    project = shotcut.write_project(job_dir, worker.current_edit(job, config.load()), overwrite=fresh)
    try:
        shotcut.open_project(project, config.load())
    except shotcut.ShotcutError as err:
        raise HTTPException(409, str(err)) from err
    db.update(video_id, info={**(job.get("info") or {}), "shotcut_opened": time.time()})
    return _back(video_id)


@app.post("/jobs/{video_id}/shotcut/use")
def shotcut_use(video_id: str):
    """Render the saved Shotcut project to final.mp4 (in the worker; melt takes ~a minute)."""
    job = _editable_clip(video_id)
    if not (config.job_dir(video_id) / "shotcut" / shotcut.PROJECT).exists():
        raise HTTPException(409, "Open the clip in Shotcut first")
    if shotcut.find(config.load()) is None:
        raise HTTPException(409, f"Shotcut isn't installed. Install it with:  {shotcut.INSTALL_HINT}")
    info = {**(job.get("info") or {}), "render_from": "shotcut"}
    _move(video_id, job["state"], state="TRANSFORMED", info=info, failed_step=None, error=None)
    hooks["wake_worker"]()
    return _back(video_id)


def _move(video_id: str, from_state: str, **fields) -> None:
    """Change state only if nothing else (the worker, another tab) changed it first."""
    if not db.transition(video_id, from_state, **fields):
        raise HTTPException(409, "The clip changed in the meantime; reload the page")


@app.post("/jobs/{video_id}/approve")
async def approve(request: Request, video_id: str):
    job = _job_or_404(video_id)
    if job["state"] != "READY":
        raise HTTPException(409, f"Job is {job['state']}, not READY")
    form = await request.form()
    platforms = [p for p in config.PLATFORMS if form.get(f"platform_{p}")]
    if not platforms:
        raise HTTPException(400, "Tick at least one platform")
    captions = {p: str(form.get(f"caption_{p}", "")).strip() for p in config.PLATFORMS}
    if len(captions["x"]) > 280:
        raise HTTPException(400, "The X caption is over 280 characters")
    _move(video_id, "READY", captions=captions, platforms=platforms,
          ai_label=bool(form.get("ai_label")), state="APPROVED", error=None, hold=False)
    hooks["wake_worker"]()
    return _back(video_id)


@app.post("/jobs/{video_id}/save")
async def save_captions(request: Request, video_id: str):
    _job_or_404(video_id)
    form = await request.form()
    db.update(video_id,
              captions={p: str(form.get(f"caption_{p}", "")).strip() for p in config.PLATFORMS},
              platforms=[p for p in config.PLATFORMS if form.get(f"platform_{p}")],
              ai_label=bool(form.get("ai_label")))
    return _back(video_id)


@app.post("/jobs/{video_id}/reject")
def reject(video_id: str):
    job = _job_or_404(video_id)
    if not db.reject(video_id):
        raise HTTPException(409, "It's being posted (or already posted), so it can't be discarded")
    return _redirect("/library?tab=discarded" if job["kind"] == "clip" else "/")


@app.post("/jobs/{video_id}/retry")
def retry(video_id: str):
    # Outputs are only written when a step finishes, so nothing needs clearing.
    # transform_work/ is deliberately kept: already-edited segments aren't re-billed.
    _job_or_404(video_id)
    db.retry(video_id)
    db.update(video_id, hold=False)
    hooks["wake_worker"]()
    return _back(video_id)


@app.post("/jobs/{video_id}/retransform")
def retransform(video_id: str, target: str = Form(None)):
    """Redo the albino edit. With the same target, only segments that fell back to the
    original footage are re-done (the rest are cached and not re-billed). A new target
    means every segment is edited again, for the new person."""
    job = _job_or_404(video_id)
    if job["state"] != "READY":
        raise HTTPException(409, "Only available while the clip is waiting for approval")
    fields = {"state": "CUT", "transform_mode": "albino", "hold": False}
    if target is not None:
        fields["target"] = transform_adapter.clean_target(target)
    _move(video_id, "READY", **fields)
    job_dir = config.job_dir(video_id)
    for name in ("transformed.mp4", "final.mp4"):
        (job_dir / name).unlink(missing_ok=True)
    hooks["wake_worker"]()
    return _back(video_id)


def _piece(job: dict, index: int) -> dict:
    for piece in (job.get("transform_info") or {}).get("pieces") or []:
        if piece["index"] == index:
            return piece
    raise HTTPException(404, "No such piece")


@app.get("/media/{video_id}/piece/{index}")
def piece_file(video_id: str, index: int):
    """One 10-second piece as it went into the final clip (after colour matching)."""
    if not JOB_ID.match(video_id):
        raise HTTPException(404)
    piece = _piece(_job_or_404(video_id), index)
    job_dir = (config.JOBS_DIR / video_id).resolve()
    path = (job_dir / piece["file"]).resolve()
    if job_dir not in path.parents or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="video/mp4")


@app.post("/jobs/{video_id}/redo_piece")
def redo_piece(video_id: str, index: int = Form(...)):
    """Edit one piece again (about 1 Omni edit). The other pieces' edits are kept and
    reused, so they cost nothing; colour matching then runs over all of them again."""
    job = _job_or_404(video_id)
    if job["state"] != "READY":
        raise HTTPException(409, "Only available while the clip is waiting to be posted")
    piece = _piece(job, index)
    if piece["kind"] == "sliver":
        raise HTTPException(409, "This piece is under a second long, so it isn't sent to Omni")
    made_with = (job.get("transform_info") or {}).get("backend") or "vertex"
    now_on = transform_adapter.backend(config.load())
    if made_with != now_on:
        # the other pieces' edits are kept under the old route's name: all of them would be redone
        raise HTTPException(409, f"This clip was made with {made_with.title()}, but albino edits now run on "
                                 f"{now_on.title()} (transform: backend), so redoing one piece would redo every "
                                 f"piece. Switch the backend back to redo just this one, or use 'Redo for this "
                                 f"person' to redo the whole clip on {now_on.title()}.")
    job_dir = (config.JOBS_DIR / video_id).resolve()
    if piece.get("cache"):
        cached = (job_dir / piece["cache"]).resolve()
        if job_dir in cached.parents:
            cached.unlink(missing_ok=True)
    _move(video_id, "READY", state="CUT", transform_mode="albino", hold=False)
    for name in ("transformed.mp4", "final.mp4"):
        (job_dir / name).unlink(missing_ok=True)
    hooks["wake_worker"]()
    return _back(video_id)


@app.post("/jobs/{video_id}/process")
def process(video_id: str):
    """Bring back a skipped video or a discarded clip."""
    job = _job_or_404(video_id)
    if job["state"] in ("SKIPPED", "REJECTED"):
        if job["kind"] == "clip":
            cut_done = (config.job_dir(video_id) / "highlight.mp4").exists()
            _move(video_id, job["state"], state="PREVIEW" if cut_done else "NEW", hold=False)
        else:
            _move(video_id, job["state"], state="DETECTED", hold=False)
        hooks["wake_worker"]()
    return _back(video_id)


@app.post("/jobs/{video_id}/continue")
def continue_job(video_id: str):
    """Release a video (and its clips) that `python -m clipper run` stopped early."""
    job = _job_or_404(video_id)
    if cli_lock.running(job.get("parent_id") or video_id):
        raise HTTPException(409, "The terminal is still working on this video; wait for it to finish")
    db.update(video_id, hold=False)
    for clip in db.list_clips(parent_id=video_id):
        if clip["hold"]:
            db.update(clip["video_id"], hold=False)
    hooks["wake_worker"]()
    return _back(video_id)


# ------------------------------------------------------------ connections

@app.get("/connections")
def connections(request: Request, msg: str = "", error: str = ""):
    cfg = config.load()
    return templates.TemplateResponse(request, "connections.html", {
        "cfg": cfg, "msg": msg, "error": error, "service": cfg["posting"]["service"],
        "status": {p: platforms.module(p).status(cfg) for p in config.PLATFORMS},
        "saved": {name: bool(config.secret(name)) for name in config.SECRET_NAMES},
        "gemini": gemini.status(cfg),
        "agentrouter": agentrouter.status(cfg),
        "flow": flow_robot.status(cfg),
        "use_vertex": "vertex" in cfg["gemini"]["backends"],
        "creators": cfg["creators"],
        "watermark": cfg["watermark"],
    })


def _to_connections(msg: str = "", error: str = "") -> RedirectResponse:
    from urllib.parse import urlencode
    return RedirectResponse("/connections?" + urlencode({k: v for k, v in (("msg", msg), ("error", error)) if v}),
                            status_code=303)


@app.post("/connections/keys")
async def save_keys(request: Request):
    """Store the app keys you paste in (only non-empty fields; existing keys are never shown back)."""
    form = await request.form()
    changed = [name for name in config.SECRET_NAMES if str(form.get(name) or "").strip()]
    for name in changed:
        config.set_secret(name, str(form.get(name)))
    return _to_connections(msg=f"Saved {len(changed)} key{'s' if len(changed) != 1 else ''}." if changed
                           else "Nothing to save: paste a key first.")


@app.post("/connections/gemini/vertex")
def gemini_vertex():
    """Turn the Vertex route on/off (the button on the Gemini card)."""
    on_now = "vertex" in config.load()["gemini"]["backends"]
    config.ui_set(use_vertex=not on_now)
    hooks["wake_worker"]()
    return _to_connections(msg="Vertex is ON — Clipper will try your Cloud project first." if not on_now
                           else "Vertex is OFF — Clipper uses only your free Gemini key(s).")


@app.post("/connections/watermark")
async def save_watermark(request: Request):
    """Save the @username watermark (text + on/off) from the Connections card."""
    form = await request.form()
    text = str(form.get("text") or "").strip()[:40]
    config.ui_set(watermark={"enabled": bool(form.get("enabled")) and bool(text), "text": text,
                             "position": str(form.get("position") or "bottom-right")})
    hooks["wake_worker"]()
    return _to_connections(msg=f"Watermark {'on' if text and form.get('enabled') else 'saved (off)'}: "
                               f"{text or 'no text yet'}. New clips get it; re-render older ones to add it.")


@app.post("/connections/creators/add")
async def creators_add(request: Request):
    form = await request.form()
    tags = [t for t in re.split(r"[,\s]+", str(form.get("hashtags") or "")) if t.strip()]
    try:
        config.add_creator(str(form.get("handle") or ""), str(form.get("credit") or ""), tags)
    except ValueError as err:
        return _to_connections(error=str(err))
    hooks["poll_now"]()
    return _to_connections(msg=f"Now watching @{str(form.get('handle')).lstrip('@').strip()}.")


@app.post("/connections/creators/remove")
def creators_remove(handle: str = Form(...)):
    config.remove_creator(handle)
    return _to_connections(msg=f"Stopped watching @{handle.lstrip('@').strip()}.")


@app.post("/connections/creators/toggle")
def creators_toggle(handle: str = Form(...), enabled: str = Form("")):
    config.set_creator_enabled(handle, enabled == "on")
    hooks["poll_now"]()
    return _to_connections(msg="Updated.")


@app.post("/connections/flow/open")
def flow_open():
    try:
        flow_robot.open_window(config.load())
    except Exception as err:  # noqa: BLE001
        return _to_connections(error=f"Couldn't open the robot window: {err}")
    return _to_connections(msg="The Flow robot's Chrome window is open. Sign in there with your AI Pro account, "
                               "open (or make) a Flow project, then click Check.")


@app.post("/connections/flow/check")
def flow_check():
    try:
        st = flow_robot.check(config.load())
    except Exception as err:  # noqa: BLE001
        return _to_connections(error=str(err))
    credits = f", {st['credits']} credits" if st.get("credits") is not None else ""
    hooks["wake_worker"]()
    return _to_connections(msg=f"Flow robot ready: {st.get('account') or 'signed in'}{credits}.")


@app.post("/connections/flow/clear")
def flow_clear():
    flow_robot.clear_pause()
    hooks["wake_worker"]()
    return _to_connections(msg="Cleared: clips waiting for the Flow robot carry on now.")


@app.get("/connect/{platform}")
def connect(platform: str):
    if platform not in ("tiktok", "instagram"):
        raise HTTPException(404)
    connector = platforms.module(platform)
    cfg = config.load()
    if connector.status(cfg)["missing"]:
        return _to_connections(error=f"Save your {platform} app keys first.")
    return RedirectResponse(connector.login_url(cfg), status_code=303)


@app.get("/connect/tiktok/callback/")
@app.get("/connect/tiktok/callback")
def tiktok_callback(code: str = "", state: str = "", error: str = "", error_description: str = ""):
    return _finish_login("tiktok", code, state, error_description or error)


@app.get("/connect/instagram/callback")
def instagram_callback(code: str = "", state: str = "", error: str = "", error_description: str = ""):
    return _finish_login("instagram", code, state, error_description or error)


def _finish_login(platform: str, code: str, state: str, error: str) -> RedirectResponse:
    if error or not code:
        return _to_connections(error=f"{platform.title()} login was cancelled or failed: {error or 'no code'}")
    try:
        conn = platforms.module(platform).finish_login(code, state, config.load())
    except Exception as err:  # noqa: BLE001
        return _to_connections(error=str(err))
    name = conn.get("username") or conn.get("display_name") or "your account"
    return _to_connections(msg=f"{platform.title()} connected: {name}")


@app.post("/connect/x/verify")
def x_verify():
    try:
        conn = platforms.module("x").verify(config.load())
    except Exception as err:  # noqa: BLE001
        return _to_connections(error=f"X keys didn't work: {err}")
    return _to_connections(msg=f"X connected: @{conn.get('username', '?')}")


@app.post("/connections/{platform}/disconnect")
def disconnect(platform: str):
    if platform not in config.PLATFORMS:
        raise HTTPException(404)
    platforms.module(platform).disconnect()
    return _to_connections(msg=f"{platform.title()} disconnected.")


# ------------------------------------------------------------ manual posting helpers

@app.post("/jobs/{video_id}/mark_posted")
def mark_posted(video_id: str, platform: str = Form(...)):
    """You posted it yourself (or it went through after all): record it, so Clipper never posts it again."""
    job = _job_or_404(video_id)
    if platform not in config.PLATFORMS:
        raise HTTPException(400, "Unknown platform")
    if job["state"] not in ("READY", "FAILED", "POSTED"):
        raise HTTPException(409, f"Clip is {job['state']}")
    prior = job.get("post_result") or {}
    platforms_done = {**(prior.get("platforms") or {}),
                      platform: {"ok": True, "url": "", "message": "Marked as posted by you", "drafted": False,
                                 "skipped": False, "manual": True}}
    pending = {k: v for k, v in (prior.get("pending") or {}).items() if k != platform}
    fields = {"post_result": {**prior, "platforms": platforms_done, "pending": pending}}
    if all(platforms_done.get(p, {}).get("ok") for p in (job.get("platforms") or [platform])):
        fields.update(state="POSTED", failed_step=None, error=None)
    db.update(video_id, **fields)
    return _back(video_id)


@app.post("/jobs/{video_id}/forget_pending")
def forget_pending(video_id: str, platform: str = Form(...)):
    """You checked the platform and the upload isn't there: allow a fresh upload on the next retry."""
    job = _job_or_404(video_id)
    if job["state"] != "FAILED":
        raise HTTPException(409, "Only for a failed post")
    prior = job.get("post_result") or {}
    pending = {k: v for k, v in (prior.get("pending") or {}).items() if k != platform}
    db.update(video_id, post_result={**prior, "pending": pending,
                                     "status": "timeout" if pending else "completed"})
    return _back(video_id)


@app.post("/monitor/poll")
def poll_now():
    hooks["poll_now"]()
    return _back()
