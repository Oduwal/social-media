import pytest

from clipper import cutter, media, music
from tests.conftest import make_clip, needs_ffmpeg

pytestmark = needs_ffmpeg


def test_render_cut_is_9x16_with_audio(tmp_path):
    src = make_clip(tmp_path / "src.mp4", seconds=8, size="1280x720")
    out = cutter.render(src, tmp_path / "cut.mp4", 2, 6)
    assert media.video_size(out) == (1080, 1920)
    assert media.has_audio(out)
    assert media.duration(out) == pytest.approx(4, abs=0.15)


def test_render_adds_silent_track_when_source_has_none(tmp_path):
    src = make_clip(tmp_path / "src.mp4", seconds=4, tone=False)
    out = cutter.render(src, tmp_path / "cut.mp4", 0, 3)
    assert media.has_audio(out)


def test_render_pads_audio_that_ends_before_the_video(tmp_path):
    src = tmp_path / "short_sound.mp4"
    media.run(["ffmpeg", "-y", "-loglevel", "error",
               "-f", "lavfi", "-i", "testsrc=size=720x1280:rate=30:duration=10",
               "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", src])
    out = cutter.render(src, tmp_path / "cut.mp4", 1, 9)
    audio = [s for s in media.probe(out)["streams"] if s["codec_type"] == "audio"][0]
    assert float(audio["duration"]) == pytest.approx(8, abs=0.15)


def test_silences_detects_gap(tmp_path):
    # 2 s tone, 1 s silence, 2 s tone
    audio = tmp_path / "gap.wav"
    media.run(["ffmpeg", "-y", "-loglevel", "error",
               "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
               "-f", "lavfi", "-i", "anullsrc=sample_rate=44100:channel_layout=mono",
               "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
               "-filter_complex", "[1]atrim=duration=1[s];[0][s][2]concat=n=3:v=0:a=1", audio])
    gaps = media.silences(audio)
    assert len(gaps) == 1
    assert gaps[0][0] == pytest.approx(2, abs=0.1)
    assert gaps[0][1] == pytest.approx(3, abs=0.1)


def test_silences_on_silent_video_is_empty(tmp_path):
    clip = make_clip(tmp_path / "src.mp4", seconds=2, tone=False)
    assert media.silences(clip) == []


def test_library_moods_and_path_safety(tmp_path):
    (tmp_path / "a.mp3").write_bytes(b"x")
    (tmp_path / "b.mp3").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("ignore me")
    (tmp_path / "library.json").write_text('{"b.mp3": {"mood": "hype", "title": "Big Drums"}}')
    tracks = music.list_tracks(tmp_path)
    assert [t["file"] for t in tracks] == ["a.mp3", "b.mp3"]
    assert music.pick_for_mood("HYPE", tmp_path) == "b.mp3"
    assert music.pick_for_mood("sad", tmp_path) == "a.mp3"
    with pytest.raises(FileNotFoundError):
        music.track_path("../secret.mp3", tmp_path)
