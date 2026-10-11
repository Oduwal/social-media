import pytest
from fastapi.testclient import TestClient

from clipper import app as webapp
from clipper import config, music
from tests.conftest import make_clip, make_tone, needs_ffmpeg

VIDEO = "7412345678901234567"
CLIP = {"title": "The big reveal", "start_mmss": "00:01", "end_mmss": "00:21", "start_s": 1, "end_s": 21,
        "score": 9, "reason": "best", "hook": "h", "spoken_words": "", "caption": "wow",
        "hashtags": [], "music_mood": "hype"}


@pytest.fixture
def client(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "MUSIC_DIR", tmp_path / "music")
    (tmp_path / "music").mkdir()
    return TestClient(webapp.app)


def _video(db):
    db.insert_if_new(VIDEO, f"https://www.tiktok.com/@c/video/{VIDEO}", state="CLIPPED")
    return db.get(VIDEO)


def _clip(db, n=1, state="PREVIEW", files=True, **fields):
    clip_id = f"{VIDEO}-{n:02d}"
    db.insert_clip(clip_id, _video(db) if not db.get(VIDEO) else db.get(VIDEO), CLIP,
                   info={"title": f"Clip {n}"}, captions={"tiktok": "a", "instagram": "b", "x": "c"},
                   platforms=["tiktok"], **fields)
    db.update(clip_id, state=state, cut_range={"start": 1, "end": 21})
    if files:
        job_dir = config.job_dir(clip_id)
        make_clip(job_dir / "highlight.mp4", seconds=3)
        (job_dir / "transformed.mp4").write_bytes((job_dir / "highlight.mp4").read_bytes())
        (job_dir / "final.mp4").write_bytes((job_dir / "highlight.mp4").read_bytes())
    return clip_id


def test_index_renders_with_no_jobs(client):
    r = client.get("/")
    assert r.status_code == 200 and "Videos" in r.text


def test_add_link_creates_manual_video(client, fresh_db):
    r = client.post("/jobs", data={"url": f"https://www.tiktok.com/@c/video/{VIDEO}?lang=en"},
                    follow_redirects=False)
    assert r.status_code == 303
    job = fresh_db.get(VIDEO)
    assert job["state"] == "DETECTED" and job["source"] == "manual" and job["kind"] == "video"
    assert job["url"] == f"https://www.tiktok.com/@c/video/{VIDEO}"


@needs_ffmpeg
def test_library_video_and_clip_pages_render(client, fresh_db):
    a = _clip(fresh_db, 1)
    _clip(fresh_db, 2, state="READY")
    lib = client.get("/library")
    # albino scrapped: the review step offers "Use these" / Delete, not "Make albino"
    assert lib.status_code == 200 and "Clip 1" in lib.text and "Use these" in lib.text
    assert "Make albino" not in lib.text and "Delete selected" in lib.text
    assert "Clip 2" not in lib.text  # READY isn't in the "to review" tab
    assert "Clip 2" in client.get("/library?tab=ready").text
    assert "Clip 1" in client.get(f"/jobs/{VIDEO}").text
    page = client.get(f"/jobs/{a}")
    assert page.status_code == 200 and "Use this clip" in page.text and "Make albino" not in page.text
    assert client.get(f"/media/{a}/highlight.mp4").status_code == 200
    assert client.get(f"/media/{a}/../../secret").status_code == 404


