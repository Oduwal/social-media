"""Regression tests for the second code review: locks, recoverable renders, conditional writes."""

import argparse
import os

import pytest
from fastapi.testclient import TestClient

from clipper import __main__ as cli
from clipper import app as webapp
from clipper import cli_lock, config, db, worker

VID = "7412345678901234567"


@pytest.fixture
def client(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "MUSIC_DIR", tmp_path / "music")
    (tmp_path / "music").mkdir()
    return TestClient(webapp.app)


def _clip(state, n=1, **fields):
    if not db.get(VID):
        db.insert_if_new(VID, "u", state="CLIPPED")
    cid = f"{VID}-{n:02d}"
    db.insert_clip(cid, db.get(VID), {"start_s": 0, "end_s": 20}, **fields)
    db.update(cid, state=state)
    return cid


def test_lock_follows_a_live_process(fresh_db):
    assert not cli_lock.running(VID)
    cli_lock.acquire(VID)
    assert cli_lock.running(VID)  # this test process is alive
    db.kv_set(f"cli_running:{VID}", {"pid": 2_000_000_000, "since": 0})  # a pid that can't exist
    assert not cli_lock.running(VID)
    cli_lock.release(VID)
    assert db.kv_get(f"cli_running:{VID}") is None


def test_continue_is_refused_while_the_terminal_works(client, fresh_db):
    db.insert_if_new(VID, "u", state="DOWNLOADED")
    db.update(VID, hold=True)
    cli_lock.acquire(VID)
    page = client.get(f"/jobs/{VID}")
    assert "terminal is working" in page.text and "Continue" not in page.text
    assert client.post(f"/jobs/{VID}/continue").status_code == 409
    assert db.get(VID)["hold"] is True
    cli_lock.release(VID)
    client.post(f"/jobs/{VID}/continue")
    assert db.get(VID)["hold"] is False


def test_cli_does_not_cut_clips_the_dashboard_owns(fresh_db, monkeypatch):
    db.insert_if_new(VID, "u", state="CLIPPED")
    owned = _clip("NEW", 1)                       # hold=0: the dashboard worker's
    cut = []
    monkeypatch.setattr(worker, "advance", lambda job, cfg=None: cut.append(job["video_id"]))
    cli.run(argparse.Namespace(url=f"https://www.tiktok.com/@c/video/{VID}", until="clips", force=False))
    assert owned not in cut
    assert not cli_lock.running(VID)


def test_failed_render_can_be_fixed_in_the_editor(client, fresh_db, tmp_path):
    from tests.conftest import make_clip
    cid = _clip("FAILED", info={"render_from": "shotcut"})
    db.update(cid, failed_step="mix")
    make_clip(config.job_dir(cid) / "transformed.mp4", seconds=3)
    page = client.get(f"/jobs/{cid}")
    assert "Edit video" in page.text and "render failed" in page.text
    r = client.post(f"/api/clips/{cid}/edit", json={"trim": {"start": 0, "end": 2}})
    assert r.status_code == 200
    job = db.get(cid)
    assert job["state"] == "TRANSFORMED" and job["info"]["render_from"] == "editor" and job["failed_step"] is None


def test_other_failures_are_not_editable(client, fresh_db):
    cid = _clip("FAILED")
    db.update(cid, failed_step="transform")
    assert client.post(f"/api/clips/{cid}/edit", json={}).status_code == 409


def test_reject_cannot_overwrite_a_post_in_progress(fresh_db):
    cid = _clip("POSTING")
    assert db.reject(cid) is False and db.get(cid)["state"] == "POSTING"
    other = _clip("APPROVED", 2)
    assert db.reject(other) is True and db.get(other)["state"] == "REJECTED"


def test_approve_is_conditional(client, fresh_db):
    cid = _clip("READY", captions={"tiktok": "a", "instagram": "b", "x": "c"})
    db.update(cid, state="REJECTED")  # e.g. discarded in another tab after this page loaded
    r = client.post(f"/jobs/{cid}/approve", data={"platform_x": "on", "caption_x": "x"})
    assert r.status_code == 409 and db.get(cid)["state"] == "REJECTED"


def test_retry_button_shown_when_every_segment_failed(client, fresh_db):
    from tests.conftest import make_clip
    cid = _clip("READY", transform_info={"enabled": True, "segments": 3, "fallback_segments": [1, 2, 3],
                                         "refused": True, "original_audio": True})
    make_clip(config.job_dir(cid) / "final.mp4", seconds=1)
    assert "Retry the albino edit" in client.get(f"/jobs/{cid}").text


def test_editor_drops_a_deleted_music_track(client, fresh_db):
    from tests.conftest import make_clip
    cid = _clip("READY", music_track="gone.mp3")
    db.update(cid, edit={"trim": {"start": 0, "end": 3}, "filter": "none", "zoom": "none", "fade_in": 0,
                         "fade_out": 0, "voice_volume": 1, "texts": [],
                         "music": {"track": "gone.mp3", "volume": 0.2, "offset": 0, "ducking": True,
                                   "fade_in": 0.5, "fade_out": 1}})
    make_clip(config.job_dir(cid) / "transformed.mp4", seconds=3)
    page = client.get(f"/jobs/{cid}/edit/simple")
    assert page.status_code == 200 and '"track": null' in page.text


def test_pid_check_does_not_kill_anything():
    """os.kill on Windows terminates the target, so the liveness check must not use it."""
    assert cli_lock._alive(os.getpid()) is True
