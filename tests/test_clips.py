"""Highlights become a library of clips."""

from clipper import config, highlight, worker
from tests.conftest import make_clip, needs_ffmpeg

CFG = config._merge(config.DEFAULTS, {"creator": {"handle": "c", "hashtags": ["#c"]}})


def _clip(start, end, score, **extra):
    return {"title": f"t{start}", "start_s": start, "end_s": end, "score": score, "caption": f"cap {start}",
            "hashtags": ["#x"], "music_mood": "hype", **extra}


def test_clean_clips_orders_by_time_and_drops_overlaps_and_junk():
    clips = [_clip(100, 130, 6), _clip(10, 40, 9), _clip(30, 60, 7), _clip(500, 530, 8), _clip(70, 71, 10)]
    kept = highlight.clean_clips(clips, duration=200)
    assert [(c["start_s"], c["end_s"]) for c in kept] == [(10, 40), (100, 130)]


def test_count_hint_scales_with_length():
    assert "1-2" in highlight.count_hint(50)
    assert "7-23" in highlight.count_hint(23.5 * 60)


@needs_ffmpeg
def test_clips_step_creates_a_clip_per_highlight_and_cut_makes_previews(fresh_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path)
    vid = "7412345678901234567"
    make_clip(config.job_dir(vid) / "source.mp4", seconds=40)
    hl = {"clips": [_clip(1, 16, 9), _clip(20, 36, 7)]}
    monkeypatch.setattr(highlight, "pick", lambda *a, **k: hl)

    fresh_db.insert_if_new(vid, "https://t/v", state="HIGHLIGHTED")
    worker.advance(fresh_db.get(vid), CFG)
    assert fresh_db.get(vid)["state"] == "CLIPPED"
    clips = fresh_db.list_clips(parent_id=vid)
    assert [c["video_id"] for c in clips] == [f"{vid}-01", f"{vid}-02"]
    first = clips[0]
    assert first["state"] == "NEW" and first["parent_id"] == vid and first["info"]["title"] == "t1"
    assert first["captions"]["tiktok"].startswith("cap 1") and "#c" in first["captions"]["tiktok"]

    for clip in clips:
        worker.advance(clip, CFG)
    first = fresh_db.get(f"{vid}-01")
    assert first["state"] == "PREVIEW"
    assert (config.job_dir(first["video_id"]) / "highlight.mp4").exists()
    assert (config.job_dir(first["video_id"]) / "thumb.jpg").exists()
    assert fresh_db.next_actionable() is None, "previews must wait for the user"


def test_running_the_clips_step_twice_does_not_duplicate(fresh_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(highlight, "pick", lambda *a, **k: {"clips": [_clip(1, 16, 9)]})
    fresh_db.insert_if_new("5", "u", state="HIGHLIGHTED")
    worker.make_clips(fresh_db.get("5"), CFG)
    worker.make_clips(fresh_db.get("5"), CFG)
    assert len(fresh_db.list_clips(parent_id="5")) == 1


def test_use_as_is_skips_the_transform(fresh_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path)
    seen = {}

    def fake_run(highlight_path, job_dir, cfg, target="", reference_image=None):
        seen["enabled"] = cfg["transform"]["enabled"]
        return {"enabled": cfg["transform"]["enabled"], "segments": 0, "fallback_segments": [], "refused": False}

    monkeypatch.setattr(worker.transform_adapter, "run", fake_run)
    fresh_db.insert_if_new("6", "u", state="CUT")
    fresh_db.update("6", kind="clip", transform_mode="original")
    worker.advance(fresh_db.get("6"), CFG)
    assert seen["enabled"] is False
    assert fresh_db.get("6")["ai_label"] is False
