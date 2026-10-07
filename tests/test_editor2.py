"""The timeline editor: project format (project.py), ffmpeg renderer (render2.py) and the
editor's routes (editor2.py / app.py). Renders use small synthetic clips."""

import json

import pytest
from fastapi.testclient import TestClient

from clipper import app as webapp
from clipper import config, db, editor2, media, project as P, render2, worker
from tests.conftest import make_clip, needs_ffmpeg

CLIP = {"kind": "video", "src": "clip", "name": "clip", "duration": 6.0, "w": 720, "h": 1280, "audio": True}


def proj(**over):
    p = {"assets": {"clip": CLIP},
         "tracks": [{"id": "main", "kind": "main", "items": [{"id": "c1", "asset": "clip", "in": 0, "out": 4}]}]}
    p.update(over)
    return p


# ------------------------------------------------------------------ format

def test_clean_fills_defaults_and_clamps():
    p = P.clean(proj(tracks=[
        {"kind": "main", "items": [{"asset": "clip", "in": -3, "out": 99, "speed": 9, "transition": {"type": "spin"}}]},
        {"kind": "text", "items": [{"text": "  hi  ", "start": 1, "dur": 2, "font": "Comic Sans", "size": 9999,
                                    "color": "red", "anim_in": "explode", "kf": {"x": [[0, 100], [9, 900]]}}]},
    ]), {"clip": CLIP})
    main, text = p["tracks"][0]["items"][0], p["tracks"][1]["items"][0]
    assert main["in"] == 0 and main["out"] == 6 and main["speed"] == 4 and main["transition"]["type"] == "none"
    assert text["text"] == "hi" and text["font"] == "Poppins Black" and text["size"] == 400
    assert text["color"] == "#FFFFFF" and text["anim_in"] == "none"
    # a keyframe past the end is pulled in to the end, keeping where the motion was heading
    assert text["kf"]["x"] == [[0, 100], [2, pytest.approx(277.7778, abs=0.01)]]


def test_clean_refuses_what_cannot_render():
    with pytest.raises(P.ProjectError):
        P.clean({"assets": {}, "tracks": []}, {})
    with pytest.raises(P.ProjectError):  # no clips on the main track
        P.clean(proj(tracks=[{"kind": "main", "items": [{"asset": "missing", "in": 0, "out": 2}]}]), {"clip": CLIP})
    with pytest.raises(P.ProjectError):
        P.clean_asset({"kind": "image", "src": "../../etc/passwd"})


def test_layout_with_speed_and_transition():
    p = P.clean(proj(tracks=[{"kind": "main", "items": [
        {"id": "a", "asset": "clip", "in": 0, "out": 4},
        {"id": "b", "asset": "clip", "in": 2, "out": 6, "speed": 2, "transition": {"type": "fade", "dur": 0.5}}]}]),
        {"clip": CLIP})
    lay = [(it["id"], round(s, 3), round(length, 3), d) for it, s, length, d in P.layout(p)]
    assert lay == [("a", 0.0, 4.0, 0.0), ("b", 3.5, 2.0, 0.5)]
    assert P.duration(p) == 5.5


def test_motion_folds_keyframes_and_animations_together():
    it = {"start": 10, "dur": 2, "x": 540, "y": 900, "rotation": 0, "opacity": 1, "anim_in": "fade", "anim_out": "none",
          "kf": {"x": [[0, 100], [2, 300]]}}
    m = P.motion(it, {"x": 540, "y": 900, "scale": 1, "rotation": 0, "opacity": 1})
    assert P.at(m["x"], 11, 0) == pytest.approx(200)          # halfway between the keys
    assert P.at(m["opacity"], 10, 1) == 0 and P.at(m["opacity"], 10.3, 1) == pytest.approx(1)
    assert P.at(m["y"], 11, 0) == 900


def test_v1_edits_convert_and_back():
    v1 = {"trim": {"start": 1, "end": 5}, "filter": "vivid", "zoom": "slow_in", "fade_in": 0.5, "fade_out": 0,
          "voice_volume": 0.8, "music": {"track": None}, "texts": [
              {"text": "Hi", "start": 0, "end": 2, "style": "classic", "position": "top", "size": 72, "color": "#FFFFFF",
               "animation": "pop", "auto": False}]}
    p = P.clean(P.from_v1(v1, CLIP), {"clip": CLIP})
    main = p["tracks"][0]["items"][0]
    assert (main["in"], main["out"], main["volume"]) == (1, 5, 0.8) and main["kf"]["zoom"][-1][1] == 1.12
    text = next(t for t in p["tracks"] if t["kind"] == "text")["items"][0]
    assert text["size"] == 72 and text["anim_in"] == "pop" and p["look"]["filter"] == "vivid"
    back = P.to_v1(p, 6.0)
    assert back["trim"] == {"start": 1, "end": 5} and back["texts"][0]["text"] == "Hi"


