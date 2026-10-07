"""Consistent albino look across pieces: stricter wording, reference look, model choice."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clipper import app as webapp
from clipper import config, db, transform_adapter, worker
from tests.conftest import make_clip, needs_ffmpeg
from tests.test_transform_adapter import STUB

VID = "7412345678901234567"
CID = f"{VID}-01"


def _cfg(script, **transform):
    return config._merge(config.DEFAULTS, {"transform": {"enabled": True, "script_path": str(script),
                                                          "variant": "albinism", **transform}})


def test_strict_and_reference_wording():
    text = transform_adapter.instruction_for("BASE.", "the man in black", strict=True, reference=True)
    assert text.startswith("BASE. The skin must be evenly pale")
    assert "no freckles" in text and "attached image" in text and "ONLY to this person: the man in black." in text
    assert transform_adapter.instruction_for("BASE.", "") == "BASE."


def test_cache_names_change_with_model_wording_and_reference():
    plain = transform_adapter.variant_key("albinism", "")
    assert plain == "albinism"
    keys = {transform_adapter.variant_key("albinism", "", *extra)
            for extra in [("m1",), ("m2",), ("m1", "strict"), ("m1", "strict", "ref123")]}
    assert len(keys) == 4 and plain not in keys


class FakeModels:
    def __init__(self, available):
        self.available, self.asked = available, []

    def get(self, model):
        self.asked.append(model)
        if model not in self.available:
            raise RuntimeError("404 NOT_FOUND")
        return SimpleNamespace(name=model)


def test_first_available_model_wins(monkeypatch):
    transform_adapter._resolved_models.clear()
    models = FakeModels({"gemini-omni-1.1-flash-preview"})
    mod = SimpleNamespace(make_client=lambda: SimpleNamespace(models=models))
    chosen = transform_adapter.resolve_model(mod, ["gemini-omni-1.1-flash", "gemini-omni-1.1-flash-preview"])
    assert chosen == "gemini-omni-1.1-flash-preview"
    assert models.asked == ["gemini-omni-1.1-flash", "gemini-omni-1.1-flash-preview"]
    transform_adapter._resolved_models.clear()
    models = FakeModels({"gemini-omni-1.1-flash", "gemini-omni-1.1-flash-preview"})
    assert transform_adapter.resolve_model(mod, ["gemini-omni-1.1-flash", "gemini-omni-1.1-flash-preview"]) \
        == "gemini-omni-1.1-flash"
    assert transform_adapter.resolve_model(mod, "") == ""
    transform_adapter._resolved_models.clear()


REF_SCRIPT = STUB.replace("FAIL = set()", '''FAIL = set()
VIDEO_MODEL_NAME = "gemini-omni-flash-preview"
SENT = []
REJECT_IMAGES = False
def build_edit_input(segment_path, instruction):
    return [{"type": "video", "data": "v"}, {"type": "text", "text": instruction}]
''').replace('''def edit_segment(client, segment_path, index, verbose):
    if index in FAIL:''', '''def edit_segment(client, segment_path, index, verbose):
    parts = build_edit_input(segment_path, EDIT_INSTRUCTION)
    if REJECT_IMAGES and any(p["type"] == "image" for p in parts):
        raise RuntimeError("400 INVALID_ARGUMENT: image input not supported here")
    SENT.append((VIDEO_MODEL_NAME, [p["type"] for p in parts], parts[-1]["text"]))
    if index in FAIL:''')


@pytest.fixture
def ref_script(tmp_path):
    script = tmp_path / "script" / "transform_video.py"
    script.parent.mkdir()
    script.write_text(REF_SCRIPT)
    transform_adapter._resolved_models.clear()
    yield script
    transform_adapter._modules.clear()
    transform_adapter._resolved_models.clear()


@needs_ffmpeg
def test_own_instruction_replaces_the_scripts_wording(tmp_path, ref_script, monkeypatch):
    mod = transform_adapter.load_script(ref_script)
    monkeypatch.setattr(transform_adapter, "resolve_model", lambda m, wanted: "")
    clip = make_clip(tmp_path / "highlight.mp4", seconds=2)
    transform_adapter.run(clip, tmp_path, _cfg(ref_script, instruction="Do the  thing.\nDo not edit the speech."))
    assert mod.SENT[-1][2] == "Do the thing. Do not edit the speech."  # whitespace tidied, script text unused
    first_key = mod.EDIT_VARIANT
    transform_adapter.run(clip, tmp_path, _cfg(ref_script, instruction="Something else."))
    assert mod.EDIT_VARIANT != first_key  # a different instruction never reuses the other one's edits


@needs_ffmpeg
def test_reference_picture_goes_with_every_piece(tmp_path, ref_script, monkeypatch):
    mod = transform_adapter.load_script(ref_script)
    monkeypatch.setattr(transform_adapter, "resolve_model", lambda m, wanted: "omni-test")
    clip = make_clip(tmp_path / "highlight.mp4", seconds=2)
    ref = tmp_path / "look.jpg"
    ref.write_bytes(b"jpeg")
    info = transform_adapter.run(clip, tmp_path, _cfg(ref_script, model="omni-test", strict_look=True),
                                 reference_image=ref)
    model, types_sent, text = mod.SENT[-1]
    assert model == "omni-test" and types_sent == ["video", "image", "text"]
    assert "no freckles" in text and "attached image" in text
    assert info["reference"] is True and info["model"] == "omni-test" and info["reference_rejected"] is None


@needs_ffmpeg
def test_reference_refused_as_speech_edit_falls_back_without_it(tmp_path, ref_script, monkeypatch):
    mod = transform_adapter.load_script(ref_script)
    original_edit = mod.edit_segment

    def refuse_with_picture(client, segment_path, index, verbose):
        if any(p["type"] == "image" for p in mod.build_edit_input(segment_path, "x")):
            raise transform_adapter.ContentBlocked("The model is currently unable to process speech edits.")
        return original_edit(client, segment_path, index, verbose)

    mod.edit_segment = refuse_with_picture
    monkeypatch.setattr(transform_adapter, "resolve_model", lambda m, wanted: "")
    clip = make_clip(tmp_path / "highlight.mp4", seconds=2)
    ref = tmp_path / "look.jpg"
    ref.write_bytes(b"jpeg")
    info = transform_adapter.run(clip, tmp_path, _cfg(ref_script, strict_look=True), reference_image=ref)
    assert info["blocked"] is None and info["fallback_segments"] == [] and info["reference"] is False
    assert "speech edits" in info["reference_rejected"]


@needs_ffmpeg
def test_reference_rejected_falls_back_without_it(tmp_path, ref_script, monkeypatch):
    mod = transform_adapter.load_script(ref_script)
    mod.REJECT_IMAGES = True
    monkeypatch.setattr(transform_adapter, "resolve_model", lambda m, wanted: "")
    clip = make_clip(tmp_path / "highlight.mp4", seconds=2)
    ref = tmp_path / "look.jpg"
    ref.write_bytes(b"jpeg")
    info = transform_adapter.run(clip, tmp_path, _cfg(ref_script), reference_image=ref)
    assert info["reference"] is False and "INVALID_ARGUMENT" in info["reference_rejected"]
    assert info["fallback_segments"] == []  # the piece was still edited, just without the picture
    assert mod.SENT[-1][1] == ["video", "text"]


# ------------------------------------------------------------- the LOOK step

def test_transform_waits_for_an_approved_look(fresh_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path)
    made, ran = [], []
    monkeypatch.setattr(transform_adapter, "make_look", lambda *a, **k: made.append(1))
    monkeypatch.setattr(transform_adapter, "run", lambda *a, **k: ran.append(k.get("reference_image")) or
                        {"enabled": True, "segments": 1, "fallback_segments": [], "refused": False})
    cfg = config._merge(config.DEFAULTS, {"transform": {"reference": True}})
    db.insert_if_new(CID, "u", state="CUT")
    db.update(CID, kind="clip", transform_mode="albino")
    worker.advance(db.get(CID), cfg)
    assert db.get(CID)["state"] == "LOOK" and made == [1] and ran == []
    assert db.next_actionable() is None  # waits for the user
    (config.job_dir(CID) / "look.jpg").write_bytes(b"x")
    info = db.get(CID)["info"]
    db.update(CID, state="CUT", info={**info, "look": {**info["look"], "status": "approved"}})
    worker.advance(db.get(CID), cfg)
    assert db.get(CID)["state"] == "TRANSFORMED" and ran[0].name == "look.jpg" and made == [1]


def test_new_target_needs_a_new_look(fresh_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path)
    made = []
    monkeypatch.setattr(transform_adapter, "make_look", lambda *a, **k: made.append(a[3]))
    cfg = config._merge(config.DEFAULTS, {"transform": {"reference": True}})
    db.insert_if_new(CID, "u", state="CUT")
    db.update(CID, kind="clip", transform_mode="albino", target="the man on the left",
              info={"look": {"status": "approved", "target": "someone else"}})
    worker.advance(db.get(CID), cfg)
    assert db.get(CID)["state"] == "LOOK" and made == ["the man on the left"]


def test_use_as_is_never_makes_a_look(fresh_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(transform_adapter, "make_look", lambda *a, **k: pytest.fail("made a look"))
    monkeypatch.setattr(transform_adapter, "run", lambda *a, **k: {"enabled": False, "segments": 0,
                                                                   "fallback_segments": [], "refused": False})
    cfg = config._merge(config.DEFAULTS, {"transform": {"reference": True}})
    db.insert_if_new(CID, "u", state="CUT")
    db.update(CID, kind="clip", transform_mode="original")
    worker.advance(db.get(CID), cfg)
    assert db.get(CID)["state"] == "TRANSFORMED"


@pytest.fixture
def client(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    return TestClient(webapp.app)


def _look_clip():
    db.insert_if_new(VID, "u", state="CLIPPED")
    db.insert_clip(CID, db.get(VID), {"start_s": 0, "end_s": 20}, info={"look": {"status": "pending", "target": ""}})
    db.update(CID, state="LOOK", cut_range={"start": 0, "end": 20})
    d = config.job_dir(CID)
    (d / "look.jpg").write_bytes(b"x")
    (d / "look_source.jpg").write_bytes(b"x")


def test_look_page_and_approve(client, fresh_db):
    _look_clip()
    page = client.get(f"/jobs/{CID}").text
    assert "Approve the albino look" in page and "/media/" + CID + "/look.jpg" in page
    assert "Use this look" in page and CID in client.get("/library?tab=preview").text
    client.post(f"/jobs/{CID}/look/approve")
    job = db.get(CID)
    assert job["state"] == "CUT" and job["info"]["look"]["status"] == "approved"


def test_look_redo_and_skip(client, fresh_db):
    _look_clip()
    client.post(f"/jobs/{CID}/look/redo")
    assert db.get(CID)["state"] == "CUT" and not (config.job_dir(CID) / "look.jpg").exists()
    db.update(CID, state="LOOK")
    client.post(f"/jobs/{CID}/look/skip")
    assert db.get(CID)["info"]["look"]["status"] == "skipped"


def test_resolution_is_added_to_every_request():
    sent = []
    inner = SimpleNamespace(interactions=SimpleNamespace(create=lambda **kw: sent.append(kw) or "ok",
                                                         get=lambda i: f"got {i}"))
    mod = SimpleNamespace(make_client=lambda: inner)
    cfg = config._merge(config.DEFAULTS, {"transform": {"resolution": "1080p"}})
    client = transform_adapter.client_for(mod, cfg)
    client.interactions.create(model="m", input=[], response_format={"type": "video", "aspect_ratio": "9:16"})
    assert sent[0]["response_format"] == {"type": "video", "aspect_ratio": "9:16", "resolution": "1080p"}
    assert client.interactions.get("x") == "got x"  # status polling passes straight through
    plain = transform_adapter.client_for(mod, config._merge(config.DEFAULTS, {"transform": {"resolution": ""}}))
    assert plain is inner
