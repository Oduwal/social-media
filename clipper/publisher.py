"""Post an approved clip to TikTok, Instagram Reels and X through Upload-Post.

One multipart upload of the local file; Upload-Post fans it out. Status is
polled until every platform reports back.
"""

import logging
import time
from pathlib import Path

import requests

from . import config

log = logging.getLogger(__name__)

API = "https://api.upload-post.com/api"
POLL_EVERY_S = 10
POLL_TIMEOUT_S = 20 * 60
DONE = {"completed", "failed", "not_found"}


class PublishError(RuntimeError):
    def __init__(self, message: str, definite: bool = False):
        super().__init__(message)
        # True when Upload-Post clearly rejected the request (nothing was posted),
        # so a retry may use a fresh Idempotency-Key.
        self.definite = definite


def _headers() -> dict:
    key = config.secret("UPLOAD_POST_API_KEY")
    if not key:
        raise PublishError("UPLOAD_POST_API_KEY is not set in .env")
    return {"Authorization": f"Apikey {key}"}


def list_profiles() -> dict:
    resp = requests.get(f"{API}/uploadposts/users", headers=_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


def build_form(captions: dict, platforms: list, cfg: dict, ai_label: bool) -> list:
    ucfg = cfg["upload_post"]
    first = next((captions.get(p) for p in platforms if captions.get(p)), "")
    form = [("user", ucfg["profile"]), ("title", first), ("async_upload", "true")]
    for platform in platforms:
        form.append(("platform[]", platform))
    if "tiktok" in platforms:
        form += [("tiktok_title", captions.get("tiktok", first)),
                 ("post_mode", "DIRECT_POST"),
                 ("privacy_level", ucfg["tiktok_privacy_level"]),
                 ("is_aigc", str(ai_label).lower())]
    if "instagram" in platforms:
        form += [("instagram_title", captions.get("instagram", first)),
                 ("media_type", "REELS"), ("share_to_feed", "true"),
                 ("is_ai_generated", str(ai_label).lower())]
    if "x" in platforms:
        form += [("x_title", captions.get("x", first)[:280]),
                 ("made_with_ai", str(ai_label).lower())]
    return form


def sound_fields(sound: dict, clip_length: float) -> list:
    """Upload-Post fields that attach a trending TikTok sound to the TikTok post."""
    start_ms = int(float(sound.get("start_s") or 0) * 1000)
    return [("tiktok_music_id", str(sound["id"])),
            ("tiktok_music_volume", str(int(sound.get("volume", 35)))),
            ("tiktok_original_sound_volume", str(int(sound.get("original_volume", 100)))),
            ("tiktok_music_start", str(start_ms)),
            ("tiktok_music_end", str(start_ms + int(clip_length * 1000)))]


def publish(video: Path, video_id: str, attempt: int, captions: dict, platforms: list,
            ai_label: bool, cfg: dict, on_submitted=None, extra: list = None, key_suffix: str = "") -> dict:
    """`attempt` is part of the Idempotency-Key: the caller keeps it the same when
    retrying an upload whose outcome is unknown, so Upload-Post can drop the duplicate.
    `on_submitted(request_id)` is called as soon as the upload is accepted, so the id
    is saved before the long status wait. `extra` adds form fields (e.g. a TikTok sound);
    `key_suffix` keeps keys distinct when one clip is uploaded in several requests."""
    if not platforms:
        raise PublishError("No platforms selected", definite=True)
    form = build_form(captions, platforms, cfg, ai_label) + list(extra or [])
    form.append(("external_id", video_id))
    headers = {**_headers(), "Idempotency-Key": f"{video_id}-{attempt}{key_suffix}"}
    with open(video, "rb") as f:
        resp = requests.post(f"{API}/upload", headers=headers, data=form,
                             files={"video": (video.name, f, "video/mp4")}, timeout=900)
    if resp.status_code >= 400:
        definite = resp.status_code < 500 and resp.status_code not in (408, 409, 429)
        raise PublishError(f"Upload-Post {resp.status_code}: {resp.text[:1000]}", definite=definite)
    body = resp.json()
    request_id = body.get("request_id")
    if not request_id:
        return summarize(body)
    if on_submitted:
        on_submitted(request_id)
    return wait_for(request_id)


def wait_for(request_id: str) -> dict:
    """Poll until done. Network or server hiccups while polling are not failures of
    the upload, so they only end in a 'timeout' result that keeps the request_id."""
    started = time.monotonic()
    while time.monotonic() - started < POLL_TIMEOUT_S:
        time.sleep(POLL_EVERY_S)
        try:
            result = check_status(request_id)
        except Exception as err:  # noqa: BLE001
            log.warning("Status check for %s failed, will keep trying: %s", request_id, err)
            continue
        if result["status"] in DONE:
            return result
    return pending(request_id, "still processing after 20 min; check the Upload-Post dashboard")


def pending(request_id: str, note: str) -> dict:
    return {"request_id": request_id, "status": "timeout", "platforms": {}, "raw": {"note": note}}


def check_status(request_id: str) -> dict:
    resp = requests.get(f"{API}/uploadposts/status", headers=_headers(),
                        params={"request_id": request_id}, timeout=30)
    resp.raise_for_status()
    result = summarize(resp.json(), request_id)
    if result["status"] not in DONE:
        result["status"] = "timeout"  # still in flight: treat like a timed-out wait
    return result


def summarize(body: dict, request_id: str = None) -> dict:
    """Normalise Upload-Post's response into {platform: {ok, url, message, drafted}}."""
    results = body.get("results") or {}
    if isinstance(results, dict):
        items = [{"platform": k, **(v if isinstance(v, dict) else {"message": str(v)})}
                 for k, v in results.items()]
    else:
        items = list(results)
    platforms = {}
    for item in items:
        name = item.get("platform") or item.get("network") or "unknown"
        platforms[name] = {
            "ok": bool(item.get("success")),
            "url": item.get("url") or item.get("post_url") or item.get("permalink") or "",
            "message": item.get("message") or item.get("error") or "",
            "drafted": bool(item.get("fallback_to_inbox")),
            "skipped": bool(item.get("skipped")),
        }
    return {"request_id": request_id or body.get("request_id"),
            "status": body.get("status") or ("completed" if body.get("success") else "unknown"),
            "platforms": platforms, "raw": body}
