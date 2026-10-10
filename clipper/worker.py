"""Moves jobs through the pipeline, one job and one step at a time.

video: download -> highlight -> clips (one clip row per highlight)
clip:  cut -> PREVIEW (waits for the user to pick it) -> transform -> mix
       -> READY (waits for approval) -> post
"""

import json
import logging
import shutil
import threading
import time
import traceback

from . import config, cutter, db, downloader, highlight, media, monitor, music, notify, publisher, render
from . import flow_robot, platforms, project, render2, shotcut, studio, transform_adapter, trending

log = logging.getLogger(__name__)

# files each step produces, so a re-run of an earlier step can clear everything after it
STEP_OUTPUTS = {
    "highlight": ["highlight.json", "proxy_480p.mp4", "proxy_360p.mp4", "proxy_240p.mp4"],
    "cut": ["highlight.mp4", "thumb.jpg"],
    "transform": ["transformed.mp4", "transformed_raw.mp4", "transform_work"],
    "mix": ["final.mp4", "edit.ass"],
}


def clear_from(video_id: str, step: str) -> None:
    """Delete the outputs of `step` and every step after it."""
    job_dir = config.job_dir(video_id)
    for name in db.STEPS[db.STEPS.index(step):]:
        for item in STEP_OUTPUTS.get(name, []):
            path = job_dir / item
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()


def build_captions(clip: dict, cfg: dict, creator: dict = None) -> dict:
    """Default captions for one clip: Gemini's caption + the creator credit + hashtags."""
    creator = creator or cfg["creator"]
    credit = creator["credit"]
    tags = []
    for tag in list(creator["hashtags"]) + list(clip.get("hashtags") or []):
        tag = "#" + tag.lstrip("#").strip()
        if len(tag) > 1 and tag.lower() not in {t.lower() for t in tags}:
            tags.append(tag)
    text = (clip.get("caption") or "").strip()
    full = "\n\n".join(part for part in (text, credit, " ".join(tags[:6])) if part)

    x_tags = " ".join(tags[:2])
    x_tail = "\n\n" + " ".join(p for p in (credit, x_tags) if p)
    room = 280 - len(x_tail)
    x_text = text if len(text) <= room else text[: max(0, room - 1)].rstrip() + "…"
    return {"tiktok": full, "instagram": full, "x": (x_text + x_tail).strip()}


def run_step(job: dict, step: str, cfg: dict) -> None:
    vid = job["video_id"]
    job_dir = config.job_dir(vid)

    if step == "download":
        info = downloader.download(job["url"], job_dir)
        if cfg["creator"]["handle"]:
            monitor.remember_sec_uid(info.get("channel_id"), info.get("uploader"), cfg["creator"]["handle"])
        db.update(vid, info=info)

    elif step == "highlight":
        hl = highlight.pick(job_dir / "source.mp4", job_dir, cfg, caption=(job.get("info") or {}).get("title", ""))
        db.update(vid, info={**(job.get("info") or {}), "highlights": len(hl["clips"])})

    elif step == "clips":
        make_clips(job, cfg)

    elif step == "cut":
        source = config.job_dir(job["parent_id"]) / "source.mp4"
        cut_range = cutter.cut(source, job_dir, job["clip"],
                               cfg["clip"]["min_seconds"], cfg["clip"]["max_seconds"])
        cutter.thumbnail(job_dir / "highlight.mp4", job_dir / "thumb.jpg")
        db.update(vid, cut_range=cut_range)
        if cfg.get("pipeline", {}).get("auto_finish", False):
            # no manual pick: send every clip straight through to READY, as-is (no transform)
            db.update(vid, transform_mode="original")
            return "CUT"

    elif step == "transform":
        if job.get("transform_mode") == "original":  # user chose "use as is"
            cfg = {**cfg, "transform": {**cfg["transform"], "enabled": False}}
        reference = None
        if cfg["transform"]["enabled"] and cfg["transform"].get("reference") \
                and transform_adapter.backend(cfg) == "vertex":  # the Flow robot has no reference-look step
            look = (job.get("info") or {}).get("look") or {}
            target = job.get("target") or ""
            if look.get("status") not in ("approved", "skipped") or look.get("target", "") != target:
                # First make the reference look and wait for the user to approve it.
                transform_adapter.make_look(job_dir / "highlight.mp4", job_dir, cfg, target)
                db.update(vid, info={**(job.get("info") or {}),
                                     "look": {"status": "pending", "target": target, "made_at": time.time()}})
                return "LOOK"
            if look["status"] == "approved" and (job_dir / "look.jpg").exists():
                reference = job_dir / "look.jpg"
        info = transform_adapter.run(job_dir / "highlight.mp4", job_dir, cfg, target=job.get("target") or "",
                                     reference_image=reference)
        fields = {"transform_info": info}
        # Only label as AI when Omni actually changed something. Keep a choice the
        # user saved earlier, unless what the transform did has changed underneath it.
        previous = job.get("transform_info") or {}
        if job.get("ai_label") is None or info["refused"] or previous.get("refused"):
            fields["ai_label"] = bool(cfg["upload_post"]["ai_label"]) and info["enabled"] and not info["refused"]
        db.update(vid, **fields)

    elif step == "mix":
        render_edit(job, cfg)

    elif step == "post":
        post(job, cfg)


