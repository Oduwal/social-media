"""Trending TikTok sounds: fetch the charts, let Gemini pick the best fit for a clip.

The charts come from Upload-Post's "Get TikTok Trending Music" endpoint, which lists
tracks from TikTok's Commercial Music Library: licensed for posting, and attachable
to a TikTok post by id (tiktok_music_id), so the clip shows up on that sound's page.
Instagram and X have no such API, so this is TikTok-only.

Gemini *listens* to a short preview of each candidate while watching the clip, then
returns its top picks with reasons. The user can still pick any track from the charts.
"""

import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlencode

import requests
from pydantic import BaseModel, Field

from . import config, media

log = logging.getLogger(__name__)

API = "https://api.upload-post.com/api/uploadposts/tiktok/music/trending"
CHART_MAX_AGE_S = 12 * 3600
PREVIEW_SECONDS = 15
FLAGS = {"US": "🇺🇸", "NG": "🇳🇬", "GB": "🇬🇧", "GH": "🇬🇭", "ZA": "🇿🇦", "KE": "🇰🇪", "CA": "🇨🇦"}


class TrendingError(RuntimeError):
    pass


def folder() -> Path:
    path = config.DATA_DIR / "trending"
    (path / "previews").mkdir(parents=True, exist_ok=True)
    return path


# ------------------------------------------------------------------- charts

def fetch_chart(country: str, cfg: dict) -> list:
    """One country's chart from Upload-Post, normalised."""
    key = config.secret("UPLOAD_POST_API_KEY")
    if not key:
        raise TrendingError("UPLOAD_POST_API_KEY is not set in .env (the trending chart comes from Upload-Post)")
    tcfg = cfg["trending"]
    resp = requests.get(API, headers={"Authorization": f"Apikey {key}"}, timeout=60, params={
        "profile": cfg["upload_post"]["profile"], "country_code": country,
        "date_range": tcfg["date_range"], "genre": "ALL"})
    if resp.status_code == 409:
        raise TrendingError("Upload-Post says the TikTok login expired: reconnect TikTok on app.upload-post.com")
    if resp.status_code >= 400:
        raise TrendingError(f"Upload-Post {resp.status_code}: {resp.text[:300]} (the TikTok connection may "
                            "need the 'music' permission: reconnect it on app.upload-post.com)")
    body = resp.json()
    items = body if isinstance(body, list) else (body.get("music") or body.get("tracks")
                                                  or body.get("data") or body.get("results") or [])
    tracks = []
    for n, item in enumerate(items, start=1):
        if not item.get("id"):
            continue
        tracks.append({
            "id": str(item["id"]), "title": item.get("title") or "?", "artist": item.get("artist") or "",
            "duration": item.get("duration"), "rank": item.get("rank") or n,
            "genres": item.get("genres") or [], "cover_url": item.get("cover_url") or "",
            "preview_url": item.get("preview_url") or "", "country": country,
        })
    return tracks


def charts(cfg: dict = None, refresh: bool = False) -> dict:
    """{country: [tracks]} for every configured country, cached for 12 hours. A country
    whose fetch fails keeps its last cached chart (marked stale) rather than vanishing."""
    cfg = cfg or config.load()
    out = {}
    for country in cfg["trending"]["countries"]:
        path = folder() / f"chart_{country}.json"
        cached = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        if cached and not refresh and time.time() - cached["fetched_at"] < CHART_MAX_AGE_S:
            out[country] = cached
            continue
        try:
            fresh = {"fetched_at": time.time(), "tracks": fetch_chart(country, cfg), "error": None}
            path.write_text(json.dumps(fresh, ensure_ascii=False), encoding="utf-8")
            out[country] = fresh
        except Exception as err:  # noqa: BLE001
            log.warning("Trending chart %s: %s", country, err)
            out[country] = {**(cached or {"fetched_at": 0, "tracks": []}), "error": str(err)}
    return out


