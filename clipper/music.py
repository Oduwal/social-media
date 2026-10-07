"""Music library. The mixing itself happens in render.py.

Tracks live in music/. An optional music/library.json tags them with a mood so
the track Gemini's suggested mood maps to can be pre-selected:
    {"upbeat.mp3": {"mood": "hype", "title": "Upbeat Drums"}}
"""

import json
from pathlib import Path

from . import config

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}


def list_tracks(music_dir: Path = None) -> list:
    music_dir = Path(music_dir or config.MUSIC_DIR)
    music_dir.mkdir(parents=True, exist_ok=True)
    meta = {}
    library = music_dir / "library.json"
    if library.exists():
        meta = json.loads(library.read_text(encoding="utf-8"))
    tracks = []
    for path in sorted(music_dir.iterdir()):
        if path.suffix.lower() in AUDIO_EXTS:
            info = meta.get(path.name, {})
            tracks.append({"file": path.name, "title": info.get("title") or path.stem,
                           "mood": info.get("mood", "")})
    return tracks


def pick_for_mood(mood: str, music_dir: Path = None) -> str:
    tracks = list_tracks(music_dir)
    for track in tracks:
        if track["mood"] and track["mood"].lower() == (mood or "").lower():
            return track["file"]
    return tracks[0]["file"] if tracks else None


def track_path(name: str, music_dir: Path = None) -> Path:
    music_dir = Path(music_dir or config.MUSIC_DIR)
    path = (music_dir / name).resolve()
    if path.parent != music_dir.resolve() or not path.exists():
        raise FileNotFoundError(f"No such track: {name}")
    return path
