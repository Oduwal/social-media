"""Clipper's own posting to TikTok, Instagram and X (no Upload-Post)."""

import hashlib
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from clipper import app as webapp
from clipper import config, db, platforms, worker
from clipper.platforms import instagram, tiktok, x
from tests.conftest import make_clip, needs_ffmpeg

VID = "7412345678901234567"
CID = f"{VID}-01"
CFG = config._merge(config.DEFAULTS, {"posting": {"service": "direct", "tiktok_mode": "inbox"}})


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "data" / "jobs")
    monkeypatch.setattr(config, "ROOT", tmp_path)
    return tmp_path


def _approved(fresh_db, plats=("tiktok", "instagram", "x"), **fields):
    db.insert_if_new(VID, "u", state="CLIPPED")
    db.insert_clip(CID, db.get(VID), {"start_s": 0, "end_s": 10}, platforms=list(plats),
                   captions={"tiktok": "tt", "instagram": "ig", "x": "xx"}, **fields)
    db.update(CID, state="APPROVED")
    (config.job_dir(CID) / "final.mp4").write_bytes(b"video")
    return CID


class FakePlatform:
    def __init__(self, outcome=None, raise_after_submit=None, raise_before=None, check_result="unset"):
        self.outcome = outcome or {"ok": True, "url": "u", "message": "Posted"}
        self.raise_after_submit, self.raise_before = raise_after_submit, raise_before
        self.check_result = check_result
        self.posts, self.checks = [], []

    def post(self, job, video, caption, ai_label, cfg, on_submitted=None):
        self.posts.append(caption)
        if self.raise_before:
            raise self.raise_before
        on_submitted({"handle": len(self.posts)})
        if self.raise_after_submit:
            raise self.raise_after_submit
        return self.outcome

    def check(self, handle, cfg):
        self.checks.append(handle)
        return self.check_result


def _use(monkeypatch, **fakes):
    monkeypatch.setattr(platforms, "module", lambda name: fakes[name])


# ------------------------------------------------------------- the posting loop

def test_posts_each_platform_with_its_own_caption(data_dir, fresh_db, monkeypatch):
    fakes = {"tiktok": FakePlatform({"ok": True, "drafted": True, "message": "inbox"}),
             "instagram": FakePlatform(), "x": FakePlatform()}
    _use(monkeypatch, **fakes)
    _approved(fresh_db)
    worker.advance(db.get(CID), CFG)
    job = db.get(CID)
    assert job["state"] == "POSTED"
    assert [f.posts for f in fakes.values()] == [["tt"], ["ig"], ["xx"]]
    assert job["post_result"]["platforms"]["tiktok"]["drafted"] is True
    assert job["post_result"]["service"] == "direct"


def test_one_failure_doesnt_stop_the_others_and_retry_only_redoes_it(data_dir, fresh_db, monkeypatch):
    bad = FakePlatform(raise_before=platforms.PostError("Instagram: token expired", definite=True))
    fakes = {"tiktok": FakePlatform(), "instagram": bad, "x": FakePlatform()}
    _use(monkeypatch, **fakes)
    _approved(fresh_db)
    worker.advance(db.get(CID), CFG)
    job = db.get(CID)
    assert job["state"] == "FAILED" and "token expired" in job["error"]
    assert job["post_result"]["platforms"]["x"]["ok"] is True
    bad.raise_before = None
    db.retry(CID)
    worker.advance(db.get(CID), CFG)
    assert db.get(CID)["state"] == "POSTED"
    assert len(fakes["tiktok"].posts) == 1 and len(fakes["x"].posts) == 1 and len(bad.posts) == 2


def test_lost_connection_after_upload_is_checked_not_reuploaded(data_dir, fresh_db, monkeypatch):
    ig = FakePlatform(raise_after_submit=ConnectionError("dropped"),
                      check_result={"ok": True, "url": "https://instagram.com/p/1", "message": "Posted"})
    _use(monkeypatch, instagram=ig)
    _approved(fresh_db, plats=("instagram",))
    worker.advance(db.get(CID), CFG)
    assert db.get(CID)["post_result"]["pending"] == {"instagram": {"handle": 1}}
    db.retry(CID)
    worker.advance(db.get(CID), CFG)
    assert len(ig.posts) == 1 and ig.checks == [{"handle": 1}]
    assert db.get(CID)["state"] == "POSTED"


def test_still_processing_keeps_waiting(data_dir, fresh_db, monkeypatch):
    ig = FakePlatform(raise_after_submit=ConnectionError("dropped"), check_result=None)
    _use(monkeypatch, instagram=ig)
    _approved(fresh_db, plats=("instagram",))
    worker.advance(db.get(CID), CFG)
    db.retry(CID)
    worker.advance(db.get(CID), CFG)
    job = db.get(CID)
    assert job["state"] == "FAILED" and "still processing" in job["error"] and len(ig.posts) == 1


# ------------------------------------------------------------- manual helpers

