import argparse
import time

import pytest

from clipper import __main__ as cli
from clipper import config, db, monitor, worker

CFG = config._merge(config.DEFAULTS, {"creator": {"handle": "creatorb"}})
URL = "https://www.tiktok.com/@c/video/7412345678901234567"
VID = "7412345678901234567"


def vid_at(ts: int, n: int = 1) -> str:
    return str((ts << 32) + n)


def test_video_id_encodes_post_time():
    assert monitor.video_time(vid_at(1_700_000_000, 12345)) == 1_700_000_000


def test_backlog_is_skipped_even_when_it_shows_up_later(fresh_db, monkeypatch):
    now = int(time.time())
    first = [{"id": vid_at(now - 86400 * i), "url": ""} for i in range(1, 6)]
    monkeypatch.setattr(monitor, "list_latest", lambda cfg, handle=None: first)
    assert monitor.poll_once(CFG) == 0
    assert all(fresh_db.get(v["id"])["state"] == "SKIPPED" for v in first)

    # Next poll lists MORE old posts (e.g. yt-dlp gives 10 where Apify gave 5) plus one new post.
    older = [{"id": vid_at(now - 86400 * i), "url": ""} for i in range(6, 11)]
    fresh = [{"id": vid_at(now + 60), "url": ""}]
    monkeypatch.setattr(monitor, "list_latest", lambda cfg, handle=None: fresh + first + older)
    assert monitor.poll_once(CFG) == 1
    assert fresh_db.get(fresh[0]["id"])["state"] == "DETECTED"
    assert all(fresh_db.get(v["id"])["state"] == "SKIPPED" for v in older)


def test_sec_uid_is_per_handle(fresh_db):
    monitor.remember_sec_uid("SEC_A", "creatora", "creatora")
    assert fresh_db.kv_get(monitor.sec_uid_key("creatora")) == "SEC_A"
    assert fresh_db.kv_get(monitor.sec_uid_key("creatorb")) is None
    monitor.remember_sec_uid("SEC_X", "someoneelse", "creatorb")  # uploader mismatch: ignored
    assert fresh_db.kv_get(monitor.sec_uid_key("creatorb")) is None


def test_monitor_survives_a_config_error(fresh_db, monkeypatch):
    mon = monitor.Monitor()

    def broken():
        mon.stop_event.set()
        raise ValueError("yaml typo")

    monkeypatch.setattr(config, "load", broken)
    monkeypatch.setattr(mon, "_wait", lambda seconds: None)
    mon.run()  # must return normally, not raise
    assert "yaml typo" in fresh_db.kv_get("monitor_status")["message"]


def test_worker_survives_a_config_error(fresh_db, monkeypatch):
    fresh_db.insert_if_new("1", "u")
    w = worker.Worker()

    def broken():
        w.stop_event.set()
        raise ValueError("yaml typo")

    monkeypatch.setattr(config, "load", broken)
    w.run()  # must return normally, not raise


def _args(until):
    return argparse.Namespace(url=URL, until=until, force=False)


def _fake_pipeline(monkeypatch, held_during=None):
    """advance() stand-in: videos step through their states, the clips step makes 2 clips,
    clips go NEW -> PREVIEW."""
    order = ["DETECTED", "DOWNLOADED", "HIGHLIGHTED", "CLIPPED"]

    def fake_advance(job, cfg=None):
        if held_during is not None:
            held_during.append(db.get(job["video_id"])["hold"])
        if job["kind"] == "clip":
            db.update(job["video_id"], state="PREVIEW")
            return
        nxt = order[order.index(job["state"]) + 1]
        if nxt == "CLIPPED":
            for n in (1, 2):
                db.insert_clip(f"{job['video_id']}-{n:02d}", job, {"start_s": 0, "end_s": 20}, hold=job["hold"])
        db.update(job["video_id"], state=nxt)

    monkeypatch.setattr(worker, "advance", fake_advance)


def test_cli_cuts_every_clip_while_holding_then_releases(fresh_db, monkeypatch):
    held_during = []
    _fake_pipeline(monkeypatch, held_during)
    cli.run(_args("clips"))
    assert held_during and all(held_during), "worked on rows the dashboard's worker could also grab"
    assert db.get(VID)["state"] == "CLIPPED" and db.get(VID)["hold"] is False
    clips = db.list_clips(parent_id=VID)
    assert [c["state"] for c in clips] == ["PREVIEW", "PREVIEW"]
    assert not any(c["hold"] for c in clips)


def test_cli_stopped_early_stays_held(fresh_db, monkeypatch):
    _fake_pipeline(monkeypatch)
    cli.run(_args("download"))
    job = db.get(VID)
    assert job["state"] == "DOWNLOADED" and job["hold"] is True
    assert db.next_actionable() is None


def test_cli_refuses_a_job_queued_for_the_server(fresh_db, monkeypatch):
    monkeypatch.setattr(worker, "advance", lambda job, cfg=None: pytest.fail("should not run"))
    db.insert_if_new(VID, "u", state="DOWNLOADED")
    with pytest.raises(SystemExit):
        cli.run(_args("clips"))
