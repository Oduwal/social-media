"""Thin ffmpeg/ffprobe helpers shared by the pipeline steps."""

import json
import re
import subprocess
import time
from pathlib import Path


class MediaError(RuntimeError):
    pass


def run(cmd: list, timeout: float = 1800, cwd: Path = None) -> subprocess.CompletedProcess:
    result = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout, cwd=cwd)
    if result.returncode != 0:
        raise MediaError(f"{Path(str(cmd[0])).name} failed ({result.returncode}): "
                         f"{result.stderr.strip()[-1500:]}")
    return result


def replace(tmp: Path, dst: Path, wait: float = 45.0) -> Path:
    """tmp becomes dst. On Windows the swap is refused while another program (a video player, a
    browser tab) has dst open, usually only for a moment, so it's retried for up to `wait` seconds."""
    deadline = time.monotonic() + wait
    while True:
        try:
            tmp.replace(dst)
            return dst
        except PermissionError as err:
            if time.monotonic() >= deadline:
                raise MediaError(f"{dst.name} is open in another program (a video player or a browser "
                                 f"tab?). Close it and press Retry.") from err
            time.sleep(0.5)


def probe(path: Path) -> dict:
    out = run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", path])
    return json.loads(out.stdout)


WATERMARK_FONT = Path(__file__).resolve().parent / "static" / "fonts" / "Poppins-Bold.ttf"
WATERMARK_POS = {
    "bottom-right": "x=w-tw-h*0.03:y=h-th-h*0.03", "bottom-left": "x=h*0.03:y=h-th-h*0.03",
    "top-right": "x=w-tw-h*0.03:y=h*0.03", "top-left": "x=h*0.03:y=h*0.03",
    "bottom-center": "x=(w-tw)/2:y=h-th-h*0.03", "center": "x=(w-tw)/2:y=(h-th)/2",
}


def watermark(src: Path, out: Path, text: str, opacity: float = 0.35, position: str = "bottom-right",
              size: float = 4.0, font: Path = None) -> Path:
    """Burn a small translucent text watermark (e.g. @username) onto the video."""
    text = re.sub(r"[^@A-Za-z0-9._\- ]", "", text or "").strip()[:40]
    if not text:
        return src
    opacity = max(0.05, min(1.0, float(opacity)))
    fontpath = str(font or WATERMARK_FONT).replace("\\", "/").replace(":", r"\:")  # drawtext/Windows escaping
    pos = WATERMARK_POS.get(position, WATERMARK_POS["bottom-right"])
    draw = (f"drawtext=fontfile='{fontpath}':text='{text}':fontcolor=white@{opacity:.2f}:"
            f"fontsize=h*{float(size) / 100:.4f}:{pos}:"
            f"shadowcolor=black@{opacity * 0.6:.2f}:shadowx=2:shadowy=2")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-vf", draw,
         "-c:a", "copy", "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", out])
    return out


def duration(path: Path) -> float:
    return float(probe(path)["format"]["duration"])


def has_audio(path: Path) -> bool:
    return any(s.get("codec_type") == "audio" for s in probe(path)["streams"])


def video_size(path: Path) -> tuple:
    for stream in probe(path)["streams"]:
        if stream.get("codec_type") == "video":
            return int(stream["width"]), int(stream["height"])
    raise MediaError(f"No video stream in {path}")


def silences(path: Path, noise_db: float = -35.0, min_len: float = 0.25) -> list:
    """[(start, end), ...] of quiet stretches, used to snap cuts to pauses in speech."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn",
         "-af", f"silencedetect=noise={noise_db}dB:d={min_len}", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
    )
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", result.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: (-?[\d.]+)", result.stderr)]
    total = None
    pairs = []
    for i, start in enumerate(starts):
        if i < len(ends):
            end = ends[i]
        else:  # silence runs to the end of the file
            total = total if total is not None else duration(path)
            end = total
        pairs.append((max(0.0, start), end))
    return pairs


def skin_stats(path: Path) -> tuple:
    """Median brightness (Y) and tint (U, V) of the middle of the frame, where the person's
    face and chest usually are in a talking clip. Sampled at 2 frames a second."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-i", str(path), "-vf",
         "fps=2,crop=iw*0.4:ih*0.3:iw*0.3:ih*0.33,signalstats,metadata=print:file=-",
         "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    values = {"YAVG": [], "UAVG": [], "VAVG": []}
    for key, value in re.findall(r"lavfi\.signalstats\.(YAVG|UAVG|VAVG)=([\d.]+)", result.stdout + result.stderr):
        values[key].append(float(value))
    if not values["YAVG"]:
        raise MediaError(f"Couldn't measure {path.name}")

    def median(xs):
        xs = sorted(xs)
        return xs[len(xs) // 2]

    return median(values["YAVG"]), median(values["UAVG"]), median(values["VAVG"])


def shift_colour(src: Path, dst: Path, dy: float, du: float, dv: float) -> Path:
    """Move brightness and tint by fixed amounts (YUV offsets), audio untouched."""
    lut = (f"lutyuv=y='clip(val+{dy:.1f},0,255)':u='clip(val+{du:.1f},0,255)':"
           f"v='clip(val+{dv:.1f},0,255)'")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-vf", lut, "-c:v", "libx264", "-preset", "fast",
         "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy", dst])
    return dst


def make_proxy(src: Path, dst: Path, height: int = 480, crf: int = 30,
               fps: float = 2, audio_kbps: int = 48) -> Path:
    """Small copy for Gemini to watch. Inline requests must stay under ~15 MB.
    Gemini samples 1 frame per second, so frames beyond 2 fps are wasted bytes."""
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", src,
         "-vf", f"fps={fps},scale=-2:{height}", "-c:v", "libx264", "-preset", "veryfast", "-crf", crf,
         "-c:a", "aac", "-b:a", f"{audio_kbps}k", "-ac", "1", dst])
    return dst


def add_silent_audio(src: Path, dst: Path) -> Path:
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", src,
         "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
         "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-shortest", dst])
    return dst