@pytest.fixture
def client(data_dir, fresh_db):
    return TestClient(webapp.app)


def test_mark_posted_finishes_the_clip(client, fresh_db):
    _approved(fresh_db, plats=("tiktok", "x"))
    db.update(CID, state="READY")
    client.post(f"/jobs/{CID}/mark_posted", data={"platform": "tiktok"})
    assert db.get(CID)["state"] == "READY"
    client.post(f"/jobs/{CID}/mark_posted", data={"platform": "x"})
    job = db.get(CID)
    assert job["state"] == "POSTED" and job["post_result"]["platforms"]["x"]["manual"] is True


def test_forget_pending_allows_a_fresh_upload(client, fresh_db):
    _approved(fresh_db, plats=("x",))
    db.update(CID, state="FAILED", failed_step="post",
              post_result={"status": "timeout", "platforms": {}, "pending": {"x": {"media_id": "1"}}})
    client.post(f"/jobs/{CID}/forget_pending", data={"platform": "x"})
    assert db.get(CID)["post_result"]["pending"] == {}


@needs_ffmpeg
def test_clip_page_shows_connections_copy_and_manual_posting(client, fresh_db):
    _approved(fresh_db, plats=("tiktok",))
    db.update(CID, state="READY")
    make_clip(config.job_dir(CID) / "final.mp4", seconds=1)
    page = client.get(f"/jobs/{CID}").text
    assert "not connected: set up" in page and 'data-copy="caption_tiktok"' in page
    assert "Post it yourself instead" in page and "I posted it on TikTok" in page


def test_connections_page_and_saving_keys(client, data_dir):
    assert "Connect TikTok" in client.get("/connections").text
    client.post("/connections/keys", data={"TIKTOK_CLIENT_KEY": " abc ", "META_APP_ID": "", "EVIL": "x"})
    env = (data_dir / ".env").read_text()
    assert "TIKTOK_CLIENT_KEY=abc" in env and "META_APP_ID" not in env and "EVIL" not in env
    assert "abc" not in client.get("/connections").text  # saved keys are never shown back


def test_connect_without_keys_explains(client, monkeypatch):
    monkeypatch.delenv("TIKTOK_CLIENT_KEY", raising=False)
    r = client.get("/connect/tiktok", follow_redirects=False)
    assert "keys" in r.headers["location"]


# ------------------------------------------------------------- tiktok

def test_tiktok_chunk_plan_follows_the_rules():
    assert tiktok.chunk_plan(3_000_000) == (3_000_000, 1)              # < 5 MB: one piece
    assert tiktok.chunk_plan(40_000_000) == (40_000_000, 1)            # <= 64 MB: one piece allowed
    chunk, count = tiktok.chunk_plan(100 * 1024 * 1024)
    assert chunk == 32 * 1024 * 1024 and count == 3                    # remainder rides on the last chunk
    assert 100 * 1024 * 1024 - chunk * (count - 1) <= 128 * 1024 * 1024


def test_tiktok_login_url_uses_hex_pkce(data_dir, monkeypatch):
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "ck")
    q = parse_qs(urlparse(tiktok.login_url(CFG)).query)
    verifier = tiktok._pending_logins[q["state"][0]][0]
    assert q["code_challenge"][0] == hashlib.sha256(verifier.encode()).hexdigest()
    assert q["redirect_uri"][0] == "http://localhost:8000/connect/tiktok/callback/"
    assert "video.upload" in q["scope"][0]


def test_tiktok_inbox_status_means_drafted(data_dir, monkeypatch):
    monkeypatch.setattr(tiktok, "_api", lambda path, body: {"status": "SEND_TO_USER_INBOX"})
    outcome = tiktok.check({"publish_id": "p"}, CFG)
    assert outcome["ok"] and outcome["drafted"]
    monkeypatch.setattr(tiktok, "_api", lambda path, body: {"status": "FAILED", "fail_reason": "spam_risk"})
    assert tiktok.check({"publish_id": "p"}, CFG)["ok"] is False
    monkeypatch.setattr(tiktok, "_api", lambda path, body: {"status": "PROCESSING_UPLOAD"})
    assert tiktok.check({"publish_id": "p"}, CFG) is None


def test_tiktok_refreshes_an_expired_token(data_dir, monkeypatch):
    platforms.save("tiktok", {"access_token": "old", "refresh_token": "r", "expires_at": time.time() - 5})
    monkeypatch.setattr(tiktok, "_token", lambda params: {"access_token": "new", "refresh_token": "r2",
                                                          "expires_in": 86400})
    assert tiktok.access_token() == "new"
    assert platforms.load("tiktok")["refresh_token"] == "r2"


# ------------------------------------------------------------- instagram

