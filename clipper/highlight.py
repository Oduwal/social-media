"""Ask Gemini to watch the video and pick the best short-form highlight.

Runs on Vertex AI with the same service account as transform_video.py, or on a free
Gemini API key when Vertex has no credit (gemini.py). A 480p proxy goes inline in the
request, which both accept.
"""

import json
import logging
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from . import media

log = logging.getLogger(__name__)

INLINE_LIMIT = 15 * 1024 * 1024
MOODS = ("hype", "chill", "funny", "emotional", "dramatic", "inspirational", "suspense")


MAX_CLIPS = 25


# No numeric/length limits (ge/le/max_length) in these models: each is accepted by Vertex
# alone, but the combination makes it reject the request as INVALID_ARGUMENT. The limits
# are written into the descriptions and enforced in clean_clips() instead.
CAPTION_RULES = (
    "The post caption, written like a Nigerian viewer reacting, NOT a description of the video. "
    "Never narrate or report what happens ('He talks about...', 'The creator explains...'). Instead give a "
    "relatable reaction, comment or punchline someone would actually type under it, usually in Pidgin or "
    "casual Nigerian English, e.g. 'gehgeh that one go far o', 'omo this one don pass me', 'no be small thing', "
    "'who else dey go through this?', 'na wa o'. Short (under 12 words), one emoji at most, no hashtags. "
    "Make people want to comment, tag a friend or argue.")

class Clip(BaseModel):
    # Field order is generation order: grounding text before the timestamps.
    title: str = Field(description="A 3-8 word label for this moment, for the editor's library")
    spoken_words: str = Field(description="The exact words spoken inside this clip")
    reason: str = Field(description="Why this moment works as a standalone short")
    hook: str = Field(description="What grabs attention in the first 2 seconds")
    start_mmss: str = Field(description="Start time, MM:SS (H:MM:SS past one hour)")
    end_mmss: str = Field(description="End time, MM:SS (H:MM:SS past one hour)")
    start_s: float = Field(description="Start time in seconds")
    end_s: float = Field(description="End time in seconds")
    score: int = Field(description="How likely this clip is to get views, comments and shares, from 1 to 10, "
                                   "10 = best")
    people_count: int = Field(description="How many different people are visible in this clip")
    creator_description: str = Field(
        description="How to pick out the creator among the people in this clip, by what can be SEEN: "
                    "clothing, position in the frame, hair or headwear, accessories. Never mention skin "
                    "colour, and never refer to speaking, talking, voice or 'the speaker' (the video editing "
                    "model reads that as a request to edit speech and refuses). Empty if only the creator "
                    "is visible.")
    caption: str = Field(description=CAPTION_RULES)
    hashtags: list[str] = Field(description="Up to 5 hashtags for this clip, each starting with #")
    music_mood: Literal[MOODS] = Field(description="Background music mood that fits this clip")


class Highlights(BaseModel):
    video_duration_s: float
    clips: list[Clip] = Field(description=f"1 to {MAX_CLIPS} clips, in timeline order")



AUDIENCE = """About this content and what we want from it:
- This is NIGERIAN content for a Nigerian (and Nigerian-diaspora) audience. Understand it the way they
  do: Nigerian English, Pidgin, and Yoruba / Igbo / Hausa phrases and slang; local politics, celebrities,
  relationships, religion, money talk, "japa", street and campus culture, and the humour that goes
  with them. Judge what is funny, shocking or relatable by how a Nigerian viewer would react.
- We want clips that get the MOST views, comments and shares. Pick the moments people argue about,
  react to and send to friends: heated arguments, hot takes, controversy, public drama, confessions,
  call-outs, insults and roasts, scandal, shocking or embarrassing moments, outrage and rage-bait,
  and violence or physical confrontation when it happens. Do not skip a moment because it is
  controversial, aggressive or violent; those are exactly the moments to keep.
- CAPTIONS: write each caption as a relatable reaction a Nigerian viewer would post, in Pidgin or casual
  Nigerian English, never as a report of what the video is about. Good: "gehgeh that one go far o".
  Bad: "Gehgeh talks about how far he has gone".
- Score each clip by how much engagement it is likely to get (comments, shares, replays), not by how
  polite or tidy it is. Still never start or end mid-sentence, and keep the strong hook."""
