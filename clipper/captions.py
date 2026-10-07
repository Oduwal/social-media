"""Auto captions: HyperFrames' on-device speech-to-text (`hyperframes transcribe`, the Parakeet
model) gives every word with its timing; the words are grouped into short captions (1-3 words,
the way short-form captions read) for the editor. Nothing leaves this PC.

The transcript is kept per clip (transcript.json) until the clip's video changes. Accents can
trip the recogniser, so captions are meant to be checked and fixed in the editor.
"""

import json
import logging
import re
import shutil
import tempfile
from pathlib import Path

from . import media, studio

log = logging.getLogger(__name__)

TRANSCRIPT = "transcript.json"
MAX_WORDS, MAX_CHARS, MAX_GAP = 3, 18, 0.5
STYLE = {"style": "caption", "position": "bottom", "size": 84, "color": "#FFFFFF",
         "animation": "highlight", "accent": "#FFE600", "auto": True}


class CaptionError(RuntimeError):
    pass


def transcribe(job_dir: Path) -> list:
    """[{"text", "start", "end"}, ...] for every spoken word of transformed.mp4 (cached)."""
    video = job_dir / "transformed.mp4"
    cache = job_dir / TRANSCRIPT
    stamp = video.stat().st_mtime
    if cache.exists():
        saved = json.loads(cache.read_text(encoding="utf-8"))
        if saved.get("source_mtime") == stamp:
            return saved["words"]
    if not media.has_audio(video):
        raise CaptionError("This clip has no sound to caption")
    work = Path(tempfile.mkdtemp(prefix="clipper_tx_"))
    try:
        audio = work / "audio.wav"
        media.run(["ffmpeg", "-y", "-loglevel", "error", "-i", video, "-vn", "-ac", "1", "-ar", "16000", audio])
        result = _transcribe(audio, work)
        if not result.get("ok") and result.get("reason") == "whisper_unavailable":
            # first time: fetch the speech model (about 600 MB, a few minutes, once per PC)
            log.info("Installing HyperFrames' speech model (Parakeet), once")
            studio._run(["models", "install", "parakeet", "--json"], timeout=1800)
            result = _transcribe(audio, work)
        if not result.get("ok"):
            raise CaptionError(f"Speech-to-text didn't work: {result.get('reason') or result}")
        raw = json.loads(Path(result["transcriptPath"]).read_text(encoding="utf-8"))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    words = [{"text": str(w["text"]).strip(), "start": round(float(w["start"]), 3),
              "end": round(float(w["end"]), 3)}
             for w in (raw if isinstance(raw, list) else raw.get("words", [])) if str(w.get("text", "")).strip()]
    cache.write_text(json.dumps({"source_mtime": stamp, "words": words}, ensure_ascii=False), encoding="utf-8")
    return words


def _transcribe(audio: Path, work: Path) -> dict:
    try:
        out = studio._run(["transcribe", audio, "--dir", work, "--json"], timeout=1800).stdout
    except studio.StudioError as err:
        # the CLI exits 1 with its JSON answer when no engine is installed
        text = str(err)
        match = re.search(r"\{.*\}", text, flags=re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except ValueError:
                pass
        raise CaptionError(text) from err
    return json.loads(out.strip().splitlines()[-1])


def group(words: list) -> list:
    """Short captions: a new one after 3 words, ~18 letters, a pause, or a full stop/comma."""
    groups, cur = [], []
    for w in words:
        if cur:
            gap = w["start"] - cur[-1]["end"]
            text = " ".join(x["text"] for x in cur + [w])
            if (len(cur) >= MAX_WORDS or len(text) > MAX_CHARS or gap > MAX_GAP
                    or re.search(r"[.!?,;:]$", cur[-1]["text"])):
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def captions_for(job_dir: Path, trim_start: float, trim_end: float) -> list:
    """Caption texts for the editor, timed from the trimmed clip's start."""
    words = [w for w in transcribe(job_dir) if w["end"] > trim_start and w["start"] < trim_end]
    texts = []
    chunks = group(words)
    for i, chunk in enumerate(chunks):
        start = max(chunk[0]["start"], trim_start) - trim_start
        end = min(chunk[-1]["end"], trim_end) - trim_start
        nxt = (chunks[i + 1][0]["start"] - trim_start) if i + 1 < len(chunks) else trim_end - trim_start
        if nxt - end < 0.4:  # close the small gap so captions don't blink between phrases
            end = nxt
        end = max(end, start + 0.4)
        timings = [[round(max(w["start"] - trim_start, start), 3), round(min(w["end"] - trim_start, end), 3)]
                   for w in chunk]
        texts.append({"text": " ".join(w["text"] for w in chunk), "start": round(start, 3),
                      "end": round(end, 3), "words": timings, **STYLE})
    return texts
