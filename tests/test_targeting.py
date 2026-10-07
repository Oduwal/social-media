"""Making only one person albino when a clip shows several people."""

import pytest
from fastapi.testclient import TestClient

from clipper import app as webapp
from clipper import config, db, highlight, transform_adapter, worker
from tests.conftest import make_clip, needs_ffmpeg
from tests.test_transform_adapter import STUB, _cfg

VID = "7412345678901234567"
CFG = config._merge(config.DEFAULTS, {"creator": {"handle": "c"}})


def test_instruction_names_only_the_target():
    base = "Change the pigmentation of the person."
    assert transform_adapter.instruction_for(base, "") == base
    text = transform_adapter.instruction_for(base, "  the man in the\n striped shirt ")
    assert text.startswith(base)
    assert "ONLY to this person: the man in the striped shirt." in text
    assert "Every other person" in text


def test_each_target_gets_its_own_cache_name():
    a = transform_adapter.variant_key("albinism", "man in striped shirt")
    assert a.startswith("albinism-") and a != "albinism"
    assert a == transform_adapter.variant_key("albinism", "Man in  striped shirt")  # same person, same cache
    assert a != transform_adapter.variant_key("albinism", "woman in red")
    assert transform_adapter.variant_key("albinism", "") == "albinism"


def test_target_is_tidied():
    assert transform_adapter.clean_target(" a \n b ") == "a b"
    assert len(transform_adapter.clean_target("x" * 500)) == 300
    assert transform_adapter.clean_target(None) == ""


def test_gemini_schema_asks_for_people_and_creator():
    fields = highlight.Clip.model_fields
    assert "people_count" in fields and "creator_description" in fields


def test_gemini_schema_has_no_limits_vertex_rejects():
    """Vertex returns 400 INVALID_ARGUMENT for this schema once it carries numeric/length
    limits (verified live on 2026-10-03); they're enforced in clean_clips instead."""
    import json
    schema = json.dumps(highlight.Highlights.model_json_schema())
    for word in ("minimum", "maximum", "minItems", "maxItems", "exclusiveM"):
        assert word not in schema, word


def test_clean_clips_enforces_the_limits():
    clips = [{"start_s": i * 20, "end_s": i * 20 + 15, "score": 99, "people_count": -3,
              "hashtags": [f"#t{n}" for n in range(9)]} for i in range(40)]
    kept = highlight.clean_clips(clips, duration=2000)
    assert len(kept) == highlight.MAX_CLIPS
    assert all(c["score"] == 10 and c["people_count"] == 0 and len(c["hashtags"]) == 5 for c in kept)


@pytest.fixture
def stub(tmp_path):
    script = tmp_path / "script" / "transform_video.py"
    script.parent.mkdir()
    script.write_text(STUB)
    yield script
    transform_adapter._modules.clear()


@needs_ffmpeg
def test_adapter_sends_target_and_never_reuses_another_persons_edits(tmp_path, stub):
    mod = transform_adapter.load_script(stub)
    seen = []
    real_edit = mod.edit_segment

    def edit(client, seg, index, verbose):
        seen.append(mod.EDIT_INSTRUCTION)
        return real_edit(client, seg, index, verbose)

    mod.edit_segment = edit
    clip = make_clip(tmp_path / "highlight.mp4", seconds=2)
    info = transform_adapter.run(clip, tmp_path, _cfg(stub), target="the man in the striped shirt")
    assert info["target"] == "the man in the striped shirt"
    assert "the man in the striped shirt" in seen[-1]
    transform_adapter.run(clip, tmp_path, _cfg(stub), target="the woman in red")
    edited = sorted(p.name for p in (tmp_path / "transform_work" / "edited").iterdir())
    assert len(edited) == 2, edited  # one cached edit per person, not one reused for both
    assert "the woman in red" in seen[-1]


REFUSING_SCRIPT = STUB.replace("FAIL = set()", '''FAIL = set()
CALLS = []
class SegmentEditFailed(RuntimeError):
    pass
class _Err:
    code = "content_blocked"
    message = "The model is currently unable to process speech edits."
class _Interaction:
    errors = [_Err()]
def dump_failed_interaction(interaction, index):
    return None
def _attempt_edit(client, segment_path, index, clip_path, verbose):
    CALLS.append(index)
    dump_failed_interaction(_Interaction(), index)
    raise SegmentEditFailed("status=failed")
''').replace('''def edit_segment(client, segment_path, index, verbose):
    if index in FAIL:''', '''def edit_segment(client, segment_path, index, verbose):
    for attempt in range(3):  # like the real script: retries SegmentEditFailed
        try:
            return _attempt_edit(client, segment_path, index, None, verbose)
        except SegmentEditFailed:
            pass
    raise RuntimeError("gave up")
    if index in FAIL:''')


