"""AI intro (hook + Algenib voice), its editor project, the render, and image-overlay ideas."""

import math
import struct
import subprocess
import types

import pytest
from fastapi.testclient import TestClient

from clipper import app as webapp
from clipper import batch_edit, captions, config, db, editor2, highlight, intro, media, project as P, render, render2
from clipper import worker
from tests.conftest import make_clip, needs_ffmpeg

CFG = {"music": {"ducking": True}, "watermark": {"enabled": False, "text": "", "opacity": 0.35,
                                                   "position": "bottom-right", "size": 4}}


def make_wav(path, seconds=1.5):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"sine=frequency=220:duration={seconds}", "-ar", "44100", str(path)], check=True)
    return path


@pytest.fixture
def client(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    config.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(webapp, "INTRO_PREVIEW", tmp_path / "preview")
    return TestClient(webapp.app)


def _clip(clip_id, state="PREVIEW", seconds=6):
    parent = {"video_id": clip_id.split("-")[0], "url": "u", "source": "manual"}
    db.insert_clip(clip_id, parent, {"title": "t", "spoken_words": "abeg now", "hook": "h"},
                   info={"title": "Why I quit"}, handle="gehgeh")
    db.update(clip_id, state=state)
    folder = config.job_dir(clip_id)
    make_clip(folder / "transformed.mp4", seconds=seconds, size="1080x1920")
    return folder


# ------------------------------------------------------------------ the hook line

def test_clean_hook_strips_emoji_hashtags_quotes_and_caps_the_length():
    text = intro.clean_hook('"Is Gehgeh actually right about this?" \U0001F602 #fyp ' + "word " * 30)
    assert "#" not in text and "\U0001F602" not in text and '"' not in text
    assert text.startswith("Is Gehgeh actually right about this?") and len(text.split()) <= intro.MAX_WORDS


def test_spec_knows_the_intro_options():
    spec = batch_edit.clean_spec({"ai_intro": "on", "intro_text": "  Wait for it  "})
    assert spec["ai_intro"] is True and spec["intro_text"] == "Wait for it"
    assert batch_edit.clean_spec({})["ai_intro"] is False


# ------------------------------------------------------------------ the voice

@needs_ffmpeg
def test_speak_makes_a_faster_wav_and_replaces_the_old_one(tmp_path, monkeypatch):
    pcm = b"".join(struct.pack("<h", int(8000 * math.sin(i / 20))) for i in range(24000 * 2))   # 2 s tone
    monkeypatch.setattr(intro, "_gemini_pcm", lambda text: pcm)
    (tmp_path / "ai_intro.mp3").write_bytes(b"old")
    out = intro.speak("Is Gehgeh right about this?", tmp_path)
    assert out.name == "ai_intro.wav" and not (tmp_path / "ai_intro.mp3").exists()
    assert not (tmp_path / "ai_intro.raw.wav").exists()
    assert 2 / 1.15 - 0.2 < media.duration(out) < 2 / 1.15 + 0.2          # 15% faster


def test_speak_falls_back_to_ai33_only_when_a_key_is_set(tmp_path, monkeypatch):
    def broken(text):
        raise intro.VoiceError("all busy")

    monkeypatch.setattr(intro, "_gemini_pcm", broken)
    monkeypatch.setattr(intro, "_ai33_audio", lambda text, speed: b"mp3-bytes")
    monkeypatch.delenv("AI33_API_KEY", raising=False)
    with pytest.raises(intro.VoiceError):
        intro.speak("hello there", tmp_path)
    monkeypatch.setenv("AI33_API_KEY", "k")
    assert intro.speak("hello there", tmp_path).name == "ai_intro.mp3"


def test_the_voice_is_algenib_and_asks_for_a_deep_nigerian_tone():
    assert intro.VOICE == "Algenib" and intro.SPEED > 1.0
    assert "deep" in intro.TONE and "Nigerian" in intro.TONE


# ------------------------------------------------------------------ the editor project

@needs_ffmpeg
def test_batch_project_has_voice_hook_dipped_clip_and_clear_captions(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    folder = _clip("20-01", seconds=8)
    wav = make_wav(folder / editor2.UPLOADS / "ai_intro.wav", seconds=3.0)

    monkeypatch.setattr(intro, "make_intro", lambda job, d, cfg, text="": {
        "text": "Is Gehgeh actually right?", "file": wav.name, "duration": 3.0})
    monkeypatch.setattr(captions, "captions_for", lambda d, a, b, ref="": [
        {"text": "inside the intro", "start": 0.5, "end": 2.0, "words": None, **captions.STYLE},
        {"text": "after it", "start": 4.0, "end": 6.0, "words": None, **captions.STYLE}])
    spec = batch_edit.clean_spec({"captions": "highlight", "ai_intro": "on", "zoom": "slow_in", "quality": "high",
                                  "title_source": "text", "title_text": "he lied", "title_style": "red"})
    proj, made = batch_edit.build_project(spec, db.get("20-01"), folder, CFG)

    assert proj["version"] == 2 and made["file"] == "ai_intro.wav"
    assert "upload:ai_intro.wav" in proj["assets"] and proj["export"]["quality"] == "high"
    voice = next(t for t in proj["tracks"] if t["name"] == "Intro voice")["items"][0]
    assert voice["asset"] == "upload:ai_intro.wav" and voice["start"] == 0 and voice["music"] is False
    first, second = P.main_items(proj)[:2]
    assert first["volume"] < 0.3 and second["volume"] == 1.0                     # clip dips under the narrator
    assert abs((first["out"] - first["in"]) - 3.25) < 0.01 and second["in"] == first["out"]
    assert first["kf"]["zoom"][-1][1] == second["kf"]["zoom"][0][1]              # zoom continues smoothly
    texts = [it for t in proj["tracks"] if t["kind"] in ("text", "caption") for it in t["items"]]
    said = {it["text"] for it in texts}
    assert "Is Gehgeh actually right?" in said and "after it" in said and "inside the intro" not in said
    title = next(it for it in texts if "he lied" in it["text"])
    hook = next(it for it in texts if it["text"] == "Is Gehgeh actually right?")
    assert hook["y"] != title["y"]                                                # not on top of each other
    assert title["box"] and title["box_color"] == "#E5173F"                       # the title design survives

    out = render2.render(proj, folder, out_name="intro_test.mp4")
    assert abs(media.duration(out) - 8) < 0.5 and media.has_audio(out)


@needs_ffmpeg
def test_worker_renders_the_intro_and_falls_back_without_it(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    folder = _clip("21-01", state="TRANSFORMED", seconds=6)
    wav = make_wav(folder / editor2.UPLOADS / "ai_intro.wav", seconds=2.0)
    spec = batch_edit.clean_spec({"ai_intro": "on", "quality": "draft"})
    db.update("21-01", info={"title": "Why I quit", "render_from": "batch", "batch_edit": spec})

    monkeypatch.setattr(intro, "make_intro", lambda job, d, cfg, text="": {
        "text": "Wait for this one", "file": wav.name, "duration": 2.0})
    worker.render_edit(db.get("21-01"), config.load())
    job = db.get("21-01")
    assert (folder / "final.mp4").exists() and job["edit"]["version"] == 2
    assert job["info"]["intro"]["text"] == "Wait for this one" and "intro_error" not in job["info"]

    (folder / "final.mp4").unlink()

    def broken(job, d, cfg, text=""):
        raise intro.VoiceError("every key is busy")

    monkeypatch.setattr(intro, "make_intro", broken)
    worker.render_edit(db.get("21-01"), config.load())
    job = db.get("21-01")
    assert (folder / "final.mp4").exists()                                        # still finished
    assert "every key is busy" in job["info"]["intro_error"] and job["edit"].get("version") != 2


# ------------------------------------------------------------------ routes

def test_intro_preview_and_file_routes(client, monkeypatch):
    _clip("22-01")
    monkeypatch.setattr(intro, "write_hook", lambda item, cfg, creator="", avoid="": "Is gehgeh right?")

    def fake_speak(text, out_dir, speed=1.15):
        out_dir.mkdir(parents=True, exist_ok=True)
        make_wav(out_dir / "ai_intro.wav", 1.0)
        return out_dir / "ai_intro.wav"

    monkeypatch.setattr(intro, "speak", fake_speak)
    res = client.post("/clips/batch-edit/intro-preview", data={"ids": ["22-01"]})
    assert res.status_code == 200 and res.json()["text"] == "Is gehgeh right?" and res.json()["seconds"] > 0
    assert client.get(res.json()["url"].split("?")[0]).status_code == 200
    assert client.get("/intro-preview/secret.wav").status_code == 404
    typed = client.post("/clips/batch-edit/intro-preview", data={"ids": ["22-01"], "intro_text": "My own line"})
    assert typed.json()["text"] == "My own line"
    assert client.post("/clips/batch-edit/intro-preview", data={}).status_code == 400

    def boom(text, out_dir, speed=1.15):
        raise intro.VoiceError("busy")

    monkeypatch.setattr(intro, "speak", boom)
    assert client.post("/clips/batch-edit/intro-preview", data={"ids": ["22-01"]}).status_code == 502


def test_overlay_ideas_are_sorted_spaced_and_kept_in_range(monkeypatch):
    ideas = [intro._Idea(start=20.0, dur=9, label="Late one", search="late", why="x"),
             intro._Idea(start=3.0, dur=3, label="Seyi Tinubu", search="Seyi Tinubu photo", why="names him"),
             intro._Idea(start=4.0, dur=3, label="Too close", search="close", why="x"),
             intro._Idea(start=10.0, dur=3, label="", search="empty", why="x")]
    fake = types.SimpleNamespace(models=types.SimpleNamespace(
        generate_content=lambda **kw: types.SimpleNamespace(parsed=intro._Ideas(ideas=ideas), text="")))
    monkeypatch.setattr(highlight, "make_client", lambda cfg: fake)
    words = [{"text": "Seyi", "start": 3.0, "end": 3.4}]
    out = intro.overlay_ideas(words, 25.0, {"gemini": {"model": "m"}})
    assert [i["label"] for i in out] == ["Seyi Tinubu", "Late one"]              # sorted, spaced, no blank label
    assert out[1]["start"] <= 24.0 and 2.0 <= out[1]["dur"] <= 4.0
    assert intro.overlay_ideas([], 25.0, {"gemini": {"model": "m"}}) == []


def test_overlay_ideas_route_uses_the_trim_offset(client, monkeypatch):
    _clip("23-01", state="READY", seconds=10)
    seen = {}

    def fake_words(job_dir, offset, length):
        seen.update(offset=offset, length=length)
        return [{"text": "hi", "start": 0.0, "end": 0.4}]

    monkeypatch.setattr(intro, "words_for_clip", fake_words)
    monkeypatch.setattr(intro, "overlay_ideas", lambda words, length, cfg, n=4: [
        {"start": 1.0, "dur": 3.0, "label": "Davido", "search": "Davido", "why": "named"}])
    proj = {"tracks": [{"kind": "main", "items": [{"in": 2.0, "out": 8.0, "speed": 1.0}]}]}
    res = client.post("/api/clips/23-01/overlay-ideas", json={"project": proj})
    assert res.status_code == 200 and res.json()["ideas"][0]["label"] == "Davido"
    assert seen == {"offset": 2.0, "length": 6.0}
    assert client.post("/api/clips/nope/overlay-ideas", json={}).status_code == 404


def test_editor_gets_the_image_ideas_panel_and_titles_keep_their_look():
    js = (config.ROOT / "clipper" / "static" / "ed2" / "panels.js").read_text(encoding="utf-8")
    assert "overlay-ideas" in js and "Add image" in js
    e = render.default_edit(5)
    e["texts"] = [{"text": "x", "start": 0, "end": 3, "style": "highlighter", "position": "top", "size": 64,
                   "color": "#111111", "animation": "pop", "accent": "#FFE600", "words": None, "auto": False}]
    t = P.from_v1(e, {"kind": "video", "src": "clip", "name": "c", "duration": 5, "w": 1080, "h": 1920,
                      "audio": True})["tracks"][1]["items"][0]
    assert t["box"] and t["box_color"] == "#FFE600" and t["color"] == "#111111" and t["outline"] == 0
