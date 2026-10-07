"""HyperFrames Studio as the editor: the Clipper edit -> Studio project conversion and the
routes. Nothing here starts Studio or renders (that needs Node and takes minutes)."""

import re

import pytest
from fastapi.testclient import TestClient

from clipper import app as webapp
from clipper import config, db, render, studio, worker
from tests.conftest import make_clip, needs_ffmpeg


def _edit(**changes):
    edit = render.default_edit(20.0, "chill.mp3", 0.2)
    edit.update(changes)
    return edit


def test_trim_look_and_voice_volume_carry_over():
    edit = _edit(trim={"start": 3.0, "end": 15.5}, filter="vivid", voice_volume=0.8)
    page = studio.build_html(edit, 20.0, [], "clip-x")
    video = re.search(r"<video[^>]*>", page).group(0)
    assert 'data-media-start="3.000"' in video and 'data-duration="12.5"' in video
    assert 'data-volume="0.8"' in video and "saturate(1.35)" in video
    assert 'data-duration="12.5"' in re.search(r'id="root"[^>]*>', page).group(0)
    assert 'window.__timelines["main"] = tl' in page


def test_music_is_its_own_track_and_ducks_under_the_voice():
    edit = _edit()
    page = studio.build_html(edit, 20.0, [(2.0, 6.0)], "clip-x")
    audio = re.search(r"<audio[^>]*>", page).group(0)
    assert 'data-clipper="music"' in audio and 'src="assets/music/chill.mp3"' in audio
    assert 'data-track-index="10"' in audio
    lane = studio._music_lane(edit, 20.0, [(2.0, 6.0)])["lanes"][0]["points"]
    level = {p["t"]: p["v"] for p in lane}
    assert level[0.0] == 0 and level[20.0] == 0          # fades in and out
    assert level[2.0] == pytest.approx(0.2 * 0.35)       # dips while he talks
    assert level[6.3] == pytest.approx(0.2)               # and comes back after


def test_tiktok_version_drops_only_the_music():
    page = studio.build_html(_edit(), 20.0, [], "clip-x")
    no_music = studio.without_music(page)
    assert "<audio" not in no_music and "<video" in no_music
    assert studio.without_music(studio.build_html(_edit(music={**_edit()["music"], "track": None}), 20.0, [], "x")) \
        .count("<video") == 1


def test_texts_keep_timing_style_and_are_escaped():
    edit = _edit(texts=[{"text": "Watch <this> & that", "start": 1.0, "end": 4.0, "style": "yellow",
                         "position": "bottom", "size": 80, "color": "#FFE600", "animation": "pop"}],
                 zoom="slow_in", fade_in=0.5)
    page = studio.build_html(edit, 20.0, [], "clip-x")
    div = re.search(r'<div id="text-1"[^>]*>[^<]*</div>', page).group(0)
    assert 'data-start="1.000"' in div and 'data-duration="3.0"' in div and 'data-track-index="1"' in div
    assert "Watch &lt;this&gt; &amp; that" in div and "#FFE600" in div and "bottom: 420px" in div
    assert 'tl.from("#text-1"' in page and "scale: 1.12" in page and 'tl.from("#a-roll", {opacity: 0' in page


@needs_ffmpeg
def test_project_is_written_once_and_your_studio_work_is_kept(tmp_path, monkeypatch):
    job_dir = tmp_path / "7400000000000000001-01"
    job_dir.mkdir()
    make_clip(job_dir / "transformed.mp4", seconds=3)
    monkeypatch.setattr(config, "MUSIC_DIR", tmp_path / "no-music")
    proj = studio.write_project(job_dir, render.default_edit(3.0))
    assert (proj / "clip.mp4").exists() and (proj / "hyperframes.json").exists()
    assert proj.name == "clip-7400000000000000001-01"
    (proj / "index.html").write_text("my studio edit", encoding="utf-8")
    studio.write_project(job_dir, render.default_edit(3.0))
    assert (proj / "index.html").read_text(encoding="utf-8") == "my studio edit"   # kept
    studio.write_project(job_dir, render.default_edit(3.0), overwrite=True)        # start over
    assert "data-composition-id" in (proj / "index.html").read_text(encoding="utf-8")


@pytest.fixture
def client(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")  # never the real data/jobs
    monkeypatch.setattr(webapp, "hooks", {"wake_worker": lambda: None})
    return TestClient(webapp.app)


@needs_ffmpeg
def test_use_this_version_makes_studio_the_source_of_the_final_video(client, fresh_db, monkeypatch):
    vid, cid = "7400000000000000002", "7400000000000000002-01"
    db.insert_if_new(vid, "u", state="CLIPPED")
    db.insert_clip(cid, db.get(vid), {"start_s": 0, "end_s": 20})
    db.update(cid, state="READY")
    job_dir = config.job_dir(cid)
    make_clip(job_dir / "transformed.mp4", seconds=3)
    assert client.post(f"/jobs/{cid}/studio/use").status_code == 409   # never opened
    monkeypatch.setattr(studio, "open_studio", lambda cfg, proj: f"http://localhost:3020/#project/{proj.name}")
    page = client.get(f"/jobs/{cid}/studio").text
    assert 'iframe class="studio"' in page and f"#project/clip-{cid}" in page
    client.post(f"/jobs/{cid}/studio/use")
    job = db.get(cid)
    assert job["state"] == "TRANSFORMED" and job["info"]["render_from"] == "studio"
    rendered = []
    monkeypatch.setattr(studio, "render_project", lambda d, **kw: rendered.append(kw))
    worker.render_edit(job, config.load())
    assert rendered == [{}]


@needs_ffmpeg
def test_studio_not_starting_offers_the_simple_editor(client, fresh_db, monkeypatch, tmp_path):
    vid, cid = "7400000000000000003", "7400000000000000003-01"
    db.insert_if_new(vid, "u", state="CLIPPED")
    db.insert_clip(cid, db.get(vid), {"start_s": 0, "end_s": 20})
    make_clip(config.job_dir(cid) / "transformed.mp4", seconds=2)
    monkeypatch.setattr(studio, "write_project", lambda *a, **k: tmp_path)

    def broken(cfg, proj):
        raise studio.StudioError("Node.js isn't installed")
    monkeypatch.setattr(studio, "open_studio", broken)
    page = client.get(f"/jobs/{cid}/studio").text
    assert "Node.js isn" in page and f"/jobs/{cid}/edit/simple" in page


def test_every_style_survives_html_parsing():
    """Quotes inside a style="..." attribute would cut it short (the font size was lost once)."""
    from html.parser import HTMLParser

    styles = {}

    class Grab(HTMLParser):
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if attrs.get("id"):
                styles[attrs["id"]] = attrs.get("style") or ""

    edit = _edit(texts=[{"text": "Hi", "start": 0, "end": 2, "style": style, "position": "top", "size": 78,
                         "color": "#FFFFFF", "animation": "none"} for style in render.TEXT_STYLES],
                 filter="cinematic")
    Grab().feed(studio.build_html(edit, 20.0, [], "clip-x"))
    for i in range(1, len(render.TEXT_STYLES) + 1):
        assert "font-size: 78px" in styles[f"text-{i}"] and "font-family:" in styles[f"text-{i}"]
    assert "object-fit: cover" in styles["a-roll"] and "filter:" in styles["a-roll"]
