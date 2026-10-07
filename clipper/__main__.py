"""python -m clipper [serve|check|list-new|run --url URL --until STEP]"""

import argparse
import logging
import shutil
import sys
import threading
import webbrowser

from . import config, db


class _DropConnectionResets(logging.Filter):
    """On Windows, every time the browser stops loading a video (seeking, closing the
    tab) asyncio logs a scary ConnectionResetError traceback. It's harmless."""

    def filter(self, record):
        exc = record.exc_info[1] if record.exc_info else None
        return not isinstance(exc, ConnectionResetError)


def setup_logging() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(config.LOG_PATH, encoding="utf-8")],
    )
    logging.getLogger("asyncio").addFilter(_DropConnectionResets())


def serve(args) -> None:
    import uvicorn

    from . import app as webapp
    from .monitor import Monitor
    from .worker import Worker

    cfg = config.load()
    db.reset_interrupted()
    work = Worker()
    mon = Monitor(on_new=work.wake.set)
    webapp.hooks["wake_worker"] = work.wake.set
    webapp.hooks["poll_now"] = mon.poll_now.set
    work.start()
    mon.start()

    host, port = cfg["dashboard"]["host"], int(cfg["dashboard"]["port"])
    if cfg["dashboard"]["open_browser"] and not args.no_browser:
        threading.Timer(1.5, webbrowser.open, args=(f"http://{host}:{port}",)).start()
    uvicorn.run(webapp.app, host=host, port=port, log_level="warning")


def check(args) -> None:
    """Checks every external dependency without spending money on Omni."""
    cfg = config.load()
    ok = True

    def report(name, passed, detail=""):
        nonlocal ok
        ok &= passed
        print(f"  [{'OK' if passed else 'XX'}] {name}{': ' + detail if detail else ''}")

    print("Tools")
    report("ffmpeg", bool(shutil.which("ffmpeg")))
    report("ffprobe", bool(shutil.which("ffprobe")))
    from .downloader import ytdlp
    res = ytdlp("--version")
    report("yt-dlp", res.returncode == 0, res.stdout.strip())
    res = ytdlp("--list-impersonate-targets")
    report("yt-dlp browser impersonation (curl_cffi)", "chrome" in res.stdout.lower() and "curl_cffi" in res.stdout,
           "" if "curl_cffi" in res.stdout else "run: py -m pip install -U \"yt-dlp[default,curl-cffi]\"")

    print("Config")
    report("creator.handle", bool(cfg["creator"]["handle"]), cfg["creator"]["handle"] or "empty in config.yaml")
    from . import music
    report("music tracks", True, f"{len(music.list_tracks())} in {config.MUSIC_DIR}")
    from .downloader import cookie_args
    login = cookie_args(cfg)
    report("TikTok login for restricted posts", True,
           " ".join(login) if login else "none (restricted posts fall back to TikWM; optional)")

    print("Google (Vertex AI)")
    try:
        from google.genai import types

        from .highlight import make_client
        client = make_client(cfg)
        tokens = client.models.count_tokens(model=cfg["gemini"]["model"],
                                            contents=[types.Part(text="hello")])
        report(f"Gemini {cfg['gemini']['model']}", True, f"count_tokens={tokens.total_tokens}")
    except Exception as err:  # noqa: BLE001
        report(f"Gemini {cfg['gemini']['model']}", False, str(err)[:300])

    if cfg["transform"]["enabled"]:
        try:
            from .transform_adapter import load_script
            mod = load_script(cfg["transform"]["script_path"])
            report("transform script", cfg["transform"]["variant"] in mod.EDIT_INSTRUCTION_VARIANTS,
                   f"variant {cfg['transform']['variant']!r}")
        except Exception as err:  # noqa: BLE001
            report("transform script", False, str(err)[:300])

    print("Upload-Post")
    try:
        from .publisher import list_profiles
        body = list_profiles()
        profiles = body.get("profiles") or body.get("users") or []
        mine = next((p for p in profiles if p.get("username") == cfg["upload_post"]["profile"]), None)
        if mine is None:
            report(f"profile {cfg['upload_post']['profile']!r}", False,
                   f"not found; profiles: {[p.get('username') for p in profiles]}")
        else:
            social = mine.get("social_accounts") or {}
            for platform in cfg["upload_post"]["platforms"]:
                keys = ("x", "twitter") if platform == "x" else (platform,)
                if isinstance(social, dict):
                    linked = any(social.get(k) for k in keys)
                else:
                    linked = any(k in str(social).lower() for k in keys)
                report(f"{platform} connected", bool(linked))
    except Exception as err:  # noqa: BLE001
        report("Upload-Post API", False, str(err)[:300])

    print("\nAll good." if ok else "\nFix the XX items above.")
    sys.exit(0 if ok else 1)


