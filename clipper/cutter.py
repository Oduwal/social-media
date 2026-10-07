"""Turn Gemini's rough timestamps into a clean cut and render it at 9:16.

Gemini's timestamps are only good to about a second, so the cut points are
snapped to the nearest pause in speech and padded slightly.
"""

from pathlib import Path

from . import media

SNAP_WINDOW = 1.5
PAD = 0.3
SEGMENT_S = 10  # transform_video.py's SEGMENT_SECONDS


def _nearest(target: float, points: list, window: float):
    best = None
    for point in points:
        if abs(point - target) <= window and (best is None or abs(point - target) < abs(best - target)):
            best = point
    return best


def choose_cut(start: float, end: float, duration: float, silences: list,
               min_s: float, max_s: float, window: float = SNAP_WINDOW, pad: float = PAD,
               segment_s: float = SEGMENT_S) -> tuple:
    start, end = _choose(start, end, duration, silences, min_s, max_s, window, pad)
    return _avoid_tiny_tail(start, end, min_s, segment_s)


def _avoid_tiny_tail(start: float, end: float, min_s: float, segment_s: float) -> tuple:
    """The transform splits into segment_s pieces and bills each one. A clip just over
    a multiple (e.g. 30.2 s) would leave a sliver of a segment that still costs a full
    edit, so trim back to just under the multiple when that keeps the clip long enough."""
    length = end - start
    over = length % segment_s
    if length > segment_s and over < 1.0:
        trimmed = end - over - 0.1
        if trimmed - start >= min_s:
            end = trimmed
    return round(start, 3), round(end, 3)


def _choose(start, end, duration, silences, min_s, max_s, window, pad) -> tuple:
    """Pure function: (start, end) seconds for the final cut."""
    if duration <= min_s:
        return 0.0, duration

    start = min(max(0.0, float(start)), duration)
    end = min(max(0.0, float(end)), duration)
    if end - start < 1.0:
        raise ValueError(f"Gemini gave an unusable range {start:.1f}-{end:.1f}s")

    # Speech starts where a silence ends; speech stops where a silence starts.
    snapped_start = _nearest(start, [s_end for _, s_end in silences], window)
    snapped_end = _nearest(end, [s_start for s_start, _ in silences], window)
    if snapped_start is not None:
        start = max(0.0, snapped_start - pad)
    if snapped_end is not None:
        end = min(duration, snapped_end + pad)

    if end - start < min_s:
        end = min(duration, start + min_s)
        start = max(0.0, end - min_s)
    if end - start > max_s:
        limit = start + max_s
        # prefer ending on a pause just before the hard limit
        pauses = [s for s, _ in silences if start + min_s <= s <= limit]
        end = min(limit, pauses[-1] + pad) if pauses else limit
    return round(start, 3), round(end, 3)


def render(src: Path, dst: Path, start: float, end: float, width: int = 1080, height: int = 1920) -> Path:
    """Re-encoded cut of the ORIGINAL download, letterboxed to 9:16 at 30 fps."""
    vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
          f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=30")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", src]
    if media.has_audio(src):
        # apad: a TikTok's sound can end before its video; keep audio for the whole clip,
        # because the transform's segments and the music ducking both need it.
        cmd += ["-t", f"{end - start:.3f}", "-map", "0:v:0", "-map", "0:a:0", "-af", "apad"]
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-t", f"{end - start:.3f}", "-map", "0:v:0", "-map", "1:a:0"]
    cmd += ["-vf", vf, "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2", dst]
    media.run(cmd)
    return dst


def thumbnail(video: Path, dst: Path) -> Path:
    """A still for the library grid, taken a second in (past any fade-in)."""
    at = min(1.0, media.duration(video) / 2)
    media.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{at:.2f}", "-i", video,
               "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "4", dst])
    return dst


def cut(source: Path, job_dir: Path, clip: dict, min_s: float, max_s: float) -> dict:
    total = media.duration(source)
    silences = media.silences(source)
    start, end = choose_cut(clip["start_s"], clip["end_s"], total, silences, min_s, max_s)
    render(source, job_dir / "highlight.mp4", start, end)
    return {"start": start, "end": end, "gemini_start": clip["start_s"], "gemini_end": clip["end_s"]}
