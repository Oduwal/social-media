"""Paths and settings. Settings come from config.yaml (merged over DEFAULTS) and secrets from .env."""

import copy
import json
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
JOBS_DIR = DATA_DIR / "jobs"
MUSIC_DIR = ROOT / "music"
DB_PATH = DATA_DIR / "clipper.db"
LOG_PATH = DATA_DIR / "clipper.log"
CONFIG_PATH = ROOT / "config.yaml"

DEFAULTS = {
    "creator": {"handle": "", "credit": "", "hashtags": []},
    "monitor": {"enabled": True, "interval_minutes": 20, "jitter": 0.25,
                "playlist_end": 10, "auto_update_ytdlp": True},
    "clip": {"min_seconds": 15, "max_seconds": 45},
    "gemini": {"project_id": "", "location": "global", "model": "gemini-3.5-flash",
               # tried in order when `model` fails on every key (overloaded, or its free quota is used up;
               # the free tier counts quota per model, so another model may still have room)
               "fallback_models": ["gemini-3.6-flash"],
               "service_account_key": "", "backends": ["vertex", "aistudio"]},
    # Which engine picks highlights: "gemini" watches the video; "agentrouter" reads a transcript
    # of it (Claude/OpenAI via the Agent Router gateway, an OpenAI-compatible API).
    "highlight": {"engine": "gemini",
                  "agentrouter": {"base_url": "https://agentrouter.org/v1", "model": "claude-sonnet-4-5-20250929"}},
    "transform": {"enabled": True, "script_path": "", "variant": "albinism", "model": "",
                  "strict_look": False, "reference": False, "colour_match": True, "instruction": "",
                  "resolution": "", "backend": "vertex"},
    # The Google Flow robot (transform: backend: "flow"); see flow_robot.py
    "flow": {"chrome_path": "", "debug_port": 9333, "project_url": "", "min_gap_s": 60, "max_gap_s": 180,
             "clip_gap_s": 20, "piece_gap_s": 3,
             "daily_max_edits": 15, "cooldown_minutes": 60, "timeout_minutes": 12, "upload_timeout_minutes": 6},
    # auto_finish: on = send every cut clip straight through to READY (no manual pick);
    # off (default) = clips wait in review (PREVIEW) for you to pick which to keep.
    "pipeline": {"auto_finish": False},
    # Translucent text watermark burned onto the posted video (e.g. your @username).
    "watermark": {"enabled": False, "text": "", "opacity": 0.35, "position": "bottom-right", "size": 4},
    "music": {"default_volume": 0.18, "ducking": True},
    "upload_post": {"profile": "", "platforms": ["tiktok", "instagram", "x"],
                    "tiktok_privacy_level": "SELF_ONLY", "ai_label": True},
    "dashboard": {"host": "127.0.0.1", "port": 8000, "open_browser": True},
    "shotcut": {"path": ""},
    "editor": "clipper",           # "clipper" (timeline editor), "simple" (the old one), "studio" (HyperFrames)
    "studio": {"port": 3020},
    "downloader": {"cookies_file": "", "cookies_from_browser": ""},
    "trending": {"enabled": True, "countries": ["US", "NG"], "date_range": "7DAY",
                 "candidates_per_country": 10, "sound_volume": 35, "original_volume": 100},
    "posting": {"service": "direct", "tiktok_mode": "inbox", "graph_api_version": "v24.0"},
}

# Secrets the Connections page can save into .env
SECRET_NAMES = ("UPLOAD_POST_API_KEY", "APIFY_TOKEN", "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET",
                "META_APP_ID", "META_APP_SECRET", "X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN",
                "X_ACCESS_TOKEN_SECRET", "GEMINI_API_KEY", "AGENTROUTER_API_KEY")

PLATFORMS = ("tiktok", "instagram", "x")

load_dotenv(ROOT / ".env")


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


class ConfigError(ValueError):
    pass


