"""Auto captions: every word of the clip with its timing, grouped into short captions (1-3
words, the way short-form captions read) for the editor.

The words come from HyperFrames' on-device speech-to-text (`hyperframes transcribe`, the
Parakeet model; nothing leaves this PC), the default (config.yaml `captions: engine: parakeet`).
With `engine: gemini`, Gemini listens to the clip instead (it follows Nigerian English, Pidgin and
Yoruba/Igbo/Hausa better), and Parakeet takes over if Gemini can't be reached.

The transcript is kept per clip (transcript.json) until the clip's video changes. Captions are
still meant to be checked and fixed in the editor.
"""

import json
import logging
import math
import difflib
import re
import shutil
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from . import media, studio

log = logging.getLogger(__name__)

TRANSCRIPT = "transcript.json"
MAX_WORDS, MAX_CHARS, MAX_GAP = 3, 18, 0.5
STYLE = {"style": "caption", "position": "bottom", "size": 84, "color": "#FFFFFF",
         "animation": "highlight", "accent": "#FFE600", "auto": True}


class CaptionError(RuntimeError):
    pass


def transcribe(job_dir: Path, reference: str = "", cfg: dict = None) -> list:
    """[{"text", "start", "end"}, ...] for every spoken word of transformed.mp4 (cached).
    `cfg` turns on Gemini's transcript (see engine_for). `reference`: what Gemini heard while
    picking this clip (its spoken_words); when the on-device recogniser did the listening, where
    they disagree its spelling is used and the recogniser's timing kept."""
    words, engine = _load(job_dir / "transformed.mp4", job_dir / TRANSCRIPT, cfg)
    return correct_words(words, reference) if reference and engine != "gemini" else words


MIN_AGREEMENT = 0.5  # below this the two transcripts are too different to trust the match


def _norm(token: str) -> str:
    return re.sub(r"[^\w']", "", token.lower())


def correct_words(words: list, reference: str) -> list:
    """Parakeet's word timings with Gemini's wording. The two transcripts are lined up word by
    word; matching words take Gemini's spelling and punctuation, a stretch they disagree on gets
    Gemini's words spread over the same stretch of time. Words only Parakeet heard are kept
    (never drop speech), and words only Gemini heard are added only where there's a gap to put
    them in. If the transcripts barely agree, Parakeet's are returned untouched."""
    ref = reference.split()
    if not words or not ref:
        return words
    a, b = [_norm(w["text"]) for w in words], [_norm(t) for t in ref]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if sm.ratio() < MIN_AGREEMENT:
        return words
    out = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            out += [{**words[i1 + k], "text": ref[j1 + k]} for k in range(i2 - i1)]
        elif op == "delete":
            out += words[i1:i2]
        elif op == "replace":
            start, end = words[i1]["start"], words[i2 - 1]["end"]
            out += _spread(ref[j1:j2], start, end)
        elif op == "insert":
            prev_end = out[-1]["end"] if out else 0.0
            next_start = words[i1]["start"] if i1 < len(words) else prev_end + 0.12 * (j2 - j1)
            if next_start - prev_end >= 0.12 * (j2 - j1):
                out += _spread(ref[j1:j2], prev_end, next_start)
    return out


def _spread(tokens: list, start: float, end: float) -> list:
    """The tokens laid evenly (by letters) across [start, end]."""
    total = sum(len(t) for t in tokens) or 1
    out, at = [], start
    for t in tokens:
        nxt = at + (end - start) * len(t) / total
        out.append({"text": t, "start": round(at, 3), "end": round(nxt, 3)})
        at = nxt
    return out


def engine_for(cfg: dict = None) -> str:
    """"gemini" or "parakeet": who transcribes. Without a config (the long source video that picks
    highlights from a transcript) it is always the on-device one."""
    if not cfg:
        return "parakeet"
    return "gemini" if str((cfg.get("captions") or {}).get("engine", "parakeet")).lower() == "gemini" else "parakeet"


