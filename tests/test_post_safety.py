"""The posting step must never post the same clip twice, or post without approval."""

from clipper import config, publisher, worker

CFG = config._merge(config.DEFAULTS, {"upload_post": {"profile": "myclips"}, "posting": {"service": "upload_post"}})
ALL = ("tiktok", "instagram", "x")


def _approved(db, platforms=ALL):
    db.insert_if_new("7", "u", state="APPROVED")
    db.update("7", platforms=list(platforms), captions={}, ai_label=True)


def _ok(platforms):
    return {"status": "completed", "request_id": "r", "platforms": {p: {"ok": True} for p in platforms}}


def test_request_id_saved_before_waiting_so_retry_never_reuploads(fresh_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path)
    uploads, checks = [], []

    def fake_publish(video, vid, attempt, captions, platforms, ai_label, cfg, on_submitted=None):
        uploads.append(attempt)
        on_submitted("req-1")
        raise ConnectionError("network dropped while waiting")

    def fake_check(request_id):
        checks.append(request_id)
        return {**_ok(ALL), "request_id": request_id}

    monkeypatch.setattr(publisher, "publish", fake_publish)
    monkeypatch.setattr(publisher, "check_status", fake_check)
    _approved(fresh_db)
    worker.advance(fresh_db.get("7"), CFG)
    assert fresh_db.get("7")["state"] == "FAILED"
    assert fresh_db.get("7")["post_result"]["request_id"] == "req-1"

    fresh_db.retry("7")
    worker.advance(fresh_db.get("7"), CFG)
    assert uploads == [1], "uploaded a second time"
    assert checks == ["req-1"]
    assert fresh_db.get("7")["state"] == "POSTED"


def test_unknown_upload_outcome_reuses_idempotency_key(fresh_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path)
    keys = []

    def fake_publish(video, vid, attempt, captions, platforms, ai_label, cfg, on_submitted=None):
        keys.append(attempt)
        if len(keys) == 1:
            raise TimeoutError("read timeout during upload")  # did it arrive? unknown
        return _ok(platforms)

    monkeypatch.setattr(publisher, "publish", fake_publish)
    _approved(fresh_db)
    worker.advance(fresh_db.get("7"), CFG)
    fresh_db.retry("7")
    worker.advance(fresh_db.get("7"), CFG)
    assert keys == [1, 1]
    assert fresh_db.get("7")["state"] == "POSTED"


def test_definite_rejection_gets_a_fresh_key(fresh_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path)
    keys = []

    def fake_publish(video, vid, attempt, captions, platforms, ai_label, cfg, on_submitted=None):
        keys.append(attempt)
        if len(keys) == 1:
            raise publisher.PublishError("400 bad caption", definite=True)
        return _ok(platforms)

    monkeypatch.setattr(publisher, "publish", fake_publish)
    _approved(fresh_db)
    worker.advance(fresh_db.get("7"), CFG)
    fresh_db.retry("7")
    worker.advance(fresh_db.get("7"), CFG)
    assert keys == [1, 2]


def test_poll_errors_become_a_timeout_that_keeps_the_request_id(monkeypatch):
    def broken(request_id):
        raise ConnectionError("x")

    monkeypatch.setattr(publisher, "POLL_EVERY_S", 0)
    monkeypatch.setattr(publisher, "POLL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(publisher, "check_status", broken)
    result = publisher.wait_for("req-9")
    assert result["status"] == "timeout" and result["request_id"] == "req-9"


def test_rejecting_after_pickup_is_not_posted_or_overwritten(fresh_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path)

    def must_not_post(*args, **kwargs):
        raise AssertionError("posted a rejected clip")

    monkeypatch.setattr(publisher, "publish", must_not_post)
    _approved(fresh_db, ["x"])
    stale = fresh_db.get("7")              # the worker's copy, taken while APPROVED
    fresh_db.update("7", state="REJECTED")  # the user rejects before the step starts
    worker.advance(stale, CFG)
    assert fresh_db.get("7")["state"] == "REJECTED"


def test_step_result_does_not_overwrite_a_rejection_made_mid_step(fresh_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path)
    fresh_db.insert_if_new("8", "u", state="TRANSFORMED")

    def reject_during_mix(job, step, cfg):
        fresh_db.update("8", state="REJECTED")

    monkeypatch.setattr(worker, "run_step", reject_during_mix)
    worker.advance(fresh_db.get("8"), CFG)
    assert fresh_db.get("8")["state"] == "REJECTED"


def test_saved_ai_label_choice_survives_a_retransform(fresh_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path)
    info = {"enabled": True, "segments": 2, "fallback_segments": [], "refused": False}
    monkeypatch.setattr(worker.transform_adapter, "run", lambda *a: info)
    fresh_db.insert_if_new("9", "u", state="CUT")
    fresh_db.update("9", ai_label=False, transform_info={**info, "fallback_segments": [2]})
    worker.advance(fresh_db.get("9"), CFG)
    assert fresh_db.get("9")["ai_label"] is False