PROMPT = """You are a short-form video editor for TikTok, Instagram Reels and X.
Watch AND listen to this whole {minutes}-minute video by @{handle}.
Find EVERY self-contained moment that would work as its own short, each between
{min_s} and {max_s} seconds long. Each one needs:
- a strong hook in the first 2 seconds
- a complete thought: never start or end mid-sentence
- something funny, surprising, emotional, useful or quotable
Moments must not overlap. Skip filler, intros and dead air; don't pad the list with weak
moments, but don't stop at a few either: a long video usually has many. {count_hint}
Give timestamps as they appear on the video's timeline, both as MM:SS and in seconds.
For each clip, count the different people visible. If anyone besides @{handle} appears, describe
@{handle} only by how they look and where they are (clothing, position, hair, accessories; never
skin colour, and never as "the speaker" or by what they say). @{handle} is usually the person
facing the camera most.
If the whole video is shorter than {min_s} seconds, return the whole video as the only clip.
The creator's own caption was: {caption!r}

{audience}"""


def count_hint(duration_s: float) -> str:
    minutes = duration_s / 60
    if minutes < 2:
        return "A video this short usually has 1-2."
    low, high = max(2, int(minutes / 3)), min(MAX_CLIPS, max(4, int(minutes)))
    return f"For this length, expect roughly {low}-{high}."


def make_client(cfg: dict):
    """Vertex, falling back to a free Gemini API key (see gemini.py)."""
    from . import gemini
    return gemini.make_client(cfg)


def video_part(proxy: Path):
    from google.genai import types

    data = proxy.read_bytes()
    if len(data) > INLINE_LIMIT:
        raise ValueError(f"Proxy is {len(data) / 1e6:.1f} MB, over the ~15 MB inline limit")
    return types.Part(inline_data=types.Blob(data=data, mime_type="video/mp4"),
                      video_metadata=types.VideoMetadata(fps=1.0))


# (name, height, crf, fps, audio kbps), smallest last. A 45-minute video fits the last one.
PROXY_LADDER = [
    ("proxy_480p.mp4", 480, 30, 2, 48),
    ("proxy_360p.mp4", 360, 34, 1, 32),
    ("proxy_240p.mp4", 240, 38, 1, 24),
]


def build_proxy(source: Path, job_dir: Path) -> Path:
    """Only Gemini sees this copy; the clip itself is cut from the full-quality original.
    Long videos step down the ladder until the copy fits in one request."""
    for name, height, crf, fps, kbps in PROXY_LADDER:
        proxy = media.make_proxy(source, job_dir / name, height=height, crf=crf, fps=fps, audio_kbps=kbps)
        if proxy.stat().st_size <= INLINE_LIMIT:
            return proxy
        log.info("%s is %.1f MB, too big to send; trying smaller", name, proxy.stat().st_size / 1e6)
    raise ValueError(f"Even the smallest preview is {proxy.stat().st_size / 1e6:.1f} MB "
                     f"(video is {media.duration(source) / 60:.0f} min); over Gemini's ~15 MB request limit")


def pick(source: Path, job_dir: Path, cfg: dict, caption: str = "", out: Path = None) -> dict:
    """Pick highlights with the configured engine (highlight.engine). Writes highlight.json and
    returns it. `out` overrides the output path (used to A/B an engine without overwriting)."""
    out = out or job_dir / "highlight.json"
    if out.exists():
        return json.loads(out.read_text(encoding="utf-8"))
    engine = cfg.get("highlight", {}).get("engine", "gemini")
    if engine == "agentrouter":
        return pick_via_transcript(source, job_dir, cfg, caption, out)
    return pick_via_gemini(source, job_dir, cfg, caption, out)