def cached_charts(cfg: dict = None) -> dict:
    """Whatever charts are already on disk, without fetching (for page loads)."""
    cfg = cfg or config.load()
    out = {}
    for country in cfg["trending"]["countries"]:
        path = folder() / f"chart_{country}.json"
        if path.exists():
            out[country] = json.loads(path.read_text(encoding="utf-8"))
    return out


def find_track(track_id: str, chart_data: dict) -> dict:
    """The track with this id, with every country it's trending in."""
    found = None
    for country, chart in chart_data.items():
        for track in chart.get("tracks") or []:
            if track["id"] == track_id:
                found = found or {**track, "countries": []}
                found["countries"].append({"country": country, "rank": track["rank"]})
    return found


def candidates(chart_data: dict, per_country: int) -> list:
    """The top tracks of each chart, merged: a song trending in two countries appears once."""
    merged = {}
    for country, chart in chart_data.items():
        for track in sorted(chart.get("tracks") or [], key=lambda t: t["rank"])[:per_country]:
            entry = merged.setdefault(track["id"], {**track, "countries": []})
            entry["countries"].append({"country": country, "rank": track["rank"]})
    return list(merged.values())


def label(track: dict) -> str:
    where = ", ".join(f"#{c['rank']} in {c['country']}" for c in track.get("countries") or [])
    return f"\"{track['title']}\" by {track['artist'] or 'unknown'}" + (f" (trending {where})" if where else "")


# ----------------------------------------------------------------- previews

def preview_file(track: dict) -> Path:
    """The track's preview, downloaded once and kept (also played in the dashboard)."""
    path = folder() / "previews" / f"{track['id']}.mp3"
    if not path.exists():
        if not track.get("preview_url"):
            raise TrendingError("This track has no preview")
        with requests.get(track["preview_url"], timeout=60, stream=True) as resp:
            resp.raise_for_status()
            tmp = path.with_suffix(".part")
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(1 << 16):
                    f.write(chunk)
            tmp.replace(path)
    return path


def _gemini_snippet(track: dict) -> bytes:
    """A short mono copy of the preview for Gemini to listen to (keeps the request small)."""
    src = preview_file(track)
    dst = src.with_name(f"{track['id']}_gemini.mp3")
    if not dst.exists():
        media.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-t", str(PREVIEW_SECONDS),
                   "-ac", "1", "-ar", "22050", "-b:a", "48k", dst])
    return dst.read_bytes()


# ----------------------------------------------------------------- matching

# no ge/le/max_length here: Vertex rejects schemas that carry many limits (see highlight.py)
class Pick(BaseModel):
    sound: str = Field(description="The label of the sound, e.g. S3")
    reason: str = Field(description="One sentence: why this sound fits this clip")
    fit: int = Field(description="How well it fits, from 1 to 10")
    start_s: float = Field(description="Seconds into the sound's preview where the best part to use starts")


class Picks(BaseModel):
    picks: list[Pick] = Field(description="The best 3 sounds for this clip, best first")


PROMPT = """You are a TikTok editor choosing a trending sound for this clip (the first video).
After it come short previews of trending TikTok sounds, each introduced by its label (S1, S2, ...).
Listen to each one. Pick the 3 that would make THIS clip most engaging: match its mood, energy and
pacing, leave room for the speaker's voice (the sound plays under the original audio), and avoid
lyrics that clash with what's being said. Clip: {title!r}. What happens: {reason!r}. Mood: {mood}."""


def _clip_video_part(job_dir: Path):
    from google.genai import types

    base = job_dir / "transformed.mp4"
    if not base.exists():
        base = job_dir / "highlight.mp4"
    proxy = job_dir / "sound_proxy.mp4"
    media.make_proxy(base, proxy, height=360, crf=34, fps=1, audio_kbps=32)
    return types.Part(inline_data=types.Blob(data=proxy.read_bytes(), mime_type="video/mp4"),
                      video_metadata=types.VideoMetadata(fps=1.0))