def test_instagram_publishes_a_finished_container_once(data_dir, monkeypatch):
    platforms.save("instagram", {"token": "t", "ig_user_id": "17", "username": "me"})
    statuses = iter(["FINISHED", "PUBLISHED"])
    monkeypatch.setattr(instagram, "_get", lambda url, params: {"status_code": next(statuses)}
                        if "fields" in params and params["fields"].startswith("status") else {"permalink": "https://ig/p/1"})
    published = []
    monkeypatch.setattr(instagram.requests, "post", lambda url, data, timeout: (published.append(url),
                        SimpleNamespace(status_code=200, content=b"1", json=lambda: {"id": "m1"}))[1])
    assert instagram.check({"container": "c1"}, CFG)["url"] == "https://ig/p/1"
    assert instagram.check({"container": "c1"}, CFG)["ok"] is True  # already PUBLISHED: no second publish
    assert len(published) == 1 and published[0].endswith("/17/media_publish")


def test_instagram_without_a_linked_professional_account_explains(data_dir, monkeypatch):
    monkeypatch.setenv("META_APP_ID", "a")
    monkeypatch.setenv("META_APP_SECRET", "s")
    state = parse_qs(urlparse(instagram.login_url(CFG)).query)["state"][0]
    answers = iter([{"access_token": "short"}, {"access_token": "long"}, {"data": [{"name": "Page"}]}])
    monkeypatch.setattr(instagram, "_get", lambda url, params: next(answers))
    with pytest.raises(platforms.PostError, match="Professional"):
        instagram.finish_login("code", state, CFG)


# ------------------------------------------------------------- trending sounds by web search

class FakeGemini:
    """First call: search (free text). Second: structure the list. Third: pick for the clip."""

    def __init__(self):
        from clipper import trending
        self.calls = []
        self.answers = [
            SimpleNamespace(text="Vibin by Wxoda is huge in the US...", parsed=None),
            SimpleNamespace(parsed=trending.WebChart(sounds=[
                trending.WebSound(title="Vibin", artist="Wxoda", country="us", why="dance trend"),
                trending.WebSound(title="Ozeba", artist="Rema", country="NG", why="skits")]), text=""),
            SimpleNamespace(parsed=trending.Picks(picks=[
                trending.Pick(sound="S2", reason="fits the skit", fit=8, start_s=0)]), text=""),
        ]
        self.models = self

    def generate_content(self, model, contents, config):
        self.calls.append(config)
        return self.answers[len(self.calls) - 1]


@needs_ffmpeg
def test_web_trending_search_then_pick(data_dir, fresh_db, monkeypatch):
    from clipper import trending
    _approved(fresh_db, plats=("tiktok",))
    make_clip(config.job_dir(CID) / "transformed.mp4", seconds=2)
    fake = FakeGemini()
    sound = trending.suggest_web(db.get(CID), CFG, client=fake)
    assert fake.calls[0].tools, "the first call must use Google Search"
    pick = sound["suggestions"][0]
    assert (pick["title"], pick["country"], pick["source"]) == ("Ozeba", "NG", "web")
    assert pick["search_url"].startswith("https://www.tiktok.com/search?q=Ozeba")
    chart = trending.cached_web_chart()
    assert [t["country"] for t in chart["tracks"]] == ["US", "NG"]  # normalised


def test_sound_suggestions_dont_need_upload_post_when_posting_directly(monkeypatch):
    monkeypatch.delenv("UPLOAD_POST_API_KEY", raising=False)
    job = {"kind": "clip", "platforms": ["tiktok"], "sound": None}
    assert worker.wants_sound_suggestions(job, CFG)


# ------------------------------------------------------------- x

def test_x_check_finds_the_post_by_its_video(data_dir, monkeypatch):
    platforms.save("x", {"user_id": "u1", "username": "me"})
    monkeypatch.setattr(x, "_call", lambda method, path, **kw: {"data": [
        {"id": "t9", "attachments": {"media_keys": ["7_555"]}}]})
    assert x.check({"media_id": "555", "user_id": "u1", "at": 0}, CFG)["url"] == "https://x.com/me/status/t9"


def test_x_check_waits_before_deciding_it_wasnt_posted(data_dir, monkeypatch):
    platforms.save("x", {"user_id": "u1", "username": "me"})
    monkeypatch.setattr(x, "_call", lambda method, path, **kw: {"data": []})
    assert x.check({"media_id": "555", "user_id": "u1", "at": time.time()}, CFG) is None
    assert x.check({"media_id": "555", "user_id": "u1", "at": time.time() - 999}, CFG)["ok"] is False


def test_x_missing_keys_is_a_clear_error(monkeypatch):
    for key in x.KEYS:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(platforms.PostError, match="X_API_KEY"):
        x._auth()


def test_tiktok_inbox_mode_only_asks_for_draft_permissions(data_dir, monkeypatch):
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "ck")
    q = parse_qs(urlparse(tiktok.login_url(CFG)).query)
    assert q["scope"][0] == "user.info.basic,video.upload"  # no video.publish unless Direct Post is on
    direct = config._merge(CFG, {"posting": {"tiktok_mode": "direct"}})
    assert "video.publish" in parse_qs(urlparse(tiktok.login_url(direct)).query)["scope"][0]
