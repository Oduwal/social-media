"""Clipper's own posting to TikTok, Instagram and X (no third-party posting service).

Each platform module offers:
    status(cfg)              -> {"connected": bool, "account": str, "missing": [...], ...}
    post(job, video, caption, ai_label, cfg, on_submitted) -> result
    check(handle, cfg)       -> result, or None while the platform is still processing

A result is {"ok", "url", "message", "drafted", "manual"}. `on_submitted(handle)` is
called as soon as the platform has the upload, so a crash or dropped connection after
that point is followed by a status check, never a second upload.
"""

import json
import threading
import time

from .. import config

_lock = threading.Lock()


class PostError(RuntimeError):
    def __init__(self, message: str, definite: bool = False):
        super().__init__(message)
        # True when the platform clearly refused (nothing was posted): safe to try again
        self.definite = definite


def _path():
    return config.DATA_DIR / "connections.json"


def load_all() -> dict:
    with _lock:
        path = _path()
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def load(platform: str) -> dict:
    return load_all().get(platform) or {}


def save(platform: str, data: dict) -> None:
    """Tokens are logins: they stay in data/ (never shared), one file for all platforms."""
    with _lock:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        allc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if data:
            allc[platform] = {**data, "saved_at": time.time()}
        else:
            allc.pop(platform, None)
        path.write_text(json.dumps(allc, indent=2), encoding="utf-8")


def result(ok: bool, url: str = "", message: str = "", drafted: bool = False) -> dict:
    return {"ok": ok, "url": url, "message": message, "drafted": drafted, "skipped": False}


def module(platform: str):
    from . import instagram, tiktok, x
    return {"tiktok": tiktok, "instagram": instagram, "x": x}[platform]