def test_picking_several_clips_queues_only_previews(client, fresh_db):
    a = _clip(fresh_db, 1, files=False)
    b = _clip(fresh_db, 2, files=False)
    c = _clip(fresh_db, 3, state="READY", files=False)
    r = client.post("/clips/pick", data={"mode": "albino", "ids": [a, b, c], "back": "/library"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/library"
    assert fresh_db.get(a)["state"] == "CUT" and fresh_db.get(a)["transform_mode"] == "albino"
    assert fresh_db.get(b)["state"] == "CUT"
    assert fresh_db.get(c)["state"] == "READY"  # not a preview: untouched


def test_use_as_is_and_discard(client, fresh_db):
    a = _clip(fresh_db, 1, files=False)
    b = _clip(fresh_db, 2, files=False)
    client.post("/clips/pick", data={"mode": "original", "ids": [a]})
    client.post("/clips/pick", data={"mode": "discard", "ids": [b]})
    assert fresh_db.get(a)["state"] == "CUT" and fresh_db.get(a)["transform_mode"] == "original"
    assert fresh_db.get(b)["state"] == "REJECTED"


def test_pick_rejects_bad_input_and_offsite_redirects(client, fresh_db):
    a = _clip(fresh_db, 1, files=False)
    assert client.post("/clips/pick", data={"mode": "albino"}).status_code == 400
    assert client.post("/clips/pick", data={"mode": "nuke", "ids": [a]}).status_code == 400
    r = client.post("/clips/pick", data={"mode": "albino", "ids": [a], "back": "//evil.com"},
                    follow_redirects=False)
    assert r.headers["location"] == "/library"


@needs_ffmpeg
def test_clip_page_and_approve(client, fresh_db):
    a = _clip(fresh_db, 1, state="READY")
    make_tone(config.MUSIC_DIR / "beat.mp3", seconds=2)
    page = client.get(f"/jobs/{a}")
    assert "Approve" in page.text and "Edit video" in page.text
    assert client.get("/music/beat.mp3").status_code == 200

    client.post(f"/jobs/{a}/approve", data={"platform_tiktok": "on", "platform_x": "on", "caption_tiktok": "T",
                                            "caption_instagram": "I", "caption_x": "X", "ai_label": "on",
                                            "tiktok_privacy": "SELF_ONLY", "tiktok_consent": "on"})
    job = fresh_db.get(a)
    assert job["state"] == "APPROVED" and job["platforms"] == ["tiktok", "x"] and job["captions"]["x"] == "X"
    assert job["tiktok_opts"]["privacy"] == "SELF_ONLY" and job["tiktok_opts"]["comment"] is False


def test_tiktok_choices_are_required_for_direct_posting():
    from fastapi import HTTPException
    from clipper import app as app_module
    check = app_module._check_tiktok_opts
    ok = app_module._tiktok_opts({"tiktok_privacy": "SELF_ONLY", "tiktok_consent": "on"})
    check(ok, {})
    for form in ({"tiktok_consent": "on"},                                     # no privacy chosen
                 {"tiktok_privacy": "SELF_ONLY"},                              # no consent
                 {"tiktok_privacy": "SELF_ONLY", "tiktok_consent": "on", "tiktok_commercial": "on"},
                 {"tiktok_privacy": "SELF_ONLY", "tiktok_consent": "on", "tiktok_commercial": "on",
                  "tiktok_branded": "on"}):                                    # branded can't be private
        try:
            check(app_module._tiktok_opts(form), {})
        except HTTPException as err:
            assert err.status_code == 400
        else:
            raise AssertionError(f"accepted {form}")


def test_approve_validation(client, fresh_db):
    a = _clip(fresh_db, 1, state="READY", files=False)
    assert client.post(f"/jobs/{a}/approve", data={"caption_x": "x"}).status_code == 400
    assert client.post(f"/jobs/{a}/approve", data={"platform_x": "on", "caption_x": "x" * 281}).status_code == 400
    p = _clip(fresh_db, 2, files=False)
    assert client.post(f"/jobs/{p}/approve", data={"platform_x": "on", "caption_x": "x"}).status_code == 409
    assert fresh_db.get(a)["state"] == "READY"


def test_discarded_clip_comes_back_to_preview(client, fresh_db):
    a = _clip(fresh_db, 1)
    client.post(f"/jobs/{a}/reject")
    assert fresh_db.get(a)["state"] == "REJECTED"
    client.post(f"/jobs/{a}/process")
    assert fresh_db.get(a)["state"] == "PREVIEW"


def test_music_list_uses_patched_dir(client):
    assert music.list_tracks() == []


@needs_ffmpeg
def test_editor_page_and_save_queues_a_render(client, fresh_db):
    a = _clip(fresh_db, 1, state="READY")
    make_tone(config.MUSIC_DIR / "beat.mp3", seconds=2)
    page = client.get(f"/jobs/{a}/edit/simple")
    assert page.status_code == 200 and "editor-data" in page.text and "Save &amp; render" in page.text

    edit = {"trim": {"start": 0.5, "end": 2.5}, "filter": "warm", "zoom": "slow_in",
            "music": {"track": "beat.mp3", "volume": 0.4},
            "texts": [{"text": "hello", "start": 0, "end": 1}]}
    r = client.post(f"/api/clips/{a}/edit", json=edit)
    assert r.status_code == 200 and r.json()["redirect"] == f"/jobs/{a}"
    job = fresh_db.get(a)
    assert job["state"] == "TRANSFORMED" and job["edit"]["filter"] == "warm"
    assert job["music_track"] == "beat.mp3" and job["info"]["render_from"] == "editor"


@needs_ffmpeg
def test_editor_rejects_bad_edits_and_wrong_states(client, fresh_db):
    a = _clip(fresh_db, 1, state="READY")
    assert client.post(f"/api/clips/{a}/edit", json={"music": {"track": "nope.mp3"}}).status_code == 400
    assert client.post(f"/api/clips/{a}/edit", json={"trim": {"start": 1, "end": 1.2}}).status_code == 400
    b = _clip(fresh_db, 2, state="POSTED")
    assert client.post(f"/api/clips/{b}/edit", json={}).status_code == 409
    assert fresh_db.get(a)["state"] == "READY"


@needs_ffmpeg
def test_shotcut_buttons_without_shotcut(client, fresh_db, monkeypatch, tmp_path):
    from clipper import shotcut
    monkeypatch.setattr(shotcut, "find", lambda cfg=None: None)
    a = _clip(fresh_db, 1, state="READY")
    assert "winget install" in client.get(f"/jobs/{a}").text
    r = client.post(f"/jobs/{a}/shotcut/open")
    assert r.status_code == 409 and "winget" in r.text
    assert (config.job_dir(a) / "shotcut" / "project.mlt").exists()  # project is ready for when it's installed
    assert client.post(f"/jobs/{a}/shotcut/use").status_code == 409
    assert fresh_db.get(a)["state"] == "READY"


@needs_ffmpeg
def test_use_shotcut_edit_queues_melt_render(client, fresh_db, monkeypatch):
    from clipper import shotcut
    monkeypatch.setattr(shotcut, "find", lambda cfg=None: config.ROOT)
    monkeypatch.setattr(shotcut, "open_project", lambda project, cfg=None: None)
    a = _clip(fresh_db, 1, state="READY")
    client.post(f"/jobs/{a}/shotcut/open")
    client.post(f"/jobs/{a}/shotcut/use")
    job = fresh_db.get(a)
    assert job["state"] == "TRANSFORMED" and job["info"]["render_from"] == "shotcut"