def load() -> dict:
    """Read config.yaml fresh every call, so edits apply without restarting long-running loops."""
    user = {}
    if CONFIG_PATH.exists():
        try:
            user = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as err:
            mark = getattr(err, "problem_mark", None)
            where = f" around line {mark.line + 1}" if mark else ""
            raise ConfigError(
                f"config.yaml has a typo{where}. Common causes: a # (hashtag) without quotes "
                f'around it (write "#fyp"), or a missing space after a colon.\nDetails: {err}') from err
        if not isinstance(user, dict):
            raise ConfigError("config.yaml should contain settings like 'creator:' and 'monitor:'")
    cfg = _merge(DEFAULTS, user)
    handle = (cfg["creator"]["handle"] or "").lstrip("@").strip()
    cfg["creator"]["handle"] = handle
    if not cfg["creator"]["credit"] and handle:
        cfg["creator"]["credit"] = f"🎥 @{handle}"

    # Things the dashboard can change without editing config.yaml live in data/ui.json.
    ui = ui_get()
    if "use_vertex" in ui:  # the Vertex on/off button
        cfg["gemini"]["backends"] = ["vertex", "aistudio"] if ui["use_vertex"] else ["aistudio"]
    if isinstance(ui.get("watermark"), dict):  # the watermark card
        cfg["watermark"] = {**cfg["watermark"], **ui["watermark"]}

    # The creators being watched: the dashboard's list wins; otherwise config.yaml's
    # 'creators:' list, otherwise the single 'creator:' (so old configs keep working).
    if isinstance(ui.get("creators"), list):
        source = ui["creators"]
    elif isinstance(user.get("creators"), list):
        source = user["creators"]
    else:
        source = [cfg["creator"]] if handle else []
    cfg["creators"] = [creator_entry(c.get("handle"), c.get("credit"), c.get("hashtags"),
                                     c.get("enabled", True)) for c in source if (c.get("handle") or "").strip()]
    if cfg["creators"]:
        cfg["creator"] = {k: cfg["creators"][0][k] for k in ("handle", "credit", "hashtags")}
    return cfg


def creator_for(cfg: dict, handle: str) -> dict:
    """The creator a video (by its handle) belongs to, for its credit and hashtags; falls back
    to the first creator for videos saved before multi-creator, or added by link."""
    handle = (handle or "").lstrip("@").strip().lower()
    for c in cfg.get("creators") or []:
        if c["handle"].lower() == handle:
            return c
    return (cfg.get("creators") or [cfg["creator"]])[0]


def creator_entry(handle: str, credit: str = "", hashtags=None, enabled: bool = True) -> dict:
    handle = (handle or "").lstrip("@").strip()
    return {"handle": handle, "credit": (credit or "").strip() or (f"🎥 @{handle}" if handle else ""),
            "hashtags": list(hashtags or []), "enabled": bool(enabled)}


UI_PATH = DATA_DIR / "ui.json"


def ui_get() -> dict:
    try:
        return json.loads(UI_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def ui_set(**fields) -> dict:
    data = {**ui_get(), **fields}
    UI_PATH.parent.mkdir(parents=True, exist_ok=True)
    UI_PATH.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return data


def save_creators(creators: list) -> None:
    """Store the dashboard's creator list (from then on it's the source, not config.yaml)."""
    ui_set(creators=[creator_entry(c.get("handle"), c.get("credit"), c.get("hashtags"),
                                   c.get("enabled", True)) for c in creators if (c.get("handle") or "").strip()])


def add_creator(handle: str, credit: str = "", hashtags=None) -> None:
    handle = (handle or "").lstrip("@").strip()
    if not handle:
        raise ValueError("Enter a TikTok handle")
    creators = [c for c in load()["creators"] if c["handle"].lower() != handle.lower()]
    creators.append(creator_entry(handle, credit, hashtags))
    save_creators(creators)


def remove_creator(handle: str) -> None:
    handle = (handle or "").lstrip("@").strip().lower()
    save_creators([c for c in load()["creators"] if c["handle"].lower() != handle])


def set_creator_enabled(handle: str, enabled: bool) -> None:
    handle = (handle or "").lstrip("@").strip().lower()
    creators = load()["creators"]
    for c in creators:
        if c["handle"].lower() == handle:
            c["enabled"] = enabled
    save_creators(creators)


def secret(name: str) -> str:
    return os.environ.get(name, "").strip()


def set_secret(name: str, value: str) -> None:
    """Write NAME=value into .env (replacing an existing line) and the running process."""
    if name not in SECRET_NAMES:
        raise ValueError(f"Not a setting the dashboard may change: {name}")
    value = value.strip().replace("\n", "").replace("\r", "")
    path = ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [line for line in lines if not line.startswith(f"{name}=")] + [f"{name}={value}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[name] = value


def job_dir(video_id: str) -> Path:
    path = JOBS_DIR / video_id
    path.mkdir(parents=True, exist_ok=True)
    return path
