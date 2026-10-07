import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clipper import db  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not on PATH")


@pytest.fixture
def fresh_db(tmp_path):
    db.init(tmp_path / "test.db")
    yield db
    db.init(tmp_path / "unused.db")


def make_clip(path: Path, seconds: float = 6, tone: bool = True, size: str = "720x1280") -> Path:
    """Synthetic test video: colour bars + a beep (or no audio)."""
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=30:duration={seconds}"]
    if tone:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", str(path)]
    subprocess.run(cmd, check=True)
    return path


def make_tone(path: Path, seconds: float = 3, freq: int = 220) -> Path:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"sine=frequency={freq}:duration={seconds}", str(path)], check=True)
    return path


@pytest.fixture(autouse=True)
def no_real_studio(tmp_path, monkeypatch):
    """Tests never start HyperFrames (a Studio server or a render) or touch its real state file;
    a test that needs Studio replaces open_studio / render_project itself."""
    from clipper import studio

    def blocked(*args, **kwargs):
        raise studio.StudioError("HyperFrames isn't run in tests")
    monkeypatch.setattr(studio, "_run", blocked)
    monkeypatch.setattr(studio, "STATE_PATH", tmp_path / "studio_server.json")
