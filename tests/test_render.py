"""The editor's render engine: edit validation, captions, and real ffmpeg renders."""

import xml.etree.ElementTree as ET

import pytest

from clipper import media, render, shotcut
from tests.conftest import make_clip, make_tone, needs_ffmpeg

TRACKS = {"beat.mp3"}


def _edit(**over):
    edit = render.default_edit(10, "beat.mp3", 0.3)
    edit.update(over)
    return edit


# ------------------------------------------------------------- validation

def test_clean_edit_clamps_and_whitelists():
    raw = _edit(trim={"start": -5, "end": 99}, filter="hacker", zoom="spin", fade_in=50, voice_volume="loud",
                texts=[{"text": "  hi  ", "start": -1, "end": 99, "style": "comic", "position": "left",
                        "size": 9999, "color": "red", "animation": "spin"},
                       {"text": "   ", "start": 0, "end": 2},
                       "not a dict"])
    edit = render.clean_edit(raw, 10, TRACKS)
    assert edit["trim"] == {"start": 0.0, "end": 10.0}
    assert edit["filter"] == "none" and edit["zoom"] == "none"
    assert edit["fade_in"] == 3 and edit["voice_volume"] == 1.0
    assert len(edit["texts"]) == 1
    t = edit["texts"][0]
    assert t == {"text": "hi", "start": 0.0, "end": 10.0, "style": "classic", "position": "top",
                 "size": 160, "color": "#FFFFFF", "animation": "pop", "accent": "#FFE600", "words": None,
                 "auto": False}


def test_clean_edit_rejects_unknown_music_and_too_short_clip():
    with pytest.raises(render.EditError):
        render.clean_edit(_edit(music={"track": "../../secret.mp3"}), 10, TRACKS)
    with pytest.raises(render.EditError):
        render.clean_edit(_edit(trim={"start": 5, "end": 5.5}), 10, TRACKS)
    with pytest.raises(render.EditError):
        render.clean_edit(["not", "an", "object"], 10, TRACKS)


def test_text_limit():
    texts = [{"text": f"t{i}", "start": 0, "end": 1} for i in range(render.MAX_TEXTS + 10)]
    assert len(render.clean_edit(_edit(texts=texts), 10, TRACKS)["texts"]) == render.MAX_TEXTS


# ------------------------------------------------------------- captions

def test_ass_escapes_override_braces_and_newlines():
    ass = render.build_ass([{"text": "a {\\b1} b\nnext", "start": 0, "end": 1.5, "style": "box",
                             "position": "bottom", "size": 60, "color": "#FFE600", "animation": "fade"}])
    line = [l for l in ass.splitlines() if l.startswith("Dialogue")][0]
    assert line.endswith("a (/b1) b\\Nnext")
    assert "0:00:00.00,0:00:01.50,box" in line
    assert "\\an2" in line and "\\1c&H00E6FF&" in line and "\\fad(250,250)" in line


def test_ass_colour_is_bgr():
    assert render._ass_color("#112233") == "&H00332211"


# ------------------------------------------------------------- command

def test_command_has_trim_zoom_filter_and_ducked_music(tmp_path):
    edit = render.clean_edit(_edit(trim={"start": 2, "end": 8}, zoom="slow_in", filter="vintage",
                                   fade_out=1), 10, TRACKS)
    cmd = render.build_command(edit, tmp_path / "base.mp4", tmp_path / "out.mp4",
                               tmp_path / "beat.mp3", "edit.ass")
    joined = " ".join(cmd)
    assert cmd[cmd.index("-ss") + 1] == "2.000" and cmd[cmd.index("-t") + 1] == "6.000"
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "zoompan=" in graph and "curves=preset=vintage" in graph and "subtitles=edit.ass" in graph
    assert "sidechaincompress" in graph and "fade=t=out:st=5.000" in graph
    assert "-stream_loop" in joined


def test_command_without_music_or_audio(tmp_path):
    edit = render.clean_edit(_edit(music={"track": None}), 10, TRACKS)
    cmd = render.build_command(edit, tmp_path / "b.mp4", tmp_path / "o.mp4", None, None, base_has_audio=False)
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "anullsrc" in " ".join(cmd) and "sidechaincompress" not in graph and "[1:a]" in graph


@needs_ffmpeg
def test_render_end_to_end(tmp_path):
    make_clip(tmp_path / "transformed.mp4", seconds=6, size="720x1280")
    make_tone(tmp_path / "beat.mp3", seconds=2)
    edit = render.clean_edit(_edit(trim={"start": 1, "end": 5}, zoom="slow_out", filter="warm", fade_in=0.5,
                                   texts=[{"text": "WAIT 😂", "start": 0, "end": 2}]), 6, TRACKS)
    out = render.render(edit, tmp_path, music_dir=tmp_path)
    assert media.video_size(out) == (1080, 1920)
    assert media.duration(out) == pytest.approx(4, abs=0.15)
    assert media.has_audio(out)
    assert (tmp_path / "edit.ass").exists()
    assert not list(tmp_path.glob("render_tmp_*")), "temp file left behind"