@needs_ffmpeg
def test_a_refusal_is_not_retried_and_stops_the_clip(tmp_path):
    script = tmp_path / "script" / "transform_video.py"
    script.parent.mkdir()
    script.write_text(REFUSING_SCRIPT)
    mod = transform_adapter.load_script(script)
    mod.split_video = lambda video: [mod.WORK_PATH / f"s{i}.mp4" for i in (1, 2, 3)]
    (tmp_path / "transform_work").mkdir()
    for i in (1, 2, 3):
        make_clip(tmp_path / "transform_work" / f"s{i}.mp4", seconds=2)
    clip = make_clip(tmp_path / "highlight.mp4", seconds=6)
    try:
        info = transform_adapter.run(clip, tmp_path, _cfg(script))
    finally:
        transform_adapter._modules.clear()
    assert mod.CALLS == [1], "retried a refusal, or kept sending segments after it"
    assert info["blocked"].startswith("The model is currently unable to process speech edits")
    assert info["fallback_segments"] == [1, 2, 3] and info["refused"] is True


def test_clips_with_several_people_get_a_prefilled_target(fresh_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path)
    clips = [{"title": "a", "start_s": 0, "end_s": 20, "score": 9, "people_count": 2,
              "creator_description": "the man in the blue striped shirt, driver's seat"},
             {"title": "b", "start_s": 30, "end_s": 50, "score": 8, "people_count": 1,
              "creator_description": "the man talking"}]
    monkeypatch.setattr(highlight, "pick", lambda *a, **k: {"clips": clips})
    db.insert_if_new(VID, "u", state="HIGHLIGHTED")
    worker.make_clips(db.get(VID), CFG)
    assert db.get(f"{VID}-01")["target"] == "the man in the blue striped shirt, driver's seat"
    assert db.get(f"{VID}-02")["target"] == ""


def test_transform_step_passes_the_target(fresh_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path)
    got = {}

    def fake_run(highlight_path, job_dir, cfg, target="", reference_image=None):
        got["target"] = target
        return {"enabled": True, "segments": 1, "fallback_segments": [], "refused": False}

    monkeypatch.setattr(worker.transform_adapter, "run", fake_run)
    db.insert_if_new("6", "u", state="CUT")
    db.update("6", kind="clip", target="the woman in red")
    worker.advance(db.get("6"), CFG)
    assert got["target"] == "the woman in red"


@pytest.fixture
def client(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "MUSIC_DIR", tmp_path / "music")
    (tmp_path / "music").mkdir()
    return TestClient(webapp.app)


def _clip(state, people=2, target=""):
    db.insert_if_new(VID, "u", state="CLIPPED")
    cid = f"{VID}-01"
    db.insert_clip(cid, db.get(VID), {"start_s": 0, "end_s": 20, "people_count": people}, target=target)
    db.update(cid, state=state, cut_range={"start": 0, "end": 20})
    return cid


def test_pick_saves_the_edited_target(client, fresh_db):
    cid = _clip("PREVIEW", target="gemini's guess")
    page = client.get(f"/jobs/{cid}")
    assert "Gemini sees 2 people" in page.text and "gemini&#39;s guess" in page.text
    client.post("/clips/pick", data={"mode": "albino", "ids": [cid], f"target_{cid}": "the one on the left"})
    job = db.get(cid)
    assert job["state"] == "CUT" and job["target"] == "the one on the left"


def test_library_badge_warns_when_nobody_is_named(client, fresh_db):
    _clip("PREVIEW", target="")
    assert "say who to change" in client.get("/library").text


def test_redo_for_a_new_person(client, fresh_db):
    cid = _clip("READY", target="old")
    db.update(cid, transform_info={"enabled": True, "segments": 2, "fallback_segments": [], "refused": False,
                                   "target": "old"})
    make_clip(config.job_dir(cid) / "final.mp4", seconds=1)
    assert "Who was made albino" in client.get(f"/jobs/{cid}").text
    client.post(f"/jobs/{cid}/retransform", data={"target": "the man on the right"})
    job = db.get(cid)
    assert job["state"] == "CUT" and job["target"] == "the man on the right"


def test_target_cannot_change_mid_transform(client, fresh_db):
    cid = _clip("CUT")
    assert client.post(f"/jobs/{cid}/target", data={"target": "x"}).status_code == 409
    db.update(cid, state="PREVIEW")
    client.post(f"/jobs/{cid}/target", data={"target": "  the man \n on the left "})
    assert db.get(cid)["target"] == "the man on the left"
