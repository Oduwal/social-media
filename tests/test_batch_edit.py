"""Batch edit: one set of answers applied to several clips."""

import pytest
from fastapi.testclient import TestClient

from clipper import app as webapp
from clipper import batch_edit, captions, config, db, render, worker
from tests.conftest import make_clip, needs_ffmpeg

CFG = {"music": {"ducking": True}, "watermark": {"enabled": False, "text": "", "opacity": 0.35,
                                                   "position": "bottom-right", "size": 4}}


@pytest.fixture
def client(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    config.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setitem(webapp.hooks, "wake_worker", lambda: None)
    return TestClient(webapp.app)


def _clip(clip_id, state):
    parent = {"video_id": clip_id.split("-")[0], "url": "u", "source": "manual"}
    db.insert_clip(clip_id, parent, {"title": "t"}, info={"title": "Why I quit"})
    db.update(clip_id, state=state)


def test_clean_spec_falls_back_on_junk():
    spec = batch_edit.clean_spec({"captions": "evil", "filter": "nope", "quality": "x",
                                  "title_source": "text", "title_text": "  ", "watermark_opacity": "9"})
    assert spec["captions"] == "none" and spec["filter"] == "none" and spec["quality"] == "standard"
    assert spec["title_source"] == "none"  # "same title" with no text is no title
    assert spec["watermark_opacity"] == 1.0


def test_pov_prefix_and_title_sources():
    spec = batch_edit.clean_spec({"title_source": "text", "title_text": "he never knew", "title_pov": "on"})
    assert batch_edit.title_for(spec, {}) == "POV: he never knew"
    spec = batch_edit.clean_spec({"title_source": "text", "title_text": "POV: already", "title_pov": "on"})
    assert batch_edit.title_for(spec, {}) == "POV: already"
    spec = batch_edit.clean_spec({"title_source": "ai"})
    assert batch_edit.title_for(spec, {"info": {"title": "Big moment"}}) == "Big moment"


def test_watermark_cfg_overrides_per_batch():
    spec = batch_edit.clean_spec({"watermark_text": "@me", "watermark_position": "top-left"})
    wm = batch_edit.watermark_cfg(spec, CFG)["watermark"]
    assert wm["enabled"] and wm["text"] == "@me" and wm["position"] == "top-left"
    assert not batch_edit.watermark_cfg(batch_edit.clean_spec({}), CFG)["watermark"]["enabled"]


@needs_ffmpeg
def test_build_edit_and_render_at_two_qualities(tmp_path, monkeypatch):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    make_clip(job_dir / "transformed.mp4", seconds=5, size="1080x1920")
    monkeypatch.setattr(captions, "captions_for", lambda d, a, b, ref="": [
        {"text": "hello there", "start": 0.0, "end": 1.5, "words": [[0.0, 0.7], [0.7, 1.5]], **captions.STYLE}])
    spec = batch_edit.clean_spec({"captions": "words", "title_source": "text", "title_text": "wait for it",
                                  "title_pov": "on", "filter": "warm", "zoom": "slow_in", "fade": "on",
                                  "quality": "high", "caption_position": "middle"})
    edit = batch_edit.build_edit(spec, {"info": {}}, job_dir, CFG)
    assert edit["filter"] == "warm" and edit["quality"] == "high" and edit["fade_in"] > 0
    caption, title = edit["texts"]
    assert caption["animation"] == "words" and caption["position"] == "middle"
    assert title["text"] == "POV: wait for it" and title["end"] == 4.0

    sizes = {}
    for quality in ("draft", "max"):
        out = render.render({**edit, "quality": quality}, job_dir, out_name=f"{quality}.mp4")
        sizes[quality] = out.stat().st_size
    assert sizes["max"] > sizes["draft"]


def test_page_lists_only_editable_clips(client):
    _clip("1-01", "PREVIEW")
    _clip("2-01", "POSTED")
    page = client.get("/clips/batch-edit", params={"ids": ["1-01", "2-01"]})
    assert page.status_code == 200 and 'value="1-01"' in page.text and 'value="2-01"' not in page.text
    assert client.get("/clips/batch-edit", params={"ids": ["2-01"]}).status_code == 400


def test_apply_queues_preview_and_ready_clips(client):
    _clip("1-01", "PREVIEW")
    _clip("1-02", "READY")
    _clip("1-03", "POSTED")
    res = client.post("/clips/batch-edit", data={"ids": ["1-01", "1-02", "1-03"], "captions": "highlight",
                                                 "quality": "high", "back": "/library?tab=ready"},
                      follow_redirects=False)
    assert res.status_code == 303
    preview, ready, posted = db.get("1-01"), db.get("1-02"), db.get("1-03")
    assert preview["state"] == "CUT" and preview["transform_mode"] == "original"
    assert ready["state"] == "TRANSFORMED"
    for job in (preview, ready):
        assert job["info"]["render_from"] == "batch" and job["info"]["batch_edit"]["captions"] == "highlight"
    assert posted["state"] == "POSTED" and "batch_edit" not in (posted["info"] or {})


@needs_ffmpeg
def test_worker_renders_a_batch_clip(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    _clip("3-01", "TRANSFORMED")
    job_dir = config.job_dir("3-01")
    make_clip(job_dir / "transformed.mp4", seconds=3, size="1080x1920")
    spec = batch_edit.clean_spec({"title_source": "ai", "watermark_text": "@me", "quality": "draft"})
    db.update("3-01", info={"title": "Why I quit", "render_from": "batch", "batch_edit": spec})
    worker.render_edit(db.get("3-01"), config.load())
    assert (job_dir / "final.mp4").exists()
    saved = db.get("3-01")["edit"]
    assert saved["quality"] == "draft" and saved["texts"][0]["text"] == "Why I quit"


def test_presets_save_load_delete_and_last_used(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UI_PATH", tmp_path / "ui.json")
    _clip("1-01", "PREVIEW")
    res = client.post("/clips/batch-edit/preset", data={"preset_name": "My usual look", "captions": "highlight",
                                                        "quality": "high", "title_pov": "on"})
    assert res.json()["presets"]["My usual look"]["quality"] == "high"
    assert client.post("/clips/batch-edit/preset", data={"preset_name": "  "}).status_code == 400
    client.post("/clips/batch-edit", data={"ids": ["1-01"], "captions": "box", "filter": "warm"})
    _clip("1-02", "PREVIEW")
    page = client.get("/clips/batch-edit", params={"ids": ["1-02"]})
    assert "My usual look" in page.text and '"filter": "warm"' in page.text   # preset listed, last answers prefilled
    assert "be-pop" in page.text and "/media/1-02/thumb.jpg" in page.text      # the previews
    client.post("/clips/batch-edit/preset/delete", data={"preset_name": "My usual look"})
    assert batch_edit.presets() == {}


def test_batch_post_checks_everything_before_posting(client, monkeypatch):
    monkeypatch.setattr(webapp.platforms, "module", lambda p: type("M", (), {"status": staticmethod(
        lambda cfg: {"connected": p == "x", "account": "me", "mode": "inbox"}), "drafts_left": staticmethod(lambda: 5)}))
    for cid, text in (("5-01", "short"), ("5-02", "x" * 400)):
        _clip(cid, "READY")
        db.update(cid, captions={"tiktok": text, "instagram": "i", "x": text}, platforms=["x"])
    page = client.get("/clips/batch-post", params={"ids": ["5-01", "5-02"]})
    assert page.status_code == 200 and "Why I quit" in page.text
    data = {"ids": ["5-01", "5-02"], "platform_x": "on", "back": "/library?tab=ready"}
    assert client.post("/clips/batch-post", data=data).status_code == 400                       # not confirmed
    assert client.post("/clips/batch-post", data={**data, "confirm": "on"}).status_code == 400  # X caption too long
    assert db.get("5-01")["state"] == "READY" and db.get("5-02")["state"] == "READY"
    ok = client.post("/clips/batch-post", data={**data, "ids": ["5-01"], "confirm": "on"}, follow_redirects=False)
    assert ok.status_code == 303 and db.get("5-01")["state"] == "APPROVED" and db.get("5-01")["platforms"] == ["x"]
    assert db.get("5-02")["state"] == "READY"


def test_media_is_served_with_ranges_without_keeping_the_file_open(client, tmp_path, monkeypatch):
    _clip("6-01", "READY")
    folder = config.job_dir("6-01")
    (folder / "final.mp4").write_bytes(bytes(range(256)) * 10)
    full = client.get("/media/6-01/final.mp4")
    assert full.status_code == 200 and len(full.content) == 2560 and full.headers["accept-ranges"] == "bytes"
    part = client.get("/media/6-01/final.mp4", headers={"Range": "bytes=10-19"})
    assert part.status_code == 206 and part.content == bytes(range(10, 20))
    assert part.headers["content-range"] == "bytes 10-19/2560"
    assert client.get("/media/6-01/final.mp4", headers={"Range": "bytes=-5"}).content == bytes(range(251, 256))
    assert client.get("/media/6-01/final.mp4", headers={"Range": "bytes=9999-"}).status_code == 416
    (folder / "next.mp4").write_bytes(b"new")
    (folder / "next.mp4").replace(folder / "final.mp4")          # nothing holds the file open any more
    assert client.get("/media/6-01/final.mp4").content == b"new"


def test_replace_retries_while_the_target_is_locked(tmp_path, monkeypatch):
    from clipper import media
    src, dst = tmp_path / "a.tmp", tmp_path / "a.mp4"
    src.write_bytes(b"x")
    real, calls = type(src).replace, []

    def flaky(self, target):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("locked")
        return real(self, target)

    monkeypatch.setattr(type(src), "replace", flaky)
    monkeypatch.setattr(media.time, "sleep", lambda s: None)
    assert media.replace(src, dst) == dst and dst.read_bytes() == b"x" and len(calls) == 3
    src.write_bytes(b"y")
    monkeypatch.setattr(type(src), "replace", lambda self, t: (_ for _ in ()).throw(PermissionError("locked")))
    with pytest.raises(media.MediaError, match="open in another program"):
        media.replace(src, dst, wait=0)


def test_batch_post_refuses_a_platform_that_is_not_connected(client, monkeypatch):
    monkeypatch.setattr(webapp.platforms, "module", lambda p: type("M", (), {"status": staticmethod(
        lambda cfg: {"connected": p == "x", "account": "", "mode": "inbox"}), "drafts_left": staticmethod(lambda: 5)}))
    _clip("7-01", "READY")
    db.update("7-01", captions={"x": "hi"}, platforms=["tiktok", "instagram", "x"])
    page = client.get("/clips/batch-post", params={"ids": ["7-01"]})
    assert 'name="platform_x" checked' in page.text and 'name="platform_instagram" checked' not in page.text
    res = client.post("/clips/batch-post", data={"ids": ["7-01"], "platform_instagram": "on", "confirm": "on"})
    assert res.status_code == 400 and "isn't connected" in res.text and db.get("7-01")["state"] == "READY"


def test_retry_selected_keeps_the_edit_and_captions(client):
    spec = batch_edit.clean_spec({"captions": "words", "quality": "high"})
    for cid, step in (("8-01", "mix"), ("8-02", "post")):
        _clip(cid, "FAILED")
        db.update(cid, failed_step=step, error="boom", edit={"quality": "high"}, captions={"x": "hi"},
                  platforms=["x"], info={"render_from": "batch", "batch_edit": spec})
    _clip("8-03", "READY")
    res = client.post("/clips/retry", data={"ids": ["8-01", "8-02", "8-03"], "back": "/library?tab=failed"},
                      follow_redirects=False)
    assert res.status_code == 303
    mix, post, ready = db.get("8-01"), db.get("8-02"), db.get("8-03")
    assert mix["state"] == db.RETRY_STATE["mix"] and post["state"] == db.RETRY_STATE["post"]
    assert mix["failed_step"] is None and mix["error"] is None
    assert mix["edit"] == {"quality": "high"} and mix["info"]["batch_edit"] == spec     # edit kept
    assert post["captions"] == {"x": "hi"} and post["platforms"] == ["x"]                # post settings kept
    assert ready["state"] == "READY"                                                     # only failed clips move
    assert client.post("/clips/retry", data={}).status_code == 400


def test_batch_post_respects_tiktoks_draft_limit(client, monkeypatch):
    monkeypatch.setattr(webapp.platforms, "module", lambda p: type("M", (), {
        "status": staticmethod(lambda cfg: {"connected": True, "account": "me", "mode": "inbox"}),
        "drafts_left": staticmethod(lambda: 2)}))
    for i in (1, 2, 3):
        _clip(f"9-0{i}", "READY")
        db.update(f"9-0{i}", captions={"tiktok": "t"}, platforms=["tiktok"])
    ids = ["9-01", "9-02", "9-03"]
    assert "room for <b>2</b>" in client.get("/clips/batch-post", params={"ids": ids}).text
    res = client.post("/clips/batch-post", data={"ids": ids, "platform_tiktok": "on", "confirm": "on"})
    assert res.status_code == 400 and "room for 2" in res.text
    assert all(db.get(i)["state"] == "READY" for i in ids)
    cleared = client.post("/clips/batch-post", data={"ids": ids, "platform_tiktok": "on", "confirm": "on",
                                                     "tiktok_cleared": "on"}, follow_redirects=False)
    assert cleared.status_code == 303 and all(db.get(i)["state"] == "APPROVED" for i in ids)
    for i in ids:
        db.update(i, state="READY")
    ok = client.post("/clips/batch-post", data={"ids": ids[:2], "platform_tiktok": "on", "confirm": "on"},
                     follow_redirects=False)
    assert ok.status_code == 303 and db.get("9-01")["state"] == "APPROVED"


def test_tiktok_draft_count_and_friendly_errors(fresh_db):
    import time
    from clipper.platforms import tiktok
    _clip("4-01", "FAILED")
    db.update("4-01", post_result={"platforms": {"tiktok": {"ok": True, "drafted": True}}})
    assert db.count_tiktok_drafts(time.time() - 60) == 1 and db.count_tiktok_drafts(time.time() + 60) == 0
    assert "5 unposted drafts" in tiktok.FRIENDLY["spam_risk_too_many_pending_share"]


def test_rewrite_captions_replaces_the_caption_and_keeps_credit_and_hashtags(client, monkeypatch):
    from clipper import highlight
    for cid, state in (("10-01", "READY"), ("10-02", "PREVIEW"), ("10-03", "POSTED")):
        _clip(cid, state)
        db.update(cid, clip={"caption": "He talks about money", "spoken_words": "abeg", "hashtags": ["#naija"]},
                  captions={"tiktok": "old"})
    seen = {}

    def fake(items, cfg):
        seen["ids"] = [i["id"] for i in items]
        return {i["id"]: "omo this one don pass me" for i in items}

    monkeypatch.setattr(highlight, "rewrite_captions", fake)
    res = client.post("/clips/rewrite-captions", data={"ids": ["10-01", "10-02", "10-03"], "back": "/library"},
                      follow_redirects=False)
    assert res.status_code == 303 and seen["ids"] == ["10-01", "10-02"]           # posted clips are left alone
    job = db.get("10-01")
    assert job["clip"]["caption"] == "omo this one don pass me" and job["clip"]["hashtags"] == ["#naija"]
    assert job["captions"]["tiktok"].startswith("omo this one don pass me") and "#naija" in job["captions"]["tiktok"]
    assert db.get("10-03")["captions"] == {"tiktok": "old"}

    def broken(items, cfg):
        raise RuntimeError("503 high demand")

    monkeypatch.setattr(highlight, "rewrite_captions", broken)
    err = client.post("/clips/rewrite-captions", data={"ids": ["10-01"]})
    assert err.status_code == 502 and "Nothing was changed" in err.text
    assert client.post("/clips/rewrite-captions", data={"ids": ["10-03"]}).status_code == 400
    assert db.get("10-01")["clip"]["caption"] == "omo this one don pass me"


def test_clean_caption_strips_hashtags_and_whitespace():
    from clipper import highlight
    assert highlight.clean_caption("gehgeh   that one go far o #fyp\n#ng") == "gehgeh that one go far o"
