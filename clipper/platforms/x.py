"""X (Twitter) API v2 with your own developer keys (pay-per-use, ~$0.015 per post).

No login screen: the developer portal gives you four keys for your own account
(API key + secret, access token + secret; set the app to "Read and write" first).

Posting: chunked media upload (initialize / append / finalize / status) -> create the post.
If the connection drops while the post is being created, the retry looks for a recent
post of yours carrying that video before posting again, so it isn't posted twice.
"""

import time
from pathlib import Path

import requests
from requests_oauthlib import OAuth1

from .. import config
from . import PostError, load, result, save

API = "https://api.x.com/2"
CHUNK = 4 * 1024 * 1024
KEYS = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")
SETTLE_S = 90  # a fresh post can take a moment to show in your timeline


def _auth() -> OAuth1:
    missing = [k for k in KEYS if not config.secret(k)]
    if missing:
        raise PostError(f"X isn't connected: add {', '.join(missing)} on the Connections page", definite=True)
    return OAuth1(*(config.secret(k) for k in KEYS))


def status(cfg: dict) -> dict:
    missing = [k for k in KEYS if not config.secret(k)]
    conn = load("x")
    return {"connected": not missing and bool(conn.get("user_id")), "account": conn.get("username", ""),
            "missing": missing}


def _call(method: str, path: str, **kwargs) -> dict:
    resp = requests.request(method, f"{API}{path}", auth=_auth(), timeout=120, **kwargs)
    body = resp.json() if resp.content else {}
    if resp.status_code >= 400:
        detail = body.get("detail") or body.get("title") or (body.get("errors") or [{}])[0].get("message") \
            or resp.text[:300]
        if resp.status_code == 402:
            detail = f"{detail} (your X developer account needs credit for pay-per-use)"
        raise PostError(f"X {resp.status_code}: {detail}",
                        definite=resp.status_code < 500 and resp.status_code != 429)
    return body


def verify(cfg: dict = None) -> dict:
    """Check the keys and remember which account they belong to (one small paid read)."""
    me = _call("GET", "/users/me").get("data") or {}
    save("x", {"user_id": me.get("id"), "username": me.get("username", "")})
    return load("x")


def disconnect() -> None:
    save("x", {})


def _upload(video: Path) -> str:
    size = video.stat().st_size
    media_id = _call("POST", "/media/upload/initialize", json={
        "media_type": "video/mp4", "total_bytes": size, "media_category": "tweet_video"})["data"]["id"]
    with open(video, "rb") as f:
        index = 0
        while True:
            piece = f.read(CHUNK)
            if not piece:
                break
            _call("POST", f"/media/upload/{media_id}/append",
                  data={"segment_index": str(index)}, files={"media": ("chunk", piece, "application/octet-stream")})
            index += 1
    info = _call("POST", f"/media/upload/{media_id}/finalize").get("data") or {}
    started = time.monotonic()
    while (info.get("processing_info") or {}).get("state") in ("pending", "in_progress"):
        if time.monotonic() - started > 600:
            raise PostError("X is taking too long to process the video")
        time.sleep(max(2, int(info["processing_info"].get("check_after_secs", 3))))
        info = _call("GET", "/media/upload", params={"command": "STATUS", "media_id": media_id}).get("data") or {}
    if (info.get("processing_info") or {}).get("state") == "failed":
        error = (info["processing_info"].get("error") or {}).get("message", "processing failed")
        raise PostError(f"X couldn't process the video: {error}", definite=True)
    return media_id


def post(job: dict, video: Path, caption: str, ai_label: bool, cfg: dict, on_submitted=None) -> dict:
    conn = load("x") or verify(cfg)
    media_id = _upload(video)
    if on_submitted:
        on_submitted({"media_id": media_id, "user_id": conn.get("user_id"), "at": time.time()})
    created = _call("POST", "/tweets", json={"text": caption[:280], "media": {"media_ids": [media_id]}})
    tweet_id = (created.get("data") or {}).get("id")
    return result(True, f"https://x.com/{conn.get('username') or 'i'}/status/{tweet_id}", "Posted")


def check(handle: dict, cfg: dict):
    """Was the post created after all? Look for one of your recent posts with this video."""
    data = _call("GET", f"/users/{handle['user_id']}/tweets", params={
        "max_results": 10, "expansions": "attachments.media_keys", "media.fields": "media_key"})
    for tweet in data.get("data") or []:
        keys = (tweet.get("attachments") or {}).get("media_keys") or []
        if any(k.split("_", 1)[-1] == str(handle["media_id"]) for k in keys):
            username = load("x").get("username") or "i"
            return result(True, f"https://x.com/{username}/status/{tweet['id']}", "Posted")
    if time.time() - handle.get("at", 0) < SETTLE_S:
        return None  # too soon to be sure it isn't there
    # Not posted: report it as a failure, so a retry posts it (with a fresh upload).
    return result(False, "", "The post wasn't created; retry to post it")