def pick_via_gemini(source: Path, job_dir: Path, cfg: dict, caption: str, out: Path) -> dict:
    """Gemini watches the video (a 480p proxy) and returns the highlights."""
    from google.genai import types

    duration = media.duration(source)
    proxy = build_proxy(source, job_dir)
    prompt = PROMPT.format(handle=cfg["creator"]["handle"] or "the creator",
                           minutes=max(1, round(duration / 60)), count_hint=count_hint(duration),
                           min_s=cfg["clip"]["min_seconds"], max_s=cfg["clip"]["max_seconds"],
                           caption=caption[:500], audience=AUDIENCE)
    client = make_client(cfg)
    resp = client.models.generate_content(
        model=cfg["gemini"]["model"],
        contents=[video_part(proxy), prompt],  # video before text
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=Highlights,
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )
    result = resp.parsed
    if result is None:
        result = Highlights.model_validate_json(resp.text)
    data = result.model_dump()
    data["clips"] = clean_clips(data["clips"], duration)
    if not data["clips"]:
        raise ValueError("Gemini returned no usable timestamps")
    data["engine"] = "gemini"
    data["model"] = cfg["gemini"]["model"]
    usage = getattr(resp, "usage_metadata", None)
    if usage is not None:
        data["usage"] = {"prompt_tokens": usage.prompt_token_count,
                         "output_tokens": usage.candidates_token_count}
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Gemini found %d highlight(s)", len(data["clips"]))
    return data


# ------------------------------------------------------------ transcript engine (Agent Router)

TRANSCRIPT_SYSTEM = (
    "You are a short-form video editor for TikTok, Instagram Reels and X. You are given the "
    "timestamped transcript of a video (you cannot see the picture, only what is said). Pick the "
    "moments that would each work as a self-contained short. Reply with ONLY a JSON object. "
    + AUDIENCE)

TRANSCRIPT_USER = """This is a {minutes}-minute video by @{handle}. Transcript, each line "[start_seconds] words":

{transcript}

Find EVERY self-contained moment that would make its own short, each between {min_s} and {max_s} seconds.
Each needs a strong hook in the first 2 seconds and a complete thought (never start or end mid-sentence).
Moments must not overlap; skip filler and dead air. {count_hint}
Base every timestamp on the transcript's own times. If the whole thing is shorter than {min_s} seconds,
return it as the single clip. The creator's own caption was: {caption!r}

Reply with ONLY this JSON (no prose, no markdown):
{{"video_duration_s": <number>, "clips": [{{
  "title": "<3-8 word label>", "spoken_words": "<exact words in this clip>",
  "reason": "<why it works as a short>", "hook": "<what grabs attention in the first 2 seconds>",
  "start_mmss": "<MM:SS>", "end_mmss": "<MM:SS>", "start_s": <number>, "end_s": <number>,
  "score": <1-10>, "people_count": 1, "creator_description": "",
  "caption": "<relatable Nigerian-voice reaction (Pidgin / casual), not a description; no hashtags>", "hashtags": ["#up to 5"],
  "music_mood": "<one of: {moods}>"
}}]}}"""


def transcript_lines(words: list, gap: float = 0.7) -> str:
    """Group word timings into readable lines, each prefixed with its start time in seconds."""
    lines, cur = [], []
    for w in words:
        if cur and (w["start"] - cur[-1]["end"] > gap or re.search(r"[.!?]$", cur[-1]["text"])):
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    return "\n".join(f"[{g[0]['start']:.1f}] " + " ".join(x["text"] for x in g) for g in lines)


def pick_via_transcript(source: Path, job_dir: Path, cfg: dict, caption: str, out: Path) -> dict:
    """A Claude/OpenAI model (via Agent Router) reads the transcript and returns the highlights."""
    from . import agentrouter, captions

    duration = media.duration(source)
    words = captions.words_for(source, job_dir / "source_transcript.json")
    if not words:
        raise ValueError("No speech found to pick highlights from")
    user = TRANSCRIPT_USER.format(
        handle=cfg["creator"]["handle"] or "the creator", minutes=max(1, round(duration / 60)),
        transcript=transcript_lines(words), count_hint=count_hint(duration),
        min_s=cfg["clip"]["min_seconds"], max_s=cfg["clip"]["max_seconds"],
        caption=caption[:500], moods=", ".join(MOODS))
    content = agentrouter.chat_json(cfg, TRANSCRIPT_SYSTEM, user)
    result = Highlights.model_validate_json(_json_block(content))
    data = result.model_dump()
    data["clips"] = clean_clips(data["clips"], duration)
    if not data["clips"]:
        raise ValueError("Agent Router returned no usable timestamps")
    data["engine"] = "agentrouter"
    data["model"] = cfg["highlight"]["agentrouter"]["model"]
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Agent Router (%s) found %d highlight(s)", data["model"], len(data["clips"]))
    return data