# ------------------------------------------------------------------ renderer

def test_text_events_hide_unrevealed_words_without_boxes():
    it = P.clean(proj(tracks=[{"kind": "main", "items": [{"asset": "clip", "in": 0, "out": 4}]},
                              {"kind": "caption", "items": [{"text": "one two three", "start": 1, "dur": 1.5, "effect": "words",
                                                             "words": [[0, 0.4], [0.5, 0.9], [1.0, 1.4]]}]}]),
                 {"clip": CLIP})["tracks"][1]["items"][0]
    events = render2.text_events(0, it)
    assert len(events) == 3
    assert events[0].endswith(r"one {\alpha&HFF&}two three{\alpha&H00&}")
    assert events[-1].endswith("one two three") and "&HFF&" not in events[-1]


def test_progress_bar_never_scales_to_nothing():
    """ffmpeg's scaler crashes on a picture a pixel or two wide."""
    p = P.clean(proj(assets={"clip": CLIP, "bar": {"kind": "image", "src": "shape/bar", "w": 1080, "h": 14}},
                     tracks=[{"kind": "main", "items": [{"asset": "clip", "in": 0, "out": 4}]},
                             {"kind": "overlay", "items": [{"asset": "bar", "start": 0, "dur": 4, "w": 1080, "scale_mode": "width",
                                                            "kf": {"scale": [[0, 0.001], [4, 1]]}}]}]), {"clip": CLIP})
    _, graph, _ = render2.build(p, config.DATA_DIR, None)
    assert "scale=w='max(8," in graph


@needs_ffmpeg
def test_renders_cuts_transition_text_and_sticker(tmp_path):
    make_clip(tmp_path / "transformed.mp4", seconds=6, size="360x640")
    asset = {**CLIP, "w": 360, "h": 640}
    render2.make_shape(tmp_path, "pill", 200, 60, "#FFFFFF", 30)
    p = P.clean({"assets": {"clip": asset, "pill": {"kind": "image", "src": "shape/pill", "w": 200, "h": 60, "color": "#FFFFFF", "radius": 30}},
                 "tracks": [
                     {"kind": "main", "items": [{"asset": "clip", "in": 0, "out": 3, "kf": {"zoom": [[0, 1], [3, 1.3]]}},
                                                {"asset": "clip", "in": 3, "out": 6, "speed": 1.5,
                                                 "transition": {"type": "slideup", "dur": 0.4}}]},
                     {"kind": "overlay", "items": [{"asset": "pill", "start": 0.5, "dur": 2, "x": 540, "y": 1500, "w": 400,
                                                    "anim_in": "pop", "kf": {"rotation": [[0, -10], [2, 10]]}}]},
                     {"kind": "text", "items": [{"text": "HELLO", "start": 0.2, "dur": 2, "font": "Anton", "anim_in": "slide_up"}]},
                     {"kind": "caption", "items": [{"text": "word by word", "start": 2, "dur": 2, "effect": "highlight"}]},
                 ]}, {"clip": asset})
    out = render2.render(p, tmp_path, out_name="out.mp4")
    assert media.duration(out) == pytest.approx(P.duration(p), abs=0.1)
    assert media.video_size(out) == (1080, 1920) and media.has_audio(out)
    music_free = render2.render(p, tmp_path, out_name="tiktok.mp4", music=False)
    assert media.duration(music_free) == pytest.approx(P.duration(p), abs=0.1)


@needs_ffmpeg
def test_export_size_720p(tmp_path):
    make_clip(tmp_path / "transformed.mp4", seconds=2, size="360x640")
    p = P.clean({**proj(), "export": {"height": 1280, "fps": 24, "quality": "small"},
                 "tracks": [{"kind": "main", "items": [{"asset": "clip", "in": 0, "out": 2}]}]},
                {"clip": {**CLIP, "duration": 2.0}})
    out = render2.render(p, tmp_path, out_name="small.mp4")
    assert media.video_size(out) == (720, 1280)


