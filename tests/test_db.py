import time


def test_insert_is_idempotent(fresh_db):
    assert fresh_db.insert_if_new("1", "u") is True
    assert fresh_db.insert_if_new("1", "u") is False


def test_json_fields_round_trip(fresh_db):
    fresh_db.insert_if_new("1", "u")
    fresh_db.update("1", captions={"x": "hi"}, platforms=["x"], ai_label=True)
    job = fresh_db.get("1")
    assert job["captions"] == {"x": "hi"}
    assert job["platforms"] == ["x"]
    assert job["ai_label"] is True


def test_next_actionable_prefers_approved_then_oldest(fresh_db):
    fresh_db.insert_if_new("old", "u")
    time.sleep(0.03)  # Windows' clock ticks every ~16 ms: make "old" really older
    fresh_db.insert_if_new("new", "u")
    fresh_db.insert_if_new("ready", "u", state="READY")
    fresh_db.insert_if_new("skip", "u", state="SKIPPED")
    assert fresh_db.next_actionable()["video_id"] == "old"
    fresh_db.update("ready", state="APPROVED")
    assert fresh_db.next_actionable()["video_id"] == "ready"


def test_nothing_actionable_when_waiting_or_done(fresh_db):
    for vid, state in [("a", "READY"), ("b", "POSTED"), ("c", "SKIPPED"), ("d", "FAILED")]:
        fresh_db.insert_if_new(vid, "u", state=state)
    assert fresh_db.next_actionable() is None


def test_retry_resets_to_state_before_failed_step(fresh_db):
    fresh_db.insert_if_new("1", "u")
    fresh_db.mark_failed("1", "transform", "boom")
    fresh_db.retry("1")
    job = fresh_db.get("1")
    assert job["state"] == "CUT" and job["error"] is None


def test_interrupted_post_goes_back_to_ready_not_reposted(fresh_db):
    fresh_db.insert_if_new("1", "u", state="POSTING")
    fresh_db.reset_interrupted()
    assert fresh_db.get("1")["state"] == "READY"


def test_kv(fresh_db):
    assert fresh_db.kv_get("missing", 5) == 5
    fresh_db.kv_set("k", {"a": 1})
    assert fresh_db.kv_get("k") == {"a": 1}
