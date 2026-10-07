"""The adapter must give every job its own work folder, so the user's script
never reuses another video's cached segments. No Omni calls are made here."""

import shutil
from pathlib import Path

import pytest

from clipper import media, transform_adapter
from tests.conftest import make_clip, needs_ffmpeg

pytestmark = needs_ffmpeg

REAL_SCRIPT = Path(r"C:\Users\ODUWA\Pictures\transform_video.py")

STUB = '''
import shutil, subprocess
from pathlib import Path
SCRIPT_DIR = Path(__file__).resolve().parent
WORK_PATH = SCRIPT_DIR / "albino_work"
OUTPUT_PATH = SCRIPT_DIR / "albino_output"
EDIT_INSTRUCTION_VARIANTS = {"albinism": "x", "recolor": "y"}
EDIT_VARIANT = "albinism"
EDIT_INSTRUCTION = "x"
FAIL = set()
def make_client():
    return None
def split_video(video_path):
    d = WORK_PATH / "segments"; d.mkdir(parents=True, exist_ok=True)
    out = d / "segment_001.mp4"
    if not out.exists():
        shutil.copyfile(video_path, out)
    return [out]
def edit_segment(client, segment_path, index, verbose):
    if index in FAIL:
        raise RuntimeError("refused")
    d = WORK_PATH / "edited"; d.mkdir(parents=True, exist_ok=True)
    out = d / f"edited_{index:03d}_{EDIT_VARIANT}.mp4"
    shutil.copyfile(segment_path, out)
    return out
def reassemble(clips, output_name):
    OUTPUT_PATH.mkdir(exist_ok=True)
    shutil.copyfile(clips[0], OUTPUT_PATH / output_name)
'''


def _cfg(script, variant="albinism", enabled=True):
    return {"transform": {"enabled": enabled, "script_path": str(script), "variant": variant}}


@pytest.fixture
def stub(tmp_path):
    script_dir = tmp_path / "script"
    script_dir.mkdir()
    script = script_dir / "transform_video.py"
    script.write_text(STUB)
    yield script
    transform_adapter._modules.clear()


def test_each_job_gets_its_own_work_dir(tmp_path, stub):
    job_a, job_b = tmp_path / "a", tmp_path / "b"
    job_a.mkdir(); job_b.mkdir()
    clip_a = make_clip(job_a / "highlight.mp4", seconds=2)
    clip_b = make_clip(job_b / "highlight.mp4", seconds=3)

    transform_adapter.run(clip_a, job_a, _cfg(stub))
    transform_adapter.run(clip_b, job_b, _cfg(stub))

    assert not (stub.parent / "albino_work").exists(), "wrote into the script's own shared folder"
    assert media.duration(job_b / "transformed.mp4") == pytest.approx(3, abs=0.2)
    assert (job_a / "transform_work" / "segments" / "segment_001.mp4").exists()


def test_failed_segments_are_reported(tmp_path, stub):
    mod = transform_adapter.load_script(stub)
    mod.FAIL.add(1)
    clip = make_clip(tmp_path / "highlight.mp4", seconds=2)
    info = transform_adapter.run(clip, tmp_path, _cfg(stub))
    assert info["fallback_segments"] == [1]
    assert info["refused"] is True
    assert (tmp_path / "transformed.mp4").exists()


def test_disabled_transform_copies_clip(tmp_path, stub):
    clip = make_clip(tmp_path / "highlight.mp4", seconds=1)
    info = transform_adapter.run(clip, tmp_path, _cfg(stub, enabled=False))
    assert info["enabled"] is False
    assert (tmp_path / "transformed.mp4").read_bytes() == clip.read_bytes()


def test_unknown_variant_is_rejected(tmp_path, stub):
    clip = make_clip(tmp_path / "highlight.mp4", seconds=1)
    with pytest.raises(ValueError):
        transform_adapter.run(clip, tmp_path, _cfg(stub, variant="nope"))


