"""The @username watermark and bulk clip deletion."""

import pytest
from fastapi.testclient import TestClient

from clipper import app as webapp
from clipper import config, db, media, worker
from tests.conftest import make_clip, needs_ffmpeg


@pytest.fixture
def client(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    config.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    return TestClient(webapp.app)


# ------------------------------------------------------------------ watermark

@needs_ffmpeg
def test_watermark_burns_text_and_keeps_size_and_length(tmp_path):
    src = make_clip(tmp_path / "in.mp4", seconds=3, size="1080x1920")
    out = media.watermark(src, tmp_path / "out.mp4", "@myuser", 0.35, "bottom-right", 4)
    assert out.exists() and media.video_size(out) == (1080, 1920)
    assert abs(media.duration(out) - 3) < 0.2 and media.has_audio(out)


@needs_ffmpeg
def test_watermark_with_no_text_is_a_noop(tmp_path):
    src = make_clip(tmp_path / "in.mp4", seconds=1)
    assert media.watermark(src, tmp_path / "out.mp4", "   ", 0.3) == src   # returns the source, writes nothing
    assert not (tmp_path / "out.mp4").exists()


def test_apply_watermark_respects_the_setting(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(worker.media, "watermark",
                        lambda src, out, *a, **k: (calls.append((a, k)), out.write_bytes(b"x"))[1])
    final = tmp_path / "final.mp4"
    final.write_bytes(b"orig")
    # off: untouched
    worker.apply_watermark(final, {"watermark": {"enabled": False, "text": "@x"}})
    assert final.read_bytes() == b"orig" and not calls
    # on with no text: untouched
    worker.apply_watermark(final, {"watermark": {"enabled": True, "text": "  "}})
    assert not calls
    # on with text: applied in place
    worker.apply_watermark(final, {"watermark": {"enabled": True, "text": "@star", "opacity": 0.4}})
    assert final.read_bytes() == b"x" and calls


def test_watermark_config_override_from_ui(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UI_PATH", tmp_path / "ui.json")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "none.yaml")
    assert config.load()["watermark"]["enabled"] is False
    config.ui_set(watermark={"enabled": True, "text": "@me", "position": "top-left"})
    wm = config.load()["watermark"]
    assert wm["enabled"] and wm["text"] == "@me" and wm["position"] == "top-left"


# ------------------------------------------------------------------ delete

def test_delete_removes_only_clips(fresh_db):
    db.insert_if_new("700000000000000001", "u", state="DETECTED")          # a video
    p = {"video_id": "700000000000000001", "url": "u", "source": "monitor", "handle": "x"}
    db.insert_clip("700000000000000001-01", p, {"start_s": 0, "end_s": 5}, state="READY")
    assert db.delete("700000000000000001-01")["video_id"] == "700000000000000001-01"
    assert not db.get("700000000000000001-01")                             # gone
    assert not db.delete("700000000000000001")                             # videos aren't deleted this way
    assert db.get("700000000000000001")["state"] == "DETECTED"


def test_delete_route_removes_rows_and_files(client):
    p = {"video_id": "700000000000000002", "url": "u", "source": "monitor", "handle": "x"}
    for n in (1, 2):
        cid = f"700000000000000002-0{n}"
        db.insert_clip(cid, p, {"start_s": 0, "end_s": 5}, state="READY")
        (config.JOBS_DIR / cid).mkdir(parents=True)
        (config.JOBS_DIR / cid / "final.mp4").write_bytes(b"v")
    r = client.post("/clips/delete", data={"ids": ["700000000000000002-01", "700000000000000002-02"]},
                    follow_redirects=False)
    assert r.status_code in (302, 303)
    assert not db.get("700000000000000002-01") and not db.get("700000000000000002-02")
    assert not (config.JOBS_DIR / "700000000000000002-01").exists()