def words_for(video: Path, cache: Path, cfg: dict = None) -> list:
    """Transcribe any video to word-level timings [{"text", "start", "end"}, ...], cached next to
    it and reused until the video changes. Used for captions (transformed.mp4) and for picking
    highlights from a transcript (source.mp4)."""
    return _load(video, cache, cfg)[0]


def _load(video: Path, cache: Path, cfg: dict = None) -> tuple:
    """(words, engine that heard them). A saved transcript is reused only if the engine asked for
    is the one that made it (so switching engines redoes it)."""
    want = engine_for(cfg)
    stamp = video.stat().st_mtime
    if cache.exists():
        saved = json.loads(cache.read_text(encoding="utf-8"))
        have = saved.get("engine", "parakeet")
        if saved.get("source_mtime") == stamp and have == want:
            return saved["words"], have
    if not media.has_audio(video):
        raise CaptionError("This clip has no sound to caption")
    words, engine = None, "parakeet"
    work = Path(tempfile.mkdtemp(prefix="clipper_tx_"))
    try:
        audio = work / "audio.wav"
        media.run(["ffmpeg", "-y", "-loglevel", "error", "-i", video, "-vn", "-ac", "1", "-ar", "16000", audio])
        length = media.duration(audio)
        gemini_error = ""
        if want == "gemini" and length <= GEMINI_MAX_S:
            try:
                words, engine = gemini_words(audio, length, cfg, work), "gemini"
            except Exception as err:  # noqa: BLE001 - no key, busy, refused: the on-device model is the fallback
                gemini_error = str(err)[:200]
                log.warning("Gemini couldn't transcribe the clip (%s); using the on-device recogniser", gemini_error)
        if words is None:
            try:
                words = _transcribe_all(audio, length, work)
            except (CaptionError, studio.StudioError) as err:
                if gemini_error:
                    raise CaptionError(f"Gemini couldn't transcribe it ({gemini_error}) and the on-device "
                                       f"recogniser isn't available either ({str(err)[:120]})") from err
                raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    cache.write_text(json.dumps({"source_mtime": stamp, "engine": engine, "words": words},
                                ensure_ascii=False), encoding="utf-8")
    return words, engine


# ------------------------------------------------------------------ Gemini's transcript

GEMINI_MAX_S = 15 * 60   # longer audio stays with the on-device model (a very long answer, and a big upload)
MAX_SEGMENT_S = 12.0


class _Segment(BaseModel):
    text: str = Field(description="What is said in this stretch, exactly as spoken, 2 to 8 words")
    start_s: float = Field(description="Second the first word of this stretch begins, to 2 decimals")
    end_s: float = Field(description="Second the last word of this stretch ends, to 2 decimals")


class _Transcript(BaseModel):
    segments: list[_Segment] = Field(description="Every spoken stretch in order; empty if nobody speaks")


TRANSCRIBE_PROMPT = """Transcribe the speech in this {length:.1f}-second audio clip, for subtitles.

- This is Nigerian content: Nigerian English, Pidgin, and some Yoruba, Igbo or Hausa. Write what is
  actually said, in the spelling a Nigerian reader expects ("dey", "wahala", "abeg", "na so"). Never
  translate, tidy up or "correct" the grammar, and never leave out swearing or slang.
- Split the speech into short subtitle stretches of 2 to 8 words, at natural pauses. Put normal
  punctuation (. , ? !) where it belongs.
- start_s and end_s are seconds from the very start of the audio, to 2 decimals, so each stretch lines
  up with when it is really spoken. Stretches follow each other and never overlap.
- Skip music, laughter and noise that is not speech. If nobody speaks, return an empty list."""