def _split_into(mod, video_path, bounds):
    """Replacement split_video: cut the clip at the given (start, length) pairs."""
    d = mod.WORK_PATH / "segments"
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for i, (start, length) in enumerate(bounds, start=1):
        p = d / f"segment_{i:03d}.mp4"
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(start), "-i", video_path]
        if length:
            cmd += ["-t", str(length)]
        media.run(cmd + [p])
        out.append(p)
    return out


def test_tiny_last_segment_is_not_sent_for_editing(tmp_path, stub):
    mod = transform_adapter.load_script(stub)
    edited = []
    real_edit = mod.edit_segment

    def edit(client, seg, index, verbose):
        edited.append(index)
        return real_edit(client, seg, index, verbose)

    mod.split_video = lambda video: _split_into(mod, video, [(0, 2), (2, None)])
    mod.edit_segment = edit
    clip = make_clip(tmp_path / "highlight.mp4", seconds=2.4)
    info = transform_adapter.run(clip, tmp_path, _cfg(stub))
    assert edited == [1]
    assert info["skipped_tiny_segments"] == [2] and info["refused"] is False


def test_small_drifts_that_add_up_switch_off_original_audio(tmp_path, stub):
    mod = transform_adapter.load_script(stub)

    def edit(client, seg, index, verbose):  # every edit comes back 0.2 s longer
        out = mod.WORK_PATH / f"e{index}.mp4"
        make_clip(out, seconds=media.duration(seg) + 0.2)
        return out

    mod.split_video = lambda video: _split_into(mod, video, [(0, 2), (2, 2), (4, 2)])
    mod.edit_segment = edit
    clip = make_clip(tmp_path / "highlight.mp4", seconds=6)
    info = transform_adapter.run(clip, tmp_path, _cfg(stub))
    assert info["original_audio"] is False  # 0.2 s per seam = 0.6 s off by the end
    assert info["max_segment_drift_s"] == pytest.approx(0.6, abs=0.1)


def test_truncated_cached_segment_is_resplit(tmp_path, stub):
    clip = make_clip(tmp_path / "highlight.mp4", seconds=3)
    seg_dir = tmp_path / "transform_work" / "segments"
    seg_dir.mkdir(parents=True)
    make_clip(seg_dir / "segment_001.mp4", seconds=1)  # left behind by a killed run
    transform_adapter.run(clip, tmp_path, _cfg(stub))
    assert media.duration(seg_dir / "segment_001.mp4") == pytest.approx(3, abs=0.2)


@pytest.mark.skipif(not REAL_SCRIPT.exists(), reason="user's transform script not present")
def test_real_script_split_and_reassemble_with_edit_stubbed(tmp_path, monkeypatch):
    """Runs the user's REAL split_video/reassemble; only the paid Omni call is replaced."""
    try:
        mod = transform_adapter.load_script(REAL_SCRIPT)
    except Exception as err:  # noqa: BLE001
        pytest.skip(f"real script not importable here: {err}")

    def fake_edit(client, segment_path, index, verbose):
        out = mod.WORK_PATH / "edited" / f"edited_{index:03d}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(segment_path, out)
        return out

    monkeypatch.setattr(mod, "edit_segment", fake_edit)
    monkeypatch.setattr(mod, "make_client", lambda: None)
    shared_work = REAL_SCRIPT.parent / "albino_work" / "segments" / "segment_003.mp4"
    existed_before = shared_work.exists()

    clip = make_clip(tmp_path / "highlight.mp4", seconds=23)
    info = transform_adapter.run(clip, tmp_path, _cfg(REAL_SCRIPT))

    assert info["segments"] == 3 and info["fallback_segments"] == []
    assert info["original_audio"] is True
    assert media.has_audio(tmp_path / "transformed.mp4")
    assert media.duration(tmp_path / "transformed.mp4") == pytest.approx(23, abs=0.5)
    assert len(list((tmp_path / "transform_work" / "segments").glob("*.mp4"))) == 3
    assert shared_work.exists() == existed_before, "touched the script's shared albino_work folder"
    transform_adapter._modules.clear()
