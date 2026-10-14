"""AI intro for a clip, and image-overlay ideas.

Intro: Gemini writes a one-line spoken hook from what the clip says, a Gemini speech model reads it
in a deep Nigerian male voice (Algenib, a little faster), and the file is saved into the clip's
uploads so the editor treats it like any other sound: you can move it, retime it or delete it.
Free Gemini keys are tried one after another here without touching the highlight step's resting
list (a busy speech model must not bench a key the highlights need). If every Gemini key is busy
and AI33_API_KEY is set, ai33.pro's Nigerian voice is used instead.

Overlay ideas: from the clip's timed transcript, Gemini points at the moments where a picture
(a person, place, brand, headline) would help, and says what to search for.
"""

import json
import logging
import re
import subprocess
import time
import wave
from pathlib import Path

import requests
from pydantic import BaseModel, Field

from . import captions, config, editor2, gemini, highlight, media

log = logging.getLogger(__name__)

VOICE = "Algenib"                       # the deepest male voice Gemini offers (~120 Hz)
TTS_MODELS = ("gemini-3.1-flash-tts-preview", "gemini-3.8-flash-tts")
SPEED = 1.15                            # "a bit faster"; atempo keeps the pitch
TONE = ("Speak as a deep-voiced adult Nigerian man, low pitch, confident and punchy, a little fast, "
        "with a Nigerian English accent: ")
AI33_BASE = "https://api.ai33.pro"
AI33_VOICE = "edge_en-NG-AbeoNeural"
INTRO_FILE = "ai_intro"                 # uploads/ai_intro.wav|mp3, replaced on every redo
MAX_WORDS = 16

_next_key = [0]


class VoiceError(RuntimeError):
    pass


# ------------------------------------------------------------------ the hook line

class _Hook(BaseModel):
    hook: str = Field(description="One spoken sentence, at most 14 words, no emoji, hashtags or quotes")


HOOK_PROMPT = """You write the spoken hook that plays over the first seconds of a short vertical clip
of a Nigerian creator, read aloud by a narrator, to make people stay.

The clip: {clip}

Rules:
- ONE sentence, 14 words at most, natural spoken Nigerian English (light Pidgin is fine).
- Frame what is coming or ask the question the clip answers, e.g. "Is {creator} actually right about
  this?" or "Wait till you hear what he said about Davido."
- Specific to THIS clip: name the person, topic or claim. Never a generic "watch this".
- Don't give away the punchline. No emoji, no hashtags, no quotation marks.

{hint}"""


def clean_hook(text: str) -> str:
    text = re.sub(r"#\w+|[\U0001F300-\U0001FAFF☀-➿]", "", str(text or ""))
    text = re.sub(r"[\"“”]", "", text)
    words = re.sub(r"\s+", " ", text).strip().split()
    return " ".join(words[:MAX_WORDS]).strip()


def write_hook(item: dict, cfg: dict, creator: str = "", avoid: str = "") -> str:
    """The spoken hook for one clip ({title, spoken_words, hook, reason})."""
    from google.genai import types

    prompt = HOOK_PROMPT.format(clip=json.dumps(item, ensure_ascii=False), creator=creator or "he",
                                hint=f"Don't reuse this hook: {avoid}" if avoid else "")
    resp = highlight.make_client(cfg).models.generate_content(
        model=cfg["gemini"]["model"], contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=_Hook,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
    parsed = resp.parsed or _Hook.model_validate_json(resp.text)
    text = clean_hook(parsed.hook)
    if not text:
        raise VoiceError("Gemini returned an empty hook")
    return text


# ------------------------------------------------------------------ the voice

def _gemini_pcm(text: str) -> bytes:
    """24 kHz mono 16-bit PCM of `text` in the Algenib voice. Each free key is tried on each speech
    model; the starting key rotates so a batch spreads its load."""
    from google import genai
    from google.genai import types

    keys = gemini.keys()
    if not keys:
        raise VoiceError("No Gemini key is set up")
    errors = []
    start = _next_key[0] % len(keys)
    _next_key[0] += 1
    for model in TTS_MODELS:
        for n in range(len(keys)):
            i = (start + n) % len(keys)
            try:
                client = genai.Client(api_key=keys[i], http_options=types.HttpOptions(timeout=120_000))
                resp = client.models.generate_content(
                    model=model, contents=TONE + text,
                    config=types.GenerateContentConfig(
                        response_modalities=["AUDIO"],
                        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE)))))
                data = resp.candidates[0].content.parts[0].inline_data.data
                if data:
                    return data
                errors.append(f"{model}#{i + 1}: no audio")
            except Exception as err:  # noqa: BLE001 - busy, rate limited, quota: try the next key
                errors.append(f"{model}#{i + 1}: {getattr(err, 'code', '')} {str(err)[:60]}")
    raise VoiceError("Gemini couldn't speak it: " + " | ".join(errors[-4:]))


