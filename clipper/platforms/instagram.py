"""Instagram Reels through Meta's Graph API, with your own free Meta developer app.

Uploading a file straight from this PC (no public web address) needs the "Facebook
Login for Business" flavour of the API, so the Instagram account must be a
Professional account linked to a Facebook Page. While the Meta app stays in
Development mode it can post to accounts that have a role on the app (yours),
with no App Review.

Posting: create a REELS container (upload_type=resumable) -> send the file to
rupload.facebook.com -> wait until the container is FINISHED -> media_publish.
"""

import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

from .. import config
from . import PostError, load, result, save

SCOPES = "instagram_basic,instagram_content_publish,pages_show_list,pages_read_engagement,business_management"
_pending_logins = {}  # state -> created


def _graph(cfg: dict) -> str:
    return f"https://graph.facebook.com/{cfg['posting']['graph_api_version']}"


def redirect_uri(cfg: dict) -> str:
    return f"http://localhost:{cfg['dashboard']['port']}/connect/instagram/callback"


def status(cfg: dict) -> dict:
    missing = [n for n in ("META_APP_ID", "META_APP_SECRET") if not config.secret(n)]
    conn = load("instagram")
    return {"connected": bool(conn.get("token") and conn.get("ig_user_id")) and not missing,
            "account": conn.get("username", ""), "missing": missing, "redirect_uri": redirect_uri(cfg),
            "expires": conn.get("expires_at")}


# ------------------------------------------------------------------- login

def login_url(cfg: dict) -> str:
    state = secrets.token_urlsafe(16)
    _pending_logins[state] = time.time()
    version = cfg["posting"]["graph_api_version"]
    return f"https://www.facebook.com/{version}/dialog/oauth?" + urlencode({
        "client_id": config.secret("META_APP_ID"), "redirect_uri": redirect_uri(cfg), "state": state,
        "scope": SCOPES, "response_type": "code"})


def _get(url: str, params: dict) -> dict:
    resp = requests.get(url, params=params, timeout=60)
    body = resp.json() if resp.content else {}
    if resp.status_code >= 400 or "error" in body:
        err = body.get("error") or {}
        raise PostError(f"Meta: {err.get('message') or resp.text[:300]}", definite=resp.status_code < 500)
    return body


def finish_login(code: str, state: str, cfg: dict) -> dict:
    if time.time() - _pending_logins.pop(state, 0) > 900:
        raise PostError("That Instagram login link expired; click Connect Instagram again", definite=True)
    app = {"client_id": config.secret("META_APP_ID"), "client_secret": config.secret("META_APP_SECRET")}
    short = _get(f"{_graph(cfg)}/oauth/access_token", {**app, "redirect_uri": redirect_uri(cfg), "code": code})
    long = _get(f"{_graph(cfg)}/oauth/access_token", {**app, "grant_type": "fb_exchange_token",
                                                      "fb_exchange_token": short["access_token"]})
    pages = _get(f"{_graph(cfg)}/me/accounts", {
        "access_token": long["access_token"],
        "fields": "name,access_token,instagram_business_account{id,username}"}).get("data") or []
    linked = [p for p in pages if p.get("instagram_business_account")]
    if not linked:
        raise PostError("None of your Facebook Pages has an Instagram Professional account linked. In the "
                        "Instagram app: Settings > Account type > switch to Professional, then link it to a "
                        "Facebook Page, and connect again.", definite=True)
    page = linked[0]
    ig = page["instagram_business_account"]
    # A Page token made from a long-lived user token doesn't expire.
    save("instagram", {"token": page["access_token"], "ig_user_id": ig["id"], "username": ig.get("username", ""),
                       "page_name": page.get("name", ""), "expires_at": None})
    return load("instagram")


def disconnect() -> None:
    save("instagram", {})


# ------------------------------------------------------------------- posting

def _conn() -> dict:
    conn = load("instagram")
    if not conn.get("token"):
        raise PostError("Instagram isn't connected: open Connections and click Connect Instagram", definite=True)
    return conn


def post(job: dict, video: Path, caption: str, ai_label: bool, cfg: dict, on_submitted=None) -> dict:
    conn = _conn()
    resp = requests.post(f"{_graph(cfg)}/{conn['ig_user_id']}/media", timeout=60, data={
        "media_type": "REELS", "upload_type": "resumable", "caption": caption[:2200],
        "share_to_feed": "true", "access_token": conn["token"]})
    body = resp.json() if resp.content else {}
    if resp.status_code >= 400 or "id" not in body:
        err = (body.get("error") or {}).get("message") or resp.text[:300]
        raise PostError(f"Instagram: {err}", definite=resp.status_code < 500)
    container = body["id"]
    upload_uri = body.get("uri") or f"https://rupload.facebook.com/ig-api-upload/" \
                                     f"{cfg['posting']['graph_api_version']}/{container}"
    with open(video, "rb") as f:
        up = requests.post(upload_uri, data=f, timeout=900, headers={
            "Authorization": f"OAuth {conn['token']}", "offset": "0", "file_size": str(video.stat().st_size)})
    if up.status_code >= 400:
        raise PostError(f"Instagram upload failed: {up.status_code} {up.text[:300]}")
    if on_submitted:
        on_submitted({"container": container})
    return wait({"container": container}, cfg)


def check(handle: dict, cfg: dict):
    """Container FINISHED -> publish it; PUBLISHED -> done. Publishing an already-published
    container is refused by Meta, so a retry after a crash can't post twice."""
    conn = _conn()
    info = _get(f"{_graph(cfg)}/{handle['container']}", {"fields": "status_code,status",
                                                          "access_token": conn["token"]})
    code = info.get("status_code")
    if code == "IN_PROGRESS":
        return None
    if code in ("ERROR", "EXPIRED"):
        return result(False, "", f"Instagram couldn't process the video ({code}): {info.get('status', '')}")
    if code == "PUBLISHED":
        return result(True, f"https://www.instagram.com/{conn.get('username', '')}/reels/", "Posted")
    if code == "FINISHED":
        published = requests.post(f"{_graph(cfg)}/{conn['ig_user_id']}/media_publish", timeout=60,
                                  data={"creation_id": handle["container"], "access_token": conn["token"]})
        body = published.json() if published.content else {}
        if published.status_code >= 400 or "id" not in body:
            raise PostError(f"Instagram publish failed: {(body.get('error') or {}).get('message') or published.text[:300]}")
        link = _get(f"{_graph(cfg)}/{body['id']}", {"fields": "permalink", "access_token": conn["token"]})
        return result(True, link.get("permalink", ""), "Posted")
    return None


def wait(handle: dict, cfg: dict, timeout_s: float = 900) -> dict:
    started = time.monotonic()
    while time.monotonic() - started < timeout_s:
        time.sleep(8)
        try:
            done = check(handle, cfg)
        except PostError as err:
            if err.definite:
                raise
            continue
        if done is not None:
            return done
    return {"pending": True}
