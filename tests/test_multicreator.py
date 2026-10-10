"""Multi-creator watching, the dashboard creator list, the Vertex on/off toggle, and
auto-finish (clips straight to READY). Config overrides live in data/ui.json."""

import pytest

from clipper import config, db, monitor, worker


@pytest.fixture
def ui(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UI_PATH", tmp_path / "ui.json")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    return tmp_path


# ------------------------------------------------------------------ the creator list

def test_single_config_creator_seeds_the_list(ui, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", ui / "none.yaml")  # no config.yaml
    cfg = config.load()
    # DEFAULTS has no creator, so the list is empty until one is added
    assert cfg["creators"] == []
    config.add_creator("@Official_GehGeh_", hashtags=["#gehgeh"])
    cfg = config.load()
    assert len(cfg["creators"]) == 1
    c = cfg["creators"][0]
    assert c["handle"] == "Official_GehGeh_" and c["credit"] == "🎥 @Official_GehGeh_" and c["enabled"]
    assert cfg["creator"]["handle"] == "Official_GehGeh_"  # first creator mirrored for old code


def test_add_remove_and_pause(ui, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", ui / "none.yaml")
    config.add_creator("one", credit="by one", hashtags=["#a"])
    config.add_creator("two")
    assert [c["handle"] for c in config.load()["creators"]] == ["one", "two"]
    config.add_creator("ONE", hashtags=["#dup"])  # same handle (any case) replaces, no duplicate
    handles = [c["handle"] for c in config.load()["creators"]]
    assert handles.count("ONE") + handles.count("one") == 1
    config.set_creator_enabled("two", False)
    assert config.creator_for(config.load(), "two")["enabled"] is False
    config.remove_creator("one")
    assert [c["handle"] for c in config.load()["creators"]] == ["two"]


def test_creator_for_lookup_and_fallback(ui, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", ui / "none.yaml")
    config.add_creator("alpha", credit="A")
    config.add_creator("beta", credit="B")
    cfg = config.load()
    assert config.creator_for(cfg, "beta")["credit"] == "B"
    assert config.creator_for(cfg, "@BETA")["credit"] == "B"           # case / @ tolerant
    assert config.creator_for(cfg, "unknown")["credit"] == "A"         # falls back to the first


# ------------------------------------------------------------------ the Vertex toggle

def test_vertex_toggle(ui):
    config.ui_set(use_vertex=False)
    assert config.load()["gemini"]["backends"] == ["aistudio"]
    config.ui_set(use_vertex=True)
    assert config.load()["gemini"]["backends"] == ["vertex", "aistudio"]


# ------------------------------------------------------------------ the monitor loops creators

def test_poll_watches_every_enabled_creator(ui, fresh_db, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", ui / "none.yaml")
    config.add_creator("one")
    config.add_creator("two")
    config.set_creator_enabled("two", True)
    config.add_creator("paused")
    config.set_creator_enabled("paused", False)

    def fake_list(cfg, handle=None):
        return [{"id": f"{handle}1" + "0" * 15, "url": f"https://tiktok/@{handle}/1"}]

    monkeypatch.setattr(monitor, "list_latest", fake_list)
    monkeypatch.setattr(monitor, "video_time", lambda vid: 10 ** 18)   # always "new"
    new = monitor.poll_once(config.load())
    assert new == 2                                                    # one + two, not the paused one
    handles = {db.get(f"one1{'0' * 15}")["handle"], db.get(f"two1{'0' * 15}")["handle"]}
    assert handles == {"one", "two"}


def test_one_creator_failing_does_not_stop_the_others(ui, fresh_db, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", ui / "none.yaml")
    config.add_creator("good")
    config.add_creator("bad")

    def fake_list(cfg, handle=None):
        if handle == "bad":
            raise monitor.MonitorError("blocked")
        return [{"id": "9" * 19, "url": "u"}]

    monkeypatch.setattr(monitor, "list_latest", fake_list)
    monkeypatch.setattr(monitor, "video_time", lambda vid: 10 ** 18)
    assert monitor.poll_once(config.load()) == 1                      # good creator still queued


# ------------------------------------------------------------------ per-clip credit + auto-finish

def test_captions_use_the_clips_own_creator():
    cfg = config._merge(config.DEFAULTS, {})
    creator = config.creator_entry("star", credit="🎥 @star", hashtags=["#star"])
    caps = worker.build_captions({"caption": "hi", "hashtags": ["#clip"]}, cfg, creator)
    assert "🎥 @star" in caps["tiktok"] and "#star" in caps["tiktok"] and "#clip" in caps["tiktok"]


def test_auto_finish_sends_a_cut_clip_straight_to_ready(fresh_db, tmp_path, monkeypatch):
    from clipper import cutter
    vid = "123456789012345678-01"
    db.insert_clip(vid, {"video_id": "123456789012345678", "url": "u", "source": "monitor", "handle": "x"},
                   {"start_s": 0, "end_s": 5}, state="NEW")
    monkeypatch.setattr(cutter, "cut", lambda *a, **k: {"start": 0, "end": 5})
    monkeypatch.setattr(cutter, "thumbnail", lambda *a, **k: None)
    monkeypatch.setattr(config, "job_dir", lambda v: tmp_path)
    cfg = config._merge(config.DEFAULTS, {"pipeline": {"auto_finish": True}})
    stopped = worker.run_step(db.get(vid), "cut", cfg)
    assert stopped == "CUT" and db.get(vid)["transform_mode"] == "original"  # ready to flow to READY, no pick


def test_auto_pick_previews_moves_waiting_clips(fresh_db):
    p = {"video_id": "111222333444555666", "url": "u", "source": "monitor", "handle": "x"}
    db.insert_clip("111222333444555666-01", p, {"start_s": 0, "end_s": 5}, state="PREVIEW")
    db.insert_clip("111222333444555666-02", p, {"start_s": 6, "end_s": 9}, state="PREVIEW", hold=True)
    db.insert_clip("111222333444555666-03", p, {"start_s": 9, "end_s": 12}, state="READY")
    assert db.auto_pick_previews() == 1                       # only the un-held PREVIEW clip
    assert db.get("111222333444555666-01")["state"] == "CUT"
    assert db.get("111222333444555666-01")["transform_mode"] == "original"
    assert db.get("111222333444555666-02")["state"] == "PREVIEW"   # held clip left alone
