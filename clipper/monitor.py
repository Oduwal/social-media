"""Watch the creator's TikTok profile and queue new posts.

yt-dlp lists the newest posts; ids not yet in the DB become DETECTED jobs.
Listing by `tiktokuser:<secUid>` is preferred over the @handle URL because the
handle -> secUid lookup is the part of yt-dlp's TikTok extractor that breaks
most often (yt-dlp issue #17500). Apify is a paid fallback when yt-dlp fails.
"""

import json
import logging
import random
import subprocess
import sys
import threading
import time

import requests

from . import config, db
from .downloader import ytdlp

log = logging.getLogger(__name__)

APIFY_ACTOR = "clockworks~tiktok-profile-scraper"
BACKOFF_MINUTES = [30, 60, 120]
UPDATE_EVERY_S = 24 * 3600
CATCHUP_DEPTH = 60  # posts scanned on the first poll after the monitor has been offline


class MonitorError(RuntimeError):
    pass


def _entries_from_ytdlp(target: str, limit: int) -> list:
    result = ytdlp("--flat-playlist", "--playlist-end", str(limit), "-J", target)
    if result.returncode != 0:
        raise MonitorError(result.stderr.strip()[-800:] or "yt-dlp failed")
    playlist = json.loads(result.stdout)
    videos = []
    for entry in playlist.get("entries") or []:
        vid = str(entry.get("id") or "")
        if vid.isdigit():
            videos.append({"id": vid, "url": entry.get("url") or entry.get("webpage_url") or ""})
    if not videos:
        raise MonitorError("yt-dlp returned an empty list (TikTok may be blocking; see yt-dlp #17500)")
    return videos


def _entries_from_apify(handle: str, limit: int) -> list:
    token = config.secret("APIFY_TOKEN")
    if not token:
        raise MonitorError("no APIFY_TOKEN set")
    resp = requests.post(
        f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items",
        params={"token": token},
        json={"profiles": [handle], "resultsPerPage": limit, "profileSorting": "latest",
              "shouldDownloadVideos": False, "shouldDownloadCovers": False},
        timeout=300,
    )
    resp.raise_for_status()
    return [{"id": str(item["id"]), "url": item.get("webVideoUrl") or ""}
            for item in resp.json() if str(item.get("id", "")).isdigit()]


def sec_uid_key(handle: str) -> str:
    return f"sec_uid:{handle.lower()}"


def video_time(video_id: str) -> int:
    """TikTok ids are time-ordered: the top 32 bits are the post's Unix timestamp."""
    return int(video_id) >> 32


def list_latest(cfg: dict = None, handle: str = None) -> list:
    """Newest posts as [{"id", "url"}], trying each source in turn."""
    cfg = cfg or config.load()
    handle = handle or cfg["creator"]["handle"]
    if not handle:
        raise MonitorError("Set a creator to watch (config.yaml or the dashboard)")
    limit = int(cfg["monitor"]["playlist_end"])
    last_ok = (db.kv_get("monitor_status", {}) or {}).get("last_ok")
    if last_ok and time.time() - last_ok > 3 * cfg["monitor"]["interval_minutes"] * 60:
        limit = max(limit, CATCHUP_DEPTH)  # PC was off a while: look further back so nothing is missed
    sec_uid = db.kv_get(sec_uid_key(handle))
    errors = []

    targets = ([f"tiktokuser:{sec_uid}"] if sec_uid else []) + [f"https://www.tiktok.com/@{handle}"]
    for target in targets:
        try:
            videos = _entries_from_ytdlp(target, limit)
            break
        except Exception as err:  # noqa: BLE001
            errors.append(f"yt-dlp {target}: {err}")
    else:
        try:
            videos = _entries_from_apify(handle, limit)
        except Exception as err:  # noqa: BLE001
            errors.append(f"apify: {err}")
            raise MonitorError("\n".join(errors)) from err

    for video in videos:
        if not video["url"]:
            video["url"] = f"https://www.tiktok.com/@{handle}/video/{video['id']}"
    if not sec_uid:
        _learn_sec_uid(videos[0]["url"], handle)
    return videos


def _learn_sec_uid(video_url: str, handle: str) -> None:
    """The secUid (channel_id) only shows up on a full single-video extraction."""
    try:
        result = ytdlp("-j", "--skip-download", video_url, timeout=120)
        info = json.loads(result.stdout.strip().splitlines()[-1])
        remember_sec_uid(info.get("channel_id"), info.get("uploader"), handle)
    except Exception as err:  # noqa: BLE001
        log.info("Couldn't learn secUid yet: %s", err)