def make_clips(video: dict, cfg: dict) -> None:
    """One clip row per highlight. Clips inherit the video's hold, so a video run
    from the terminal doesn't have its clips picked up by the dashboard's worker."""
    hl = highlight.pick(config.job_dir(video["video_id"]) / "source.mp4",
                        config.job_dir(video["video_id"]), cfg)
    creator = config.creator_for(cfg, video.get("handle"))
    for number, clip in enumerate(hl["clips"], start=1):
        db.insert_clip(
            f"{video['video_id']}-{number:02d}", video, clip,
            hold=video.get("hold", False),
            info={"title": clip.get("title") or f"Highlight {number}",
                  "video_title": (video.get("info") or {}).get("title", "")},
            captions=build_captions(clip, cfg, creator),
            # several people: pre-fill who to change with Gemini's description of the creator
            target=transform_adapter.clean_target(clip.get("creator_description"))
            if (clip.get("people_count") or 0) > 1 else "",
            platforms=list(cfg["upload_post"]["platforms"]),
            music_track=music.pick_for_mood(clip.get("music_mood")),
            music_volume=float(cfg["music"]["default_volume"]),
        )


def post(job: dict, cfg: dict) -> None:
    if cfg["posting"]["service"] == "upload_post":
        return post_upload_post(job, cfg)
    return post_direct(job, cfg)