@needs_ffmpeg
def test_failed_render_keeps_previous_final(tmp_path):
    make_clip(tmp_path / "transformed.mp4", seconds=3)
    (tmp_path / "final.mp4").write_bytes(b"previous")
    edit = render.clean_edit(render.default_edit(3), 3, set())
    (tmp_path / "transformed.mp4").write_bytes(b"corrupt")  # ffmpeg will fail
    with pytest.raises(Exception):
        render.render(edit, tmp_path)
    assert (tmp_path / "final.mp4").read_bytes() == b"previous"


# ------------------------------------------------------------- shotcut

def test_shotcut_project_is_valid_mlt_with_trim_and_looped_music(tmp_path):
    edit = render.clean_edit(_edit(trim={"start": 2, "end": 9}, music={"track": "beat.mp3", "volume": 0.5,
                                                                       "offset": 1}), 10, TRACKS)
    xml = shotcut.build_project(tmp_path / "transformed.mp4", 10, edit, tmp_path / "beat.mp3", music_length=3)
    root = ET.fromstring(xml)
    profile = root.find("profile")
    assert (profile.get("width"), profile.get("height")) == ("1080", "1920")
    video_entry = root.find("playlist[@id='playlist0']/entry")
    assert (video_entry.get("in"), video_entry.get("out")) == ("60", "269")  # 2 s..9 s at 30 fps
    music_entries = root.findall("playlist[@id='playlist1']/entry")
    # 7 s of clip from a 3 s song starting 1 s in: 2 s + 3 s + 2 s, exactly 210 frames
    spans = [(int(e.get("in")), int(e.get("out"))) for e in music_entries]
    assert spans == [(30, 89), (0, 89), (0, 59)]
    assert sum(b - a + 1 for a, b in spans) == 210
    level = root.find(".//filter[@id='music_gain']/property[@name='level']").text
    assert float(level) == pytest.approx(-6.02, abs=0.01)


def test_shotcut_skips_music_shorter_than_a_frame(tmp_path):
    edit = render.clean_edit(_edit(), 10, TRACKS)
    xml = shotcut.build_project(tmp_path / "t.mp4", 10, edit, tmp_path / "beat.mp3", music_length=0.02)
    assert ET.fromstring(xml).find("playlist[@id='playlist1']") is None


@needs_ffmpeg
def test_zoom_is_centred(tmp_path):
    """The render must zoom on the middle, like the preview, not drift to a corner."""
    import subprocess
    base = tmp_path / "transformed.mp4"
    media.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
               "color=white:s=1080x1920:d=4:r=30,drawbox=x=0:y=0:w=iw:h=ih:color=black:t=40",
               "-f", "lavfi", "-i", "sine=duration=4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", base])
    edit = render.clean_edit(render.default_edit(4) | {"zoom": "slow_in"}, 4, set())
    out = render.render(edit, tmp_path)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", "1.0", "-i", str(out), "-frames:v", "1", "-vf",
                          "crop=iw:1:0:960,format=gray", "-f", "rawvideo", "-"], capture_output=True).stdout
    left = next(i for i, b in enumerate(raw) if b > 128)
    right = next(i for i, b in enumerate(reversed(raw)) if b > 128)
    assert abs(left - right) <= 6, (left, right)


@needs_ffmpeg
@pytest.mark.parametrize("voice", [1.0, 0.0])
def test_short_silent_clip_renders(tmp_path, voice):
    """loudnorm turns <3 s of pure silence into NaN and the AAC encoder aborts."""
    make_clip(tmp_path / "transformed.mp4", seconds=3, tone=(voice == 0.0))
    edit = render.clean_edit(render.default_edit(3) | {"trim": {"start": 0, "end": 1.5}, "voice_volume": voice},
                             3, set())
    out = render.render(edit, tmp_path)
    assert media.duration(out) == pytest.approx(1.5, abs=0.15)


def test_fit_edit_handles_removed_music_and_shorter_clip():
    edit = render.clean_edit(_edit(trim={"start": 6, "end": 10}), 10, TRACKS)
    fitted = render.fit_edit(edit, 5, set())  # clip is now 5 s, beat.mp3 deleted
    assert fitted["music"]["track"] is None
    assert fitted["trim"] == {"start": 4.0, "end": 5.0}


def test_text_trimmed_away_is_dropped_not_squashed():
    edit = render.clean_edit(_edit(texts=[{"text": "gone", "start": -5, "end": -1},
                                          {"text": "late", "start": 12, "end": 14},
                                          {"text": "partly", "start": -1, "end": 2}]), 10, TRACKS)
    assert [(t["text"], t["start"], t["end"]) for t in edit["texts"]] == [("partly", 0.0, 2.0)]


def test_shotcut_not_installed_gives_install_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(shotcut, "SEARCH", [tmp_path / "nope"])
    monkeypatch.setattr(shotcut.shutil, "which", lambda name: None)
    assert shotcut.find({}) is None
    with pytest.raises(shotcut.ShotcutError, match="winget"):
        shotcut.open_project(tmp_path / "p.mlt", {})
