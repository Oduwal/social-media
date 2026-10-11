"""TikTok Content Posting API, with your own free TikTok developer app.

Two modes (config posting.tiktok_mode):
- "inbox": the clip lands in your TikTok inbox as a draft; you tap the notification,
  add a sound and post. Works without TikTok auditing your app.
- "direct": posted automatically. Until TikTok audits the app, only as private (SELF_ONLY).

Login: Login Kit for desktop (PKCE with a hex SHA-256 challenge; redirect to this PC).
"""

import hashlib
import math
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

from .. import config
from . import PostError, load, result, save

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com/v2"
SCOPES = {"inbox": "user.info.basic,video.upload",                 # drafts: on by default for every app
          "direct": "user.info.basic,video.upload,video.publish"}  # needs "Direct Post" switched on
MAX_CHUNK = 64 * 1024 * 1024
_pending_logins = {}  # state -> (code_verifier, created)


def redirect_uri(cfg: dict) -> str:
    return f"http://localhost:{cfg['dashboard']['port']}/connect/tiktok/callback/"


def status(cfg: dict) -> dict:
    missing = [n for n in ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET") if not config.secret(n)]
    conn = load("tiktok")
    return {"connected": bool(conn.get("refresh_token")) and not missing, "account": conn.get("display_name", ""),
            "missing": missing, "mode": cfg["posting"]["tiktok_mode"],
            "expires": conn.get("refresh_expires_at"), "redirect_uri": redirect_uri(cfg)}


# ------------------------------------------------------------------- login

def login_url(cfg: dict) -> str:
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(16)
    _pending_logins[state] = (verifier, time.time())
    return AUTH_URL + "?" + urlencode({
        "client_key": config.secret("TIKTOK_CLIENT_KEY"),
        # only ask for what the app has: a scope the app lacks makes TikTok refuse the login
        "scope": SCOPES.get(cfg["posting"]["tiktok_mode"], SCOPES["inbox"]), "response_type": "code",
        "redirect_uri": redirect_uri(cfg), "state": state,
        # TikTok's desktop flow wants the SHA-256 *hex* digest, not base64url
        "code_challenge": hashlib.sha256(verifier.encode()).hexdigest(), "code_challenge_method": "S256"})


def finish_login(code: str, state: str, cfg: dict) -> dict:
    verifier, created = _pending_logins.pop(state, (None, 0))
    if not verifier or time.time() - created > 900:
        raise PostError("That TikTok login link expired; click Connect TikTok again", definite=True)
    tokens = _token({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(cfg),
                     "code_verifier": verifier})
    info = _user_info(tokens["access_token"])
    _store(tokens, info.get("display_name", ""))
    return load("tiktok")


def disconnect() -> None:
    save("tiktok", {})


def _token(params: dict) -> dict:
    resp = requests.post(f"{API}/oauth/token/", timeout=30,
                         headers={"Content-Type": "application/x-www-form-urlencoded"},
                         data={"client_key": config.secret("TIKTOK_CLIENT_KEY"),
                               "client_secret": config.secret("TIKTOK_CLIENT_SECRET"), **params})
    body = resp.json()
    if resp.status_code >= 400 or body.get("error") or "access_token" not in body:
        raise PostError(f"TikTok login failed: {body.get('error_description') or body.get('error') or resp.text[:300]}",
                        definite=True)
    return body


def _store(tokens: dict, display_name: str = None) -> None:
    now = time.time()
    previous = load("tiktok")
    save("tiktok", {
        "access_token": tokens["access_token"], "refresh_token": tokens["refresh_token"],
        "expires_at": now + int(tokens.get("expires_in", 86400)) - 120,
        "refresh_expires_at": now + int(tokens.get("refresh_expires_in", 31536000)),
        "open_id": tokens.get("open_id", previous.get("open_id")), "scope": tokens.get("scope", ""),
        "display_name": display_name if display_name is not None else previous.get("display_name", "")})


def _user_info(access_token: str) -> dict:
    resp = requests.get(f"{API}/user/info/", params={"fields": "open_id,display_name"},
                        headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    return ((resp.json() or {}).get("data") or {}).get("user") or {}


def access_token() -> str:
    conn = load("tiktok")
    if not conn.get("refresh_token"):
        raise PostError("TikTok isn't connected: open Connections and click Connect TikTok", definite=True)
    if time.time() < conn.get("expires_at", 0):
        return conn["access_token"]
    _store(_token({"grant_type": "refresh_token", "refresh_token": conn["refresh_token"]}))
    return load("tiktok")["access_token"]


# ------------------------------------------------------------------- posting

def _api(path: str, body: dict) -> dict:
    resp = requests.post(f"{API}{path}", json=body, timeout=60,
                         headers={"Authorization": f"Bearer {access_token()}",
                                  "Content-Type": "application/json; charset=UTF-8"})
    data = resp.json() if resp.content else {}
    error = data.get("error") or {}
    if resp.status_code >= 400 or (error.get("code") not in (None, "ok")):
        code = error.get("code", resp.status_code)
        definite = resp.status_code < 500 and code not in ("rate_limit_exceeded", "internal_error")
        raise PostError(f"TikTok {code}: {error.get('message') or resp.text[:300]}", definite=definite)
    return data.get("data") or {}


def chunk_plan(size: int) -> tuple:
    """(chunk_size, total_chunk_count) by TikTok's rules: up to 64 MB goes up in one piece
    (under 5 MB it must); bigger files in 32 MB chunks, the remainder riding on the last
    chunk (which may be up to 128 MB)."""
    if size <= MAX_CHUNK:
        return size, 1
    chunk = 32 * 1024 * 1024
    return chunk, math.floor(size / chunk)


def _upload(upload_url: str, video: Path) -> None:
    size = video.stat().st_size
    chunk, count = chunk_plan(size)
    with open(video, "rb") as f:
        for i in range(count):
            start = i * chunk
            end = size - 1 if i == count - 1 else start + chunk - 1
            f.seek(start)
            data = f.read(end - start + 1)
            resp = requests.put(upload_url, data=data, timeout=300, headers={
                "Content-Type": "video/mp4", "Content-Length": str(len(data)),
                "Content-Range": f"bytes {start}-{end}/{size}"})
            if resp.status_code not in (200, 201, 206):
                raise PostError(f"TikTok upload of part {i + 1}/{count} failed: {resp.status_code} {resp.text[:200]}")


def creator_info() -> dict:
    """The connected account's name, the privacy levels TikTok allows it, and which of
    comments/duets/stitches it has switched off."""
    return _api("/post/publish/creator_info/query/", {})


def post(job: dict, video: Path, caption: str, ai_label: bool, cfg: dict, on_submitted=None) -> dict:
    size = video.stat().st_size
    chunk, count = chunk_plan(size)
    source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count}
    if cfg["posting"]["tiktok_mode"] == "direct":
        info = creator_info()
        options = info.get("privacy_level_options") or ["SELF_ONLY"]
        picked = job.get("tiktok_opts") or {}
        if picked.get("privacy"):  # chosen on the clip page
            if picked["privacy"] not in options:
                raise PostError(f"TikTok doesn't offer '{picked['privacy']}' for this account right now; "
                                "choose another privacy level on the clip page", definite=True)
            privacy = picked["privacy"]
        else:
            wanted = cfg["upload_post"]["tiktok_privacy_level"]
            privacy = wanted if wanted in options else ("SELF_ONLY" if "SELF_ONLY" in options else options[0])
        post_info = {
            "title": caption[:2200], "privacy_level": privacy, "is_aigc": bool(ai_label),
            "disable_comment": not picked.get("comment", False) or bool(info.get("comment_disabled")),
            "disable_duet": not picked.get("duet", False) or bool(info.get("duet_disabled")),
            "disable_stitch": not picked.get("stitch", False) or bool(info.get("stitch_disabled"))}
        if picked.get("commercial"):
            post_info.update({"brand_content_toggle": bool(picked.get("branded")),
                              "brand_organic_toggle": bool(picked.get("own_brand"))})
        data = _api("/post/publish/video/init/", {"source_info": source, "post_info": post_info})
    else:
        data = _api("/post/publish/inbox/video/init/", {"source_info": source})
    publish_id = data["publish_id"]
    _upload(data["upload_url"], video)
    if on_submitted:
        on_submitted({"publish_id": publish_id})
    return wait({"publish_id": publish_id}, cfg)


def check(handle: dict, cfg: dict):
    data = _api("/post/publish/status/fetch/", {"publish_id": handle["publish_id"]})
    state = data.get("status")
    if state == "SEND_TO_USER_INBOX":
        return result(True, "", "Sent to your TikTok inbox: open TikTok, tap the notification, add your "
                                "sound and caption, and post", drafted=True)
    if state == "PUBLISH_COMPLETE":
        ids = data.get("publicaly_available_post_id") or []
        url = f"https://m.tiktok.com/v/{ids[0]}.html" if ids else ""  # redirects to the post
        message = "" if ids else "Posted (private until TikTok audits your app)" \
            if cfg["posting"]["tiktok_mode"] == "direct" else "Posted"
        return result(True, url, message)
    if state == "FAILED":
        return result(False, "", f"TikTok rejected it: {data.get('fail_reason', 'unknown reason')}")
    return None  # still processing


def wait(handle: dict, cfg: dict, timeout_s: float = 600) -> dict:
    started = time.monotonic()
    while time.monotonic() - started < timeout_s:
        time.sleep(5)
        try:
            done = check(handle, cfg)
        except PostError:
            continue
        if done is not None:
            return done
    return {"pending": True}