def _json_block(text: str) -> str:
    """The JSON object from a reply that might be wrapped in prose or ```json fences."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start != -1 and end > start else text


def clean_clips(clips: list, duration: float) -> list:
    """Timeline order; drop broken ranges, and where two overlap keep the stronger one.
    Also enforces the limits the schema can't carry (see the note above Clip)."""
    for clip in clips:
        clip["score"] = min(10, max(1, int(clip.get("score") or 1)))
        clip["people_count"] = min(20, max(0, int(clip.get("people_count") or 0)))
        clip["hashtags"] = list(clip.get("hashtags") or [])[:5]
    usable = [c for c in clips if 0 <= c["start_s"] < duration and c["end_s"] - c["start_s"] >= 3]
    kept = []
    for clip in sorted(usable, key=lambda c: c["score"], reverse=True)[:MAX_CLIPS]:
        if all(clip["end_s"] <= k["start_s"] or clip["start_s"] >= k["end_s"] for k in kept):
            kept.append(clip)
    return sorted(kept, key=lambda c: c["start_s"])


# ------------------------------------------------------------ rewriting captions

class _Rewritten(BaseModel):
    id: str
    caption: str = Field(description=CAPTION_RULES)


class _Rewrites(BaseModel):
    captions: list[_Rewritten]


REWRITE_PROMPT = """You write post captions for short clips of Nigerian creators, for TikTok, Instagram Reels and X.
Below are clips (id, title, what is said, why it works). Write ONE new caption for each.

{rules}

Return every id exactly as given.

Clips:
{clips}"""


def clean_caption(text: str) -> str:
    """One line, no hashtags (they're added separately), not absurdly long."""
    text = re.sub(r"#\w+", "", str(text or ""))
    return re.sub(r"\s+", " ", text).strip()[:200]


def rewrite_captions(clips: list, cfg: dict) -> dict:
    """{clip id: new caption} for clips given as [{id, title, spoken_words, hook, reason}], in the
    relatable Nigerian voice (see CAPTION_RULES). One small text request for the whole batch."""
    from google.genai import types

    prompt = REWRITE_PROMPT.format(rules=CAPTION_RULES, clips=json.dumps(clips, ensure_ascii=False, indent=1))
    resp = make_client(cfg).models.generate_content(
        model=cfg["gemini"]["model"], contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=_Rewrites,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
    parsed = resp.parsed or _Rewrites.model_validate_json(resp.text)
    wanted = {c["id"] for c in clips}
    return {r.id: clean_caption(r.caption) for r in parsed.captions if r.id in wanted and clean_caption(r.caption)}


class _Ideas(BaseModel):
    captions: list[str]


IDEAS_PROMPT = """You write post captions for a short clip of a Nigerian creator, for TikTok, Instagram Reels and X.

The clip: {clip}

{rules}

Write {n} DIFFERENT captions for this clip, each with a different angle: a funny one, a relatable one, a
shocking or hot-take one, a question that makes people comment, and so on. Vary the wording and length (all
short). Do not repeat or lightly reword any of these, which were already shown:
{avoid}"""


def suggest_captions(item: dict, cfg: dict, n: int = 6, avoid: list = None) -> list:
    """`n` different post captions for one clip ({title, spoken_words, hook, reason}), none repeating
    those in `avoid`. One small text request."""
    from google.genai import types

    prompt = IDEAS_PROMPT.format(clip=json.dumps(item, ensure_ascii=False), rules=CAPTION_RULES, n=n,
                                 avoid=json.dumps(list(avoid or [])[-30:], ensure_ascii=False))
    resp = make_client(cfg).models.generate_content(
        model=cfg["gemini"]["model"], contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=_Ideas,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
    parsed = resp.parsed or _Ideas.model_validate_json(resp.text)
    seen, out = {c.lower() for c in (avoid or [])}, []
    for raw in parsed.captions:
        text = clean_caption(raw)
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out[:n]
