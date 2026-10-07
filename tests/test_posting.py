from clipper import config, publisher, worker

CFG = config._merge(config.DEFAULTS, {
    "creator": {"handle": "creator", "credit": "🎥 @creator", "hashtags": ["#creator", "clips"]},
    "upload_post": {"profile": "myclips", "tiktok_privacy_level": "SELF_ONLY"},
    "posting": {"service": "upload_post"},
})


def test_form_only_includes_selected_platforms_and_labels():
    form = publisher.build_form({"tiktok": "tt", "x": "xx"}, ["tiktok", "x"], CFG, ai_label=True)
    d = {}
    for k, v in form:
        d.setdefault(k, []).append(v)
    assert d["platform[]"] == ["tiktok", "x"]
    assert d["tiktok_title"] == ["tt"] and d["x_title"] == ["xx"]
    assert d["is_aigc"] == ["true"] and d["made_with_ai"] == ["true"]
    assert d["privacy_level"] == ["SELF_ONLY"]
    assert "instagram_title" not in d and "is_ai_generated" not in d
    assert d["user"] == ["myclips"]


def test_summarize_handles_dict_and_list_results():
    body = {"status": "completed", "results": [
        {"platform": "tiktok", "success": True, "url": "https://t/1", "fallback_to_inbox": True},
        {"platform": "x", "success": False, "message": "nope"}]}
    s = publisher.summarize(body, "r1")
    assert s["platforms"]["tiktok"] == {"ok": True, "url": "https://t/1", "message": "", "drafted": True, "skipped": False}
    assert s["platforms"]["x"]["ok"] is False
    s2 = publisher.summarize({"success": True, "results": {"instagram": {"success": True, "url": "u"}}})
    assert s2["status"] == "completed" and s2["platforms"]["instagram"]["ok"]


def test_captions_x_fits_280_and_dedupes_hashtags():
    hl = {"caption_suggestion": "word " * 100, "hashtags": ["#Creator", "#funny"]}
    caps = worker.build_captions(hl, CFG)
    assert len(caps["x"]) <= 280
    assert "🎥 @creator" in caps["x"] and "🎥 @creator" in caps["tiktok"]
    assert caps["tiktok"].count("#creator") + caps["tiktok"].count("#Creator") == 1
    assert "#clips" in caps["tiktok"] and "#funny" in caps["tiktok"]


def test_retry_after_partial_failure_only_posts_failed_platforms(fresh_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path)
    calls = []

    def fake_publish(video, vid, attempt, captions, platforms, ai_label, cfg, on_submitted=None):
        calls.append((attempt, list(platforms)))
        return {"status": "completed", "request_id": "r",
                "platforms": {p: {"ok": p != "x" or attempt > 1} for p in platforms}}

    monkeypatch.setattr(publisher, "publish", fake_publish)
    fresh_db.insert_if_new("7", "u", state="APPROVED")
    fresh_db.update("7", platforms=["tiktok", "instagram", "x"], captions={}, ai_label=True)

    worker.advance(fresh_db.get("7"), CFG)
    assert fresh_db.get("7")["state"] == "FAILED"
    fresh_db.retry("7")
    worker.advance(fresh_db.get("7"), CFG)

    assert calls == [(1, ["tiktok", "instagram", "x"]), (2, ["x"])]
    job = fresh_db.get("7")
    assert job["state"] == "POSTED"
    assert all(r["ok"] for r in job["post_result"]["platforms"].values())