def post_direct(job: dict, cfg: dict) -> None:
    """Clipper's own posting, one platform at a time. Never double-posts:
    - platforms that already succeeded are skipped;
    - the moment a platform has the upload, its handle (TikTok publish id, Instagram
      container, X media id) is saved, and a retry asks the platform about it instead of
      uploading again;
    - one platform failing doesn't stop the others."""
    vid = job["video_id"]
    job_dir = config.job_dir(vid)
    prior = job.get("post_result") or {}
    prior_platforms = prior.get("platforms") or {}
    pending = dict(prior.get("pending") or {})  # {platform: handle}
    results = {}

    def save(status: str) -> dict:
        merged = {**prior_platforms, **results}
        record = {"status": status, "platforms": merged, "pending": dict(pending), "service": "direct"}
        (job_dir / "post.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        db.update(vid, post_result=record)
        return merged

    done = {p for p, r in prior_platforms.items() if r.get("ok")}
    captions = job.get("captions") or {}
    for platform in [p for p in (job.get("platforms") or []) if p not in done]:
        connector = platforms.module(platform)
        if platform in pending:  # uploaded last time, outcome unknown: ask, don't re-upload
            try:
                outcome = connector.check(pending[platform], cfg)
            except Exception as err:  # noqa: BLE001
                log.warning("[%s] couldn't check %s: %s", vid, platform, err)
                continue
            if outcome is not None:
                results[platform] = _normal(outcome)
                pending.pop(platform)
            continue

        def submitted(handle, platform=platform):
            pending[platform] = handle
            save("timeout")

        video = tiktok_video(job, cfg) if platform == "tiktok" and (job.get("sound") or {}).get("chosen") \
            else job_dir / "final.mp4"
        try:
            outcome = connector.post(job, video, captions.get(platform, ""), bool(job.get("ai_label")), cfg,
                                     on_submitted=submitted)
        except Exception as err:  # noqa: BLE001
            log.warning("[%s] %s: %s", vid, platform, err)
            if platform not in pending:  # nothing reached the platform: a plain failure, safe to retry
                results[platform] = _normal({"ok": False, "message": str(err)})
            continue
        if outcome.get("pending"):
            continue  # still processing on the platform's side; the handle is saved
        results[platform] = _normal(outcome)
        pending.pop(platform, None)

    merged = save("timeout" if pending else "completed")
    failed = [p for p in (job.get("platforms") or []) if not merged.get(p, {}).get("ok")]
    if pending or failed:
        waiting = ", ".join(pending)
        raise publisher.PublishError("Not posted yet: " + "; ".join(
            [f"{p}: {merged.get(p, {}).get('message') or 'failed'}" for p in failed if p not in pending]
            + ([f"{waiting}: still processing, retry in a few minutes"] if pending else [])))


def _normal(outcome: dict) -> dict:
    return {"ok": bool(outcome.get("ok")), "url": outcome.get("url", ""), "message": outcome.get("message", ""),
            "drafted": bool(outcome.get("drafted")), "skipped": bool(outcome.get("skipped")),
            "manual": bool(outcome.get("manual"))}


def post_upload_post(job: dict, cfg: dict) -> None:
    """Never double-posts:
    - only platforms that haven't succeeded yet are sent;
    - Upload-Post's request_id is saved the moment the upload is accepted, and a retry
      asks about that request instead of uploading again;
    - the Idempotency-Key only changes after a definite outcome, so retrying an upload
      whose fate is unknown (crash, dropped connection) is de-duplicated by Upload-Post."""
    vid = job["video_id"]
    job_dir = config.job_dir(vid)
    prior = job.get("post_result") or {}
    prior_platforms = prior.get("platforms") or {}
    attempt = job.get("post_attempt") or 0
    if attempt == 0 or prior.get("status") in publisher.DONE or prior.get("status") == "rejected":
        attempt += 1
        db.update(vid, post_attempt=attempt)

    # A clip can go up in more than one request (TikTok separately when it gets a trending
    # sound), so in-flight uploads are tracked per request: {key suffix: request_id}.
    pending = dict(prior.get("pending") or {})
    if not pending and prior.get("status") == "timeout" and prior.get("request_id"):
        pending = {"": prior["request_id"]}  # saved before uploads were split
    results = {}

    def save(status: str) -> dict:
        merged = {**prior_platforms, **results}
        record = {"status": status, "platforms": merged, "pending": dict(pending),
                  "request_id": next(iter(pending.values()), prior.get("request_id"))}
        (job_dir / "post.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        db.update(vid, post_result=record)
        return merged

    if pending:
        # The last upload(s) may have gone through after we stopped waiting: ask, never re-upload.
        for suffix, request_id in list(pending.items()):
            result = publisher.check_status(request_id)
            if result["status"] not in publisher.DONE:
                raise publisher.PublishError("The previous upload is still processing on Upload-Post; "
                                             "retry again in a few minutes")
            results.update(result["platforms"])
            del pending[suffix]
    else:
        done = {p for p, r in prior_platforms.items() if r.get("ok")}
        todo = [p for p in (job.get("platforms") or []) if p not in done]
        for suffix, group, video, extra in upload_groups(job, cfg, todo):
            def submitted(request_id, suffix=suffix):
                pending[suffix] = request_id
                save("timeout")

            kwargs = {"extra": extra, "key_suffix": suffix} if suffix else {}
            try:
                result = publisher.publish(video, vid, attempt, job.get("captions") or {}, group,
                                           bool(job.get("ai_label")), cfg, on_submitted=submitted, **kwargs)
            except publisher.PublishError as err:
                # definite rejection: nothing went up, a retry may use a fresh key
                save("timeout" if pending else ("rejected" if err.definite else "partial"))
                raise
            except Exception:
                # outcome unknown: keep the key (status isn't final) so Upload-Post de-duplicates
                save("timeout" if pending else "partial")
                raise
            results.update(result["platforms"])
            if result["status"] == "timeout" and result.get("request_id"):
                pending[suffix] = result["request_id"]
            else:
                pending.pop(suffix, None)

    merged = save("timeout" if pending else "completed")
    failed = [p for p in (job.get("platforms") or []) if not merged.get(p, {}).get("ok")]
    if pending or failed:
        raise publisher.PublishError(
            f"Upload-Post: not posted yet: {failed or 'still processing'}")


def upload_groups(job: dict, cfg: dict, todo: list) -> list:
    """[(key suffix, platforms, video file, extra form fields)]. With a trending sound chosen,
    TikTok gets its own upload: a version without the editor's music, with the sound
    attached on TikTok. Instagram and X get the normal final video."""
    job_dir = config.job_dir(job["video_id"])
    chosen = (job.get("sound") or {}).get("chosen")
    if not (chosen and "tiktok" in todo and cfg["trending"]["enabled"]):
        return [("", todo, job_dir / "final.mp4", None)]
    video = tiktok_video(job, cfg)
    groups = [("-tiktok", ["tiktok"], video, publisher.sound_fields(chosen, media.duration(video)))]
    rest = [p for p in todo if p != "tiktok"]
    if rest:
        groups.append(("", rest, job_dir / "final.mp4", None))
    return groups


def tiktok_video(job: dict, cfg: dict):
    """The edit rendered without its music, so the trending sound TikTok adds doesn't
    clash with it. A Shotcut edit can't be split, so it goes up as it is."""
    job_dir = config.job_dir(job["video_id"])
    final = job_dir / "final.mp4"
    source = (job.get("info") or {}).get("render_from")
    if source == "shotcut":
        return final
    out = job_dir / "final_tiktok.mp4"
    if out.exists() and out.stat().st_mtime >= final.stat().st_mtime:
        return out
    if source == "studio":  # the same Studio project, minus Clipper's music track
        if not studio.has_music(studio.project_dir(job_dir)):
            return final  # identical to final.mp4, already watermarked
        return apply_watermark(studio.render_project(job_dir, out_name="final_tiktok.mp4", music=False), cfg)
    proj = v2_project(job)
    if proj is not None:  # the timeline editor's project, minus audio marked as music
        if not any(it.get("music") for t in proj["tracks"] if t["kind"] == "audio" for it in t["items"]):
            return final
        return apply_watermark(render2.render(proj, job_dir, out_name="final_tiktok.mp4", music=False), cfg)
    edit = current_edit(job, cfg)
    edit = {**edit, "music": {**edit["music"], "track": None}}
    return apply_watermark(render.render(edit, job_dir, out_name="final_tiktok.mp4"), cfg)


def refresh_sound_suggestions(video_id: str, cfg: dict) -> None:
    """Ask Gemini for trending-sound picks for a READY clip. Never fails the clip."""
    job = db.get(video_id)
    sound = job.get("sound") or {}
    db.update(video_id, sound={**sound, "status": "working", "started": time.time(), "error": None})
    try:
        db.update(video_id, sound=trending.suggest_any(job, cfg))
    except Exception as err:  # noqa: BLE001
        log.warning("[%s] trending sound suggestions failed: %s", video_id, err)
        db.update(video_id, sound={**sound, "status": "error", "error": str(err)[:500]})


def v2_project(job: dict):
    """The timeline editor's project for this clip (re-checked against the files now on
    disk), or None when the clip's edit is in the older format."""
    from . import editor2

    edit = job.get("edit") or {}
    if edit.get("version") != project.VERSION:
        return None
    return project.clean(edit, editor2.known_assets(config.job_dir(job["video_id"])))


def current_edit(job: dict, cfg: dict) -> dict:
    """The clip's edit, fitted to what exists now. A clip never opened in the editor
    gets a default one: the whole clip plus the music Gemini's mood suggested.
    A timeline-editor project is given in the older format (for Shotcut / Studio)."""
    duration = media.duration(config.job_dir(job["video_id"]) / "transformed.mp4")
    tracks = render.music_names()
    if (job.get("edit") or {}).get("version") == project.VERSION:
        return render.fit_edit(project.to_v1(v2_project(job), duration), duration, tracks)
    if job.get("edit") is None:
        track = job.get("music_track") if job.get("music_track") in tracks else None
        return render.default_edit(duration, track, job.get("music_volume"),
                                   ducking=bool(cfg["music"]["ducking"]))
    return render.fit_edit(job["edit"], duration, tracks)


def render_edit(job: dict, cfg: dict) -> None:
    """Render the clip's edit (or the user's saved Shotcut project) to final.mp4."""
    vid = job["video_id"]
    job_dir = config.job_dir(vid)
    source = (job.get("info") or {}).get("render_from")
    if source == "shotcut":
        shotcut.render_project(job_dir, cfg)
    elif source == "studio":
        studio.render_project(job_dir)
    elif v2_project(job) is not None:
        render2.render(v2_project(job), job_dir)
    else:
        edit = current_edit(job, cfg)
        db.update(vid, edit=edit, music_track=edit["music"]["track"], music_volume=edit["music"]["volume"])
        render.render(edit, job_dir)
    apply_watermark(job_dir / "final.mp4", cfg)


def apply_watermark(path, cfg: dict):
    """Burn the configured @username watermark onto `path` in place (does nothing if off)."""
    wm = cfg.get("watermark") or {}
    if not wm.get("enabled") or not (wm.get("text") or "").strip() or not path.exists():
        return path
    tmp = path.with_name(path.stem + ".wm.mp4")
    try:
        media.watermark(path, tmp, wm["text"], wm.get("opacity", 0.35),
                        wm.get("position", "bottom-right"), wm.get("size", 4))
        tmp.replace(path)
    except Exception as err:  # noqa: BLE001 - never let a watermark failure lose the clip
        log.warning("[watermark] couldn't add it to %s: %s", path.name, err)
        tmp.unlink(missing_ok=True)
    return path


def advance(job: dict, cfg: dict = None) -> str:
    """Run the next step of one job. Returns the step name that ran."""
    cfg = cfg or config.load()
    vid, state = job["video_id"], job["state"]
    step = db.NEXT_STEP[state]
    if step == "post":
        if not db.transition(vid, "APPROVED", state="POSTING"):
            return step  # rejected or changed since it was picked up
        state = "POSTING"
    log.info("[%s] %s", vid, step)
    try:
        # a step may stop at a waiting state instead (transform -> LOOK: approve the look first)
        stopped_at = run_step(job, step, cfg)
    except (flow_robot.Paused, flow_robot.NeedsYou) as err:
        # The Flow robot can't go on right now (paused, daily cap, signed out, low credits):
        # the clip waits in the queue with the reason shown, and carries on by itself later.
        # Pieces already edited stay cached, so nothing is paid for twice.
        log.warning("[%s] waiting for the Flow robot: %s", vid, err)
        db.transition(vid, state, error=f"Waiting for the Flow robot: {err}")
        if isinstance(err, flow_robot.NeedsYou):
            base = f"http://{cfg['dashboard']['host']}:{cfg['dashboard']['port']}"
            notify.toast("The Flow robot needs you", str(err)[:120], f"{base}/connections")
        return step
    except Exception as err:  # noqa: BLE001
        log.error("[%s] %s failed: %s", vid, step, err)
        db.mark_failed(vid, step, f"{type(err).__name__}: {err}\n\n{traceback.format_exc()}", from_state=state)
        return step
    # Only move on if nobody changed the job while the step ran (e.g. the user rejected it).
    if not db.transition(vid, state, state=stopped_at or db.DONE_STATE[step], failed_step=None, error=None):
        log.info("[%s] changed while %s ran; leaving it as it is", vid, step)
        return step
    base = f"http://{cfg['dashboard']['host']}:{cfg['dashboard']['port']}"
    if stopped_at == "LOOK":
        notify.toast("Approve the albino look", (job.get("info") or {}).get("title") or vid, f"{base}/jobs/{vid}")
    elif step == "clips":
        count = len(db.list_clips(parent_id=vid))
        notify.toast(f"{count} highlights found", "They're being cut now; preview them in the library",
                     f"{base}/jobs/{vid}")
    elif step == "mix":
        title = (job.get("info") or {}).get("title") or vid
        notify.toast("Clip ready to post", title, f"{base}/jobs/{vid}")
        if wants_sound_suggestions(db.get(vid), cfg):
            refresh_sound_suggestions(vid, cfg)
    return step


def wants_sound_suggestions(job: dict, cfg: dict) -> bool:
    """Suggest trending sounds once per clip, for clips going to TikTok."""
    via_upload_post = cfg["posting"]["service"] == "upload_post"
    return (bool(cfg["trending"]["enabled"])
            and (not via_upload_post or bool(config.secret("UPLOAD_POST_API_KEY")))
            and job.get("kind") == "clip" and "tiktok" in (job.get("platforms") or [])
            and not (job.get("sound") or {}).get("suggestions"))


class Worker(threading.Thread):
    def __init__(self):
        super().__init__(name="worker", daemon=True)
        self.wake = threading.Event()
        self.stop_event = threading.Event()

    def run(self):
        while not self.stop_event.is_set():
            try:
                cfg = config.load()
                # auto_finish: don't wait for a manual pick; send clips waiting in PREVIEW
                # straight through to READY as-is (also catches clips cut before it was turned on)
                if cfg.get("pipeline", {}).get("auto_finish", False):
                    picked = db.auto_pick_previews()
                    if picked:
                        log.info("Auto-finishing %d clip(s) waiting in preview", picked)
                # while the Flow robot can't edit, leave clips waiting for their albino edit alone
                hold_transforms = (cfg["transform"]["enabled"] and transform_adapter.uses_flow(cfg)
                                   and not flow_robot.can_edit(cfg))
                job = db.next_actionable(skip_transforms=hold_transforms)
                if job is None:
                    self.wake.wait(15)
                    self.wake.clear()
                    continue
                advance(job)
            except Exception:  # noqa: BLE001 - e.g. a typo in config.yaml; keep the thread alive
                log.exception("Worker loop error; retrying in 30 s")
                self.stop_event.wait(30)