def gemini_words(audio: Path, length: float, cfg: dict, work: Path) -> list:
    """Word timings from Gemini listening to `audio`: it gives short timed stretches of speech, and
    each stretch's words are spread across its time by letters."""
    from google.genai import types

    from . import highlight

    mp3 = work / "tx.mp3"
    media.run(["ffmpeg", "-y", "-loglevel", "error", "-i", audio, "-vn", "-ac", "1", "-ar", "16000",
               "-c:a", "libmp3lame", "-b:a", "48k", mp3])
    resp = highlight.make_client(cfg).models.generate_content(
        model=cfg["gemini"]["model"],
        contents=[types.Part.from_bytes(data=mp3.read_bytes(), mime_type="audio/mp3"),
                  TRANSCRIBE_PROMPT.format(length=length)],
        config=types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=_Transcript,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
    parsed = resp.parsed or _Transcript.model_validate_json(resp.text)
    words = words_from_segments([(s.text, s.start_s, s.end_s) for s in parsed.segments], length)
    if not words:
        raise CaptionError("Gemini heard no speech in this clip")
    return words


def words_from_segments(segments: list, length: float) -> list:
    """[(text, start, end)] -> word timings. Times are clamped to the clip, kept in order and
    non-overlapping; a stretch with a missing or backwards end gets a plain reading speed."""
    words, floor = [], 0.0
    for text, start, end in sorted(segments, key=lambda s: s[1]):
        tokens = str(text or "").split()
        if not tokens:
            continue
        start = min(max(float(start), floor), max(length - 0.05, 0.0))
        end = float(end)
        if end - start < 0.08 * len(tokens) or end - start > MAX_SEGMENT_S:
            end = start + min(MAX_SEGMENT_S, 0.35 * len(tokens))
        end = min(max(end, start + 0.1), max(length, start + 0.1))
        words += _spread(tokens, start, end)
        floor = end
    return words


# HyperFrames sets a Node timeout of duration x 2000 ms, which it rejects when it isn't a whole
# number (a non-integer duration) or is very large (a long video). So transcribe in chunks, each
# padded to a whole number of seconds, and stitch the word times back together.
CHUNK_S = 480


def _transcribe_all(audio: Path, total: float, work: Path) -> list:
    words, offset, index, installed = [], 0.0, 0, False
    while offset < max(total, 0.1):
        length = min(CHUNK_S, math.ceil(total - offset)) or 1
        seg = work / f"seg_{index}.wav"
        media.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{offset:.3f}", "-i", audio,
                   "-t", str(length), "-af", "apad", "-ac", "1", "-ar", "16000", seg])  # exactly `length` s
        words += _chunk_words(seg, offset, work / f"c{index}", install=not installed)
        installed = True
        offset += CHUNK_S
        index += 1
    return words


def _chunk_words(audio: Path, offset: float, work: Path, install: bool) -> list:
    work.mkdir(parents=True, exist_ok=True)
    result = _transcribe(audio, work)
    if install and not result.get("ok") and result.get("reason") == "whisper_unavailable":
        # first time: fetch the speech model (about 600 MB, a few minutes, once per PC)
        log.info("Installing HyperFrames' speech model (Parakeet), once")
        studio._run(["models", "install", "parakeet", "--json"], timeout=1800)
        result = _transcribe(audio, work)
    if not result.get("ok"):
        raise CaptionError(f"Speech-to-text didn't work: {result.get('error') or result.get('reason') or result}")
    raw = json.loads(Path(result["transcriptPath"]).read_text(encoding="utf-8"))
    return [{"text": str(w["text"]).strip(), "start": round(float(w["start"]) + offset, 3),
             "end": round(float(w["end"]) + offset, 3)}
            for w in (raw if isinstance(raw, list) else raw.get("words", [])) if str(w.get("text", "")).strip()]


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


def captions_for(job_dir: Path, trim_start: float, trim_end: float, reference: str = "",
                 cfg: dict = None) -> list:
    """Caption texts for the editor, timed from the trimmed clip's start."""
    words = [w for w in transcribe(job_dir, reference, cfg=cfg) if w["end"] > trim_start and w["start"] < trim_end]
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