def suggest(job: dict, cfg: dict, client=None) -> dict:
    """Ask Gemini for the 3 best trending sounds for this clip. Returns the `sound` dict
    stored on the job (suggestions plus any earlier choice)."""
    from google.genai import types

    from .highlight import make_client

    chart_data = charts(cfg)
    pool = candidates(chart_data, int(cfg["trending"]["candidates_per_country"]))
    if not pool:
        errors = "; ".join(c.get("error") or "" for c in chart_data.values() if c.get("error"))
        raise TrendingError(errors or "The trending charts are empty")

    clip = job.get("clip") or {}
    contents = [_clip_video_part(config.job_dir(job["video_id"])),
                PROMPT.format(title=(job.get("info") or {}).get("title", ""), reason=clip.get("reason", ""),
                              mood=clip.get("music_mood", "unknown"))]
    labelled = {}
    for track in pool:
        try:
            audio = _gemini_snippet(track)
        except Exception as err:  # noqa: BLE001 - a missing preview just drops that track
            log.info("No preview for %s: %s", track["id"], err)
            continue
        name = f"S{len(labelled) + 1}"
        labelled[name] = track
        contents += [f"{name}: {label(track)}", types.Part(inline_data=types.Blob(data=audio, mime_type="audio/mp3"))]
    if not labelled:
        raise TrendingError("None of the trending sounds had a preview to listen to")

    client = client or make_client(cfg)
    resp = client.models.generate_content(
        model=cfg["gemini"]["model"], contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=Picks,
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
    parsed = resp.parsed or Picks.model_validate_json(resp.text)

    suggestions, seen = [], set()
    for pick in parsed.picks:
        track = labelled.get(pick.sound.strip().upper())
        if not track or track["id"] in seen:
            continue
        seen.add(track["id"])
        suggestions.append({**track, "reason": pick.reason, "fit": min(10, max(1, int(pick.fit))),
                            "start_s": max(0.0, round(float(pick.start_s), 1))})
    previous = job.get("sound") or {}
    return {**previous, "status": "ready", "error": None, "suggestions": suggestions[:3],
            "suggested_at": time.time(), "listened_to": len(labelled)}


def flag(country: str) -> str:
    return FLAGS.get(country, country)


# --------------------------------------------- without Upload-Post: web search

class WebSound(BaseModel):
    title: str = Field(description="The sound or song title as it appears on TikTok")
    artist: str = Field(description="Artist or creator of the sound, empty if unknown")
    country: str = Field(description="Two-letter country code where it is trending")
    why: str = Field(description="One short line on how people are using it")


class WebChart(BaseModel):
    sounds: list[WebSound] = Field(description="The trending TikTok sounds, most popular first")


SEARCH_PROMPT = """Search the web for the TikTok sounds and songs trending THIS WEEK (today is {today})
in these countries: {countries}. Use recent sources (TikTok Creative Center trend pages, music
charts, news). For each country list about {n} sounds with title, artist and how people use them."""


def web_chart(cfg: dict = None, refresh: bool = False, client=None) -> dict:
    """This week's trending sounds, found by Gemini with Google Search (no Upload-Post
    needed). Cached for 12 hours. {"fetched_at", "tracks": [...], "error"}."""
    from google.genai import types

    from .highlight import make_client

    cfg = cfg or config.load()
    path = folder() / "web_chart.json"
    cached = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if cached and not refresh and time.time() - cached["fetched_at"] < CHART_MAX_AGE_S:
        return cached
    try:
        client = client or make_client(cfg)
        countries = cfg["trending"]["countries"]
        found = client.models.generate_content(
            model=cfg["gemini"]["model"],
            contents=SEARCH_PROMPT.format(today=time.strftime("%Y-%m-%d"), countries=", ".join(countries),
                                          n=cfg["trending"]["candidates_per_country"]),
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
        # Search and strict JSON in one call isn't reliable on every model: structure it separately.
        structured = client.models.generate_content(
            model=cfg["gemini"]["model"],
            contents=[found.text or "", "Turn the research above into the list. Only include sounds it names."],
            config=types.GenerateContentConfig(
                response_mime_type="application/json", response_schema=WebChart,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
        chart = structured.parsed or WebChart.model_validate_json(structured.text)
        tracks, rank = [], {}
        for sound in chart.sounds:
            country = (sound.country or "").upper()[:2] or "??"
            rank[country] = rank.get(country, 0) + 1
            slug = re.sub(r"[^a-z0-9]+", "-", f"{sound.title} {sound.artist}".lower()).strip("-")[:60]
            tracks.append({"id": f"web-{slug}", "title": sound.title, "artist": sound.artist, "country": country,
                           "rank": rank[country], "why": sound.why, "preview_url": "", "source": "web",
                           "search_url": "https://www.tiktok.com/search?" + urlencode(
                               {"q": f"{sound.title} {sound.artist}".strip()})})
        fresh = {"fetched_at": time.time(), "tracks": tracks, "error": None}
        path.write_text(json.dumps(fresh, ensure_ascii=False), encoding="utf-8")
        return fresh
    except Exception as err:  # noqa: BLE001
        log.warning("Web trending search failed: %s", err)
        return {**(cached or {"fetched_at": 0, "tracks": []}), "error": str(err)}


def cached_web_chart() -> dict:
    path = folder() / "web_chart.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


WEB_PROMPT = """You are a TikTok editor choosing a trending sound for this clip (the video above).
Below are sounds trending on TikTok this week. Pick the 3 that would make THIS clip most engaging:
match its mood, energy and pacing, and leave room for the speaker's voice (the sound plays under it).
Clip: {title!r}. What happens: {reason!r}. Mood: {mood}. Use the labels (S1, S2, ...) in your answer.

{sounds}"""


def suggest_web(job: dict, cfg: dict, client=None) -> dict:
    """Gemini watches the clip and picks from this week's web-searched trending sounds."""
    from google.genai import types

    from .highlight import make_client

    client = client or make_client(cfg)
    chart = web_chart(cfg, client=client)
    pool = chart.get("tracks") or []
    if not pool:
        raise TrendingError(chart.get("error") or "Couldn't find this week's trending sounds")
    labelled = {f"S{n}": t for n, t in enumerate(pool, start=1)}
    listing = "\n".join(f"{name}: \"{t['title']}\" by {t['artist'] or 'unknown'} (trending in {t['country']}; "
                        f"{t['why']})" for name, t in labelled.items())
    clip = job.get("clip") or {}
    resp = client.models.generate_content(
        model=cfg["gemini"]["model"],
        contents=[_clip_video_part(config.job_dir(job["video_id"])),
                  WEB_PROMPT.format(title=(job.get("info") or {}).get("title", ""), reason=clip.get("reason", ""),
                                    mood=clip.get("music_mood", "unknown"), sounds=listing)],
        config=types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=Picks,
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
    parsed = resp.parsed or Picks.model_validate_json(resp.text)
    suggestions, seen = [], set()
    for pick in parsed.picks:
        track = labelled.get(pick.sound.strip().upper())
        if not track or track["id"] in seen:
            continue
        seen.add(track["id"])
        suggestions.append({**track, "countries": [{"country": track["country"], "rank": track["rank"]}],
                            "reason": pick.reason, "fit": min(10, max(1, int(pick.fit))), "start_s": 0.0})
    previous = job.get("sound") or {}
    return {**previous, "status": "ready", "error": None, "suggestions": suggestions[:3],
            "suggested_at": time.time(), "listened_to": 0, "source": "web"}


def suggest_any(job: dict, cfg: dict) -> dict:
    """Listen to Upload-Post's licensed chart when that's how we post; otherwise use web search."""
    if cfg["posting"]["service"] == "upload_post" and config.secret("UPLOAD_POST_API_KEY"):
        return suggest(job, cfg)
    return suggest_web(job, cfg)