def list_new(args) -> None:
    from .monitor import list_latest
    for video in list_latest():
        known = db.get(video["id"])
        print(f"{video['id']}  {'(' + known['state'] + ')' if known else 'NEW':12}  {video['url']}")


def run(args) -> None:
    """Push one video through the pipeline in the foreground, stopping at --until."""
    from . import worker
    from .downloader import resolve_video_id

    from . import cli_lock

    video_id, url = resolve_video_id(args.url)
    if cli_lock.running(video_id):
        print(f"Another terminal is already working on {video_id}.")
        sys.exit(1)
    # hold=1 keeps the dashboard's worker (if it's running) away from this job, and the
    # lock stops the dashboard offering Continue while this terminal is still busy.
    cli_lock.acquire(video_id)
    if db.insert_if_new(video_id, url, source="manual"):
        db.update(video_id, hold=True)
    else:
        job = db.get(video_id)
        if job["state"] in db.NEXT_STEP and not job["hold"] and not args.force:
            cli_lock.release(video_id)
            print(f"Job {video_id} is queued ({job['state']}) for the dashboard's worker, which may be "
                  "working on it right now. Use the dashboard, or pass --force if it isn't running.")
            sys.exit(1)
        if job["state"] in ("POSTING", "APPROVED"):
            cli_lock.release(video_id)
            print(f"Job {video_id} is {job['state']}; the terminal never touches posting.")
            sys.exit(1)
        db.update(video_id, hold=True)
        if job["state"] in ("SKIPPED", "REJECTED"):
            db.update(video_id, state="DETECTED")
        elif job["state"] == "FAILED" and job["failed_step"] != "post":
            db.retry(video_id)

    order = ["DETECTED", "DOWNLOADED", "HIGHLIGHTED", "CLIPPED"]
    target = {"download": "DOWNLOADED", "highlight": "HIGHLIGHTED", "clips": "CLIPPED"}[args.until]
    try:
        while True:
            job = db.get(video_id)
            if job["state"] == "FAILED":
                print(f"\nFAILED at {job['failed_step']}:\n{job['error']}")
                sys.exit(1)
            if job["state"] not in order or order.index(job["state"]) >= order.index(target):
                break
            worker.advance(job)
        if args.until == "clips":
            # cut every highlight into a preview (free); the paid transform is picked in the dashboard
            for clip in db.list_clips(parent_id=video_id, states=["NEW"]):
                if not clip["hold"]:  # queued for the dashboard's worker, which may be cutting it now
                    print(f"  skipping {clip['video_id']}: the dashboard is cutting it")
                    continue
                print(f"  cutting {clip['video_id']}: {(clip.get('info') or {}).get('title', '')}")
                worker.advance(clip)
    finally:
        cli_lock.release(video_id)
        # Release whatever reached a resting state. A video stopped early (--until
        # download/highlight) stays held until Continue is pressed in the dashboard.
        for row in [db.get(video_id)] + db.list_clips(parent_id=video_id):
            if row["state"] not in db.NEXT_STEP and row["hold"]:
                db.update(row["video_id"], hold=False)

    job = db.get(video_id)
    counts = db.clip_counts(video_id)
    print(f"\nVideo {video_id} is {job['state']}{' (held)' if job['hold'] else ''}"
          f"{f'; clips: {counts}' if counts else ''}. Files: {config.job_dir(video_id)}")
    cfg = config.load()
    print(f"Open http://{cfg['dashboard']['host']}:{cfg['dashboard']['port']}/jobs/{video_id} "
          "(with `python -m clipper` running) to "
          f"{'press Continue' if job['hold'] else 'preview the clips and pick which to make albino'}.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m clipper")
    sub = parser.add_subparsers(dest="cmd")
    p_serve = sub.add_parser("serve", help="dashboard + monitor + worker (default)")
    p_serve.add_argument("--no-browser", action="store_true")
    sub.add_parser("check", help="check tools, Google credentials and Upload-Post")
    sub.add_parser("list-new", help="print the creator's newest posts without downloading")
    p_run = sub.add_parser("run", help="find and cut the highlights of one TikTok link")
    p_run.add_argument("--url", required=True)
    p_run.add_argument("--until", default="clips", choices=["download", "highlight", "clips"],
                       help="'clips' (default) finds and cuts every highlight; the paid albino "
                            "transform is only run on the clips you pick in the dashboard")
    p_run.add_argument("--force", action="store_true",
                       help="take over a job that is queued for the dashboard's worker")
    args = parser.parse_args()

    setup_logging()
    db.init()
    try:
        {"check": check, "list-new": list_new, "run": run}.get(args.cmd, serve)(
            args if args.cmd else parser.parse_args(["serve"]))
    except config.ConfigError as err:
        print(f"\nClipper can't start: {err}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