def remember_sec_uid(channel_id: str, uploader: str, handle: str) -> None:
    """Saved per handle, so changing creator.handle never keeps watching the old creator."""
    if channel_id and (uploader or "").lower() == handle.lower() and not db.kv_get(sec_uid_key(handle)):
        db.kv_set(sec_uid_key(handle), channel_id)
        log.info("Saved secUid for @%s", handle)


def poll_once(cfg: dict = None) -> int:
    """Poll every enabled creator; returns how many new videos were queued across all of them.
    One creator failing (a blocked listing) doesn't stop the others."""
    cfg = cfg or config.load()
    handles = [c["handle"] for c in cfg.get("creators") or [] if c.get("enabled", True) and c["handle"]]
    if not handles:
        handles = [cfg["creator"]["handle"]] if cfg["creator"]["handle"] else []
    new, errors = 0, []
    for handle in handles:
        try:
            new += poll_creator(cfg, handle)
        except Exception as err:  # noqa: BLE001
            errors.append(f"@{handle}: {err}")
            log.warning("Couldn't poll @%s: %s", handle, str(err)[:200])
    if errors and new == 0 and len(errors) == len(handles):
        raise MonitorError("\n".join(errors))  # all creators failed: surface it on the dashboard
    return new


def poll_creator(cfg: dict, handle: str) -> int:
    """One creator. The first poll for a handle records when watching started; any post from
    BEFORE that moment is backlog (SKIPPED), however it turns up later."""
    videos = list_latest(cfg, handle)
    seeds = db.kv_get("seeded_at", {}) or {}
    watching_since = seeds.get(handle.lower())
    if watching_since is None:
        watching_since = time.time()
        seeds[handle.lower()] = watching_since
        db.kv_set("seeded_at", seeds)
        log.info("Started watching @%s: %d existing posts recorded as skipped", handle, len(videos))

    new = 0
    for video in videos:
        is_new = video_time(video["id"]) >= watching_since
        state = "DETECTED" if is_new else "SKIPPED"
        if db.insert_if_new(video["id"], video["url"], state=state, source="monitor", handle=handle) and is_new:
            new += 1
            log.info("New post from @%s: %s", handle, video["url"])
    return new


def update_ytdlp() -> None:
    log.info("Updating yt-dlp")
    try:
        result = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U",
                                 "yt-dlp[default,curl-cffi]"],
                                capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            log.warning("yt-dlp update failed: %s", result.stderr[-500:])
    except subprocess.TimeoutExpired:
        log.warning("yt-dlp update timed out")
    db.kv_set("ytdlp_updated_at", time.time())


class Monitor(threading.Thread):
    def __init__(self, on_new=None):
        super().__init__(name="monitor", daemon=True)
        self.stop_event = threading.Event()
        self.poll_now = threading.Event()
        self.on_new = on_new
        self.failures = 0

    def _status(self, **fields):
        status = db.kv_get("monitor_status", {}) or {}
        status.update(fields)
        db.kv_set("monitor_status", status)

    def run(self):
        while not self.stop_event.is_set():
            try:
                wait = self._tick()
            except Exception as err:  # noqa: BLE001 - e.g. a typo in config.yaml; keep watching
                log.exception("Monitor loop error")
                self._status(ok=False, message=f"Monitor error: {str(err)[:400]}", last_error=time.time())
                wait = 300
            self._wait(wait)

    def _tick(self) -> float:
        """One poll. Returns how long to wait before the next one."""
        cfg = config.load()
        watching = [c for c in cfg.get("creators") or [] if c.get("enabled", True) and c["handle"]]
        if not cfg["monitor"]["enabled"] or not watching:
            self._status(ok=False, message="Monitor off: add a creator to watch on the dashboard",
                         next_poll=None)
            return 60

        if cfg["monitor"]["auto_update_ytdlp"] and                 time.time() - (db.kv_get("ytdlp_updated_at") or 0) > UPDATE_EVERY_S:
            update_ytdlp()

        try:
            new = poll_once(cfg)
            self.failures = 0
            self._status(ok=True, message=f"Last poll found {new} new post(s)", last_ok=time.time())
            if new and self.on_new:
                self.on_new()
            interval = cfg["monitor"]["interval_minutes"] * 60
            jitter = cfg["monitor"]["jitter"]
            wait = interval * random.uniform(1 - jitter, 1 + jitter)
        except Exception as err:  # noqa: BLE001
            self.failures += 1
            wait = BACKOFF_MINUTES[min(self.failures, len(BACKOFF_MINUTES)) - 1] * 60
            log.error("Monitor poll failed (%d in a row): %s", self.failures, err)
            self._status(ok=False, message=f"Poll failed {self.failures}x: {str(err)[:400]}",
                         last_error=time.time())
        self._status(next_poll=time.time() + wait)
        return wait

    def _wait(self, seconds: float):
        self.poll_now.wait(seconds)
        self.poll_now.clear()