def _ai33_audio(text: str, speed: float) -> bytes:
    key = config.secret("AI33_API_KEY")
    if not key:
        raise VoiceError("AI33_API_KEY isn't set")
    head = {"xi-api-key": key}
    sub = requests.post(f"{AI33_BASE}/v3/text-to-speech", headers=head, timeout=60,
                        files={"text": (None, text), "voice_id": (None, AI33_VOICE),
                               "speed": (None, f"{min(1.5, max(0.5, speed)):.2f}")})
    if sub.status_code != 200 or not sub.json().get("task_id"):
        raise VoiceError(f"ai33.pro refused it: {sub.status_code} {sub.text[:120]}")
    task = sub.json()["task_id"]
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        time.sleep(2)
        info = requests.get(f"{AI33_BASE}/v1/task/{task}", headers=head, timeout=30).json()
        if info.get("status") == "done":
            url = (info.get("metadata") or {}).get("audio_url")
            if not url:
                raise VoiceError("ai33.pro finished without audio")
            return requests.get(url, timeout=60).content
        if info.get("status") in ("error", "failed"):
            raise VoiceError(f"ai33.pro failed: {info.get('error_message')}")
    raise VoiceError("ai33.pro took too long")


def speak(text: str, out_dir: Path, speed: float = SPEED) -> Path:
    """The spoken `text` saved as out_dir/ai_intro.wav (Gemini) or .mp3 (ai33.pro fallback)."""
    text = clean_hook(text)
    if not text:
        raise VoiceError("Nothing to say")
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob(INTRO_FILE + ".*"):
        old.unlink(missing_ok=True)
    try:
        pcm = _gemini_pcm(text)
    except VoiceError as err:
        log.warning("Gemini voice failed (%s)%s", err,
                    "; trying ai33.pro" if config.secret("AI33_API_KEY") else "")
        if not config.secret("AI33_API_KEY"):
            raise
        path = out_dir / f"{INTRO_FILE}.mp3"
        path.write_bytes(_ai33_audio(text, speed))
        return path
    raw = out_dir / f"{INTRO_FILE}.raw.wav"
    with wave.open(str(raw), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm)
    path = out_dir / f"{INTRO_FILE}.wav"
    try:
        # a bit faster without changing the pitch; trim leading/trailing silence
        media.run(["ffmpeg", "-y", "-loglevel", "error", "-i", raw, "-af",
                   f"atempo={speed:.3f},silenceremove=start_periods=1:start_threshold=-45dB,"
                   "areverse,silenceremove=start_periods=1:start_threshold=-45dB,areverse",
                   "-ar", "44100", path])
    finally:
        raw.unlink(missing_ok=True)
    return path


def make_intro(job: dict, job_dir: Path, cfg: dict, text: str = "") -> dict:
    """{"text", "file", "duration"}: the hook (written by Gemini unless `text` is given) and its
    voice, saved into the clip's uploads."""
    clip = job.get("clip") or {}
    if not text:
        item = {"title": (job.get("info") or {}).get("title", ""),
                "spoken_words": (clip.get("spoken_words") or "")[:800],
                "hook": clip.get("hook", ""), "reason": clip.get("reason", "")}
        text = write_hook(item, cfg, creator=str(job.get("handle") or "").lstrip("@"))
    path = speak(text, job_dir / editor2.UPLOADS)
    return {"text": clean_hook(text), "file": path.name, "duration": round(media.duration(path), 2)}


# ------------------------------------------------------------------ image ideas for the editor

class _Idea(BaseModel):
    start: float = Field(description="Seconds from the start of the clip when the picture should appear")
    dur: float = Field(description="How long to show it, 2 to 4 seconds")
    label: str = Field(description="What the picture should show, a few words, e.g. 'Seyi Tinubu'")
    search: str = Field(description="What to type into an image search to find it")
    why: str = Field(description="What is being said at that moment, in a few words")


class _Ideas(BaseModel):
    ideas: list[_Idea]


IDEAS_PROMPT = """Below is the timed transcript of a short vertical clip ({length:.0f} seconds). Pick up to
{n} moments where showing a picture would help the viewer follow what is being talked about: a named
person, a place, a brand or product, a headline or document, an object, a scene someone describes.

Rules: only things actually mentioned; the picture appears when it is first mentioned; moments at
least 5 seconds apart; each shown for 2-4 seconds; `search` is a precise image-search query
(name + context). If nothing in the clip needs a picture, return an empty list.

Transcript, each line "[seconds] words":
{transcript}"""


def overlay_ideas(words: list, length: float, cfg: dict, n: int = 4) -> list:
    """Up to `n` [{start, dur, label, search, why}] from timed words; sorted, in range, apart."""
    from google.genai import types

    if not words:
        return []
    prompt = IDEAS_PROMPT.format(length=length, n=n, transcript=highlight.transcript_lines(words))
    resp = highlight.make_client(cfg).models.generate_content(
        model=cfg["gemini"]["model"], contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=_Ideas,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
    parsed = resp.parsed or _Ideas.model_validate_json(resp.text)
    out, last = [], -99.0
    for idea in sorted(parsed.ideas, key=lambda i: i.start):
        start = round(min(max(0.0, idea.start), max(0.0, length - 1.0)), 2)
        if start - last < 4.0 or not idea.label.strip():
            continue
        out.append({"start": start, "dur": round(min(4.0, max(2.0, idea.dur or 3.0), max(1.0, length - start)), 2),
                    "label": idea.label.strip()[:60], "search": (idea.search or idea.label).strip()[:120],
                    "why": idea.why.strip()[:100]})
        last = start
    return out[:n]


def words_for_clip(job_dir: Path, offset: float, length: float) -> list:
    """Parakeet's word timings for the clip, shifted so 0 is where the edited video starts."""
    words = []
    for w in captions.transcribe(job_dir):
        if w["end"] > offset and w["start"] < offset + length:
            words.append({**w, "start": round(max(0.0, w["start"] - offset), 3),
                          "end": round(w["end"] - offset, 3)})
    return words