# ------------------------------------------------------------------ routes

VID, CID = "7400000000000000077", "7400000000000000077-01"


@pytest.fixture
def client(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "MUSIC_DIR", tmp_path / "music")
    monkeypatch.setattr(webapp, "hooks", {"wake_worker": lambda: None})
    db.insert_if_new(VID, "u", state="CLIPPED")
    db.insert_clip(CID, db.get(VID), {"start_s": 0, "end_s": 20})
    db.update(CID, state="READY")
    make_clip(config.job_dir(CID) / "transformed.mp4", seconds=4, size="360x640")
    return TestClient(webapp.app)


@needs_ffmpeg
def test_editor_page_loads_with_the_clip_as_a_project(client):
    page = client.get(f"/jobs/{CID}/editor")
    assert page.status_code == 200 and 'id="editor-data"' in page.text and "ed2/main.js" in page.text
    data = json.loads(page.text.split('id="editor-data" type="application/json">')[1].split("</script>")[0])
    assert data["project"]["tracks"][0]["items"][0]["asset"] == "clip" and data["editable"]
    assert "Anton" in data["catalog"]["fonts"] and data["catalog"]["fonts"]["Anton"]["scale"] == pytest.approx(0.577, abs=0.01)


@needs_ffmpeg
def test_save_then_the_worker_renders_the_project(client):
    data = client.get(f"/jobs/{CID}/editor").text
    project = json.loads(data.split('id="editor-data" type="application/json">')[1].split("</script>")[0])["project"]
    project["tracks"].append({"kind": "text", "items": [{"text": "Saved!", "start": 0.5, "dur": 2}]})
    r = client.post(f"/api/clips/{CID}/project", json=project)
    assert r.status_code == 200 and r.json()["redirect"] == f"/jobs/{CID}"
    job = db.get(CID)
    assert job["state"] == "TRANSFORMED" and job["edit"]["version"] == 2
    worker.render_edit(job, config.load())
    final = config.job_dir(CID) / "final.mp4"
    assert final.exists() and media.duration(final) == pytest.approx(4, abs=0.1)
    # the clip page describes it (once it's ready again), and older tools still get a format-1 view
    db.update(CID, state="READY")
    assert "1 text" in client.get(f"/jobs/{CID}").text
    assert worker.current_edit(db.get(CID), config.load())["texts"][0]["text"] == "Saved!"


@needs_ffmpeg
def test_drafts_autosave_and_come_back(client):
    page = client.get(f"/jobs/{CID}/editor").text
    project = json.loads(page.split('id="editor-data" type="application/json">')[1].split("</script>")[0])["project"]
    project["look"]["filter"] = "bw"
    assert client.post(f"/api/clips/{CID}/draft", json=project).status_code == 200
    again = client.get(f"/jobs/{CID}/editor").text
    assert '"filter": "bw"' in again or '"filter":"bw"' in again
    client.post(f"/api/clips/{CID}/discard_draft")
    assert '"filter": "bw"' not in client.get(f"/jobs/{CID}/editor").text


@needs_ffmpeg
def test_uploads_are_kept_in_the_clip_folder_and_files_outside_are_refused(client):
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGBA", (40, 20), (255, 0, 0, 255)).save(buf, "PNG")
    r = client.post(f"/api/clips/{CID}/upload", files={"file": ("../../evil.png", buf.getvalue(), "image/png")})
    assert r.status_code == 200
    aid = r.json()["id"]
    assert aid == "upload:evil.png" and (config.job_dir(CID) / "uploads" / "evil.png").exists()
    assert client.get(f"/api/clips/{CID}/asset/{aid}").status_code == 200
    bad = client.post(f"/api/clips/{CID}/upload", files={"file": ("run.exe", b"MZ", "application/octet-stream")})
    assert bad.status_code == 400
    # a project may not point at files the server didn't list
    page = client.get(f"/jobs/{CID}/editor").text
    project = json.loads(page.split('id="editor-data" type="application/json">')[1].split("</script>")[0])["project"]
    project["assets"]["sneaky"] = {"kind": "video", "src": "upload/../../../secret.mp4"}
    project["tracks"].append({"kind": "overlay", "items": [{"asset": "sneaky", "start": 0, "dur": 1}]})
    saved = client.post(f"/api/clips/{CID}/draft", json=project)
    assert saved.status_code == 200
    draft = json.loads((config.job_dir(CID) / editor2.DRAFT).read_text(encoding="utf-8"))["project"]
    assert "sneaky" not in draft["assets"] and not any(t["kind"] == "overlay" and t["items"] for t in draft["tracks"])


@needs_ffmpeg
def test_filmstrip_and_waveform(client):
    strip = client.get(f"/api/clips/{CID}/strip/clip")
    assert strip.status_code == 200 and strip.headers["content-type"] == "image/jpeg" and int(strip.headers["x-count"]) >= 4
    peaks = client.get(f"/api/clips/{CID}/peaks/clip").json()
    assert peaks["per_second"] == 40 and len(peaks["peaks"]) >= 150 and max(peaks["peaks"]) == 1.0


def test_captions_land_where_the_words_are_after_cuts(tmp_path, monkeypatch):
    from clipper import captions
    words = [{"text": w, "start": s, "end": s + 0.3} for w, s in (("one", 0.5), ("two", 1.0), ("three", 3.2), ("four", 3.6))]
    monkeypatch.setattr(captions, "transcribe", lambda job_dir: words)
    p = P.clean(proj(tracks=[{"kind": "main", "items": [
        {"asset": "clip", "in": 0, "out": 2}, {"asset": "clip", "in": 3, "out": 5, "speed": 2}]}]), {"clip": CLIP})
    items = editor2.caption_items(tmp_path, p, {"font": "Anton", "effect": "highlight"})
    starts = {it["text"]: it["start"] for it in items}
    assert starts["one two"] == 0.5                            # first cut: unchanged
    assert starts["three four"] == pytest.approx(2.1)         # second cut starts at 2 s, 2x speed: 2 + 0.2/2
    assert all(it["auto"] and it["font"] == "Anton" for it in items)


@needs_ffmpeg
def test_a_growing_layer_with_layers_on_top_renders(tmp_path):
    """Regression: a picture whose size changes every frame (a growing progress bar, a pop-in)
    with anything drawn after it used to crash ffmpeg with heap corruption."""
    make_clip(tmp_path / "transformed.mp4", seconds=3, size="360x640")
    asset = {**CLIP, "duration": 3.0, "w": 360, "h": 640}
    p = P.clean({"assets": {"clip": asset, "bar": {"kind": "image", "src": "shape/bar", "w": 1080, "h": 14},
                            "dot": {"kind": "image", "src": "shape/dot", "w": 200, "h": 200, "radius": 100}},
                 "tracks": [
                     {"kind": "main", "items": [{"asset": "clip", "in": 0, "out": 3}]},
                     {"kind": "overlay", "items": [{"asset": "bar", "start": 0, "dur": 3, "w": 1080, "y": 1900,
                                                    "scale_mode": "width", "kf": {"scale": [[0, 0.01], [3, 1]]}}]},
                     {"kind": "overlay", "items": [{"asset": "dot", "start": 0.5, "dur": 2, "w": 260, "anim_in": "pop"}]},
                     {"kind": "text", "items": [{"text": "on top", "start": 0, "dur": 3}]},
                 ]}, {"clip": asset})
    out = render2.render(p, tmp_path, out_name="stack.mp4")
    assert media.duration(out) == pytest.approx(3, abs=0.1)


def test_ai_helper_answer_is_checked_like_any_save(tmp_path, monkeypatch):
    """Gemini edits the project; its answer can't add files and is validated."""
    from clipper import gemini

    p = P.clean(proj(), {"clip": CLIP})
    answer = json.loads(json.dumps(p))
    answer["look"]["filter"] = "bw"
    answer["assets"]["evil"] = {"kind": "video", "src": "upload/x.mp4"}
    answer["tracks"].append({"kind": "text", "items": [{"text": "AI title", "start": 0, "dur": 2, "font": "Anton"}]})

    class Resp:
        text = json.dumps(answer)

    class Fake:
        def __init__(self, cfg):
            self.models = self

        def generate_content(self, **kw):
            assert "Make it black and white" in kw["contents"]
            return Resp()

    monkeypatch.setattr(gemini, "make_client", Fake)
    monkeypatch.setattr(editor2, "known_assets", lambda job_dir: {"clip": CLIP})
    out = editor2.assist(p, "Make it black and white", tmp_path, config.load())
    assert out["look"]["filter"] == "bw" and "evil" not in out["assets"]
    assert any(it.get("text") == "AI title" for t in out["tracks"] for it in t["items"])
