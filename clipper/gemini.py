"""One Gemini client for highlights and sound suggestions, with a fallback.

Two ways in, tried in the order of `gemini.backends` (config.yaml):
- "vertex":   your Google Cloud project and service-account key (paid, from Cloud billing)
- "aistudio": a Gemini API key from aistudio.google.com (GEMINI_API_KEY in .env). Free when
  the key's project has no billing linked; Google may then use what you send to improve its
  products. No Google Search on the free tier, so web-searched sound charts need Vertex.

When one fails for an account reason (no credit, quota used up, billing off, key refused)
the call is repeated on the next, and the failed one is skipped for a while so every call
doesn't wait on it first. Other errors (a bad request) are raised as they are.
"""

import logging
import threading
import time

from . import config

log = logging.getLogger(__name__)

BACKENDS = ("vertex", "aistudio")
SKIP_FAILED_S = 30 * 60
_down = {}  # backend -> (until, reason)
_lock = threading.Lock()

ACCOUNT_WORDS = ("billing", "quota", "prepay", "credit", "permission_denied", "permission denied",
                 "api key not valid", "api_key_invalid", "unauthenticated", "free tier")
TEMPORARY_WORDS = ("deadline_exceeded", "deadline expired", "unavailable", "internal error", "overloaded")
SHORT_REST_S = 60  # a plain "busy" 429: rest briefly, it's not the account


class NoGemini(RuntimeError):
    pass


def account_problem(err: Exception) -> bool:
    """Is this a 'you can't use this account right now' error (try another) rather than
    'this request is wrong' or 'Google is busy' (another account wouldn't help, or the
    normal retry will)? A timeout must not send your clips to the free tier."""
    code = getattr(err, "code", None) or getattr(err, "status_code", None)
    text = str(err).lower()
    if code in (500, 502, 503, 504) or any(w in text for w in TEMPORARY_WORDS):
        return False
    if code in (401, 402, 403):
        return True
    if any(w in text for w in ACCOUNT_WORDS):
        return True
    return code == 429  # quota-less 429: still worth the other route, but only briefly (see rest_for)


def rest_for(err: Exception) -> float:
    """How long to skip a route after an account problem."""
    text = str(err).lower()
    code = getattr(err, "code", None) or getattr(err, "status_code", None)
    if code == 429 and not any(w in text for w in ACCOUNT_WORDS):
        return SHORT_REST_S
    return SKIP_FAILED_S


def available(cfg: dict) -> list:
    """The backends that are set up, in the configured order."""
    gcfg = cfg["gemini"]
    order = [b for b in (gcfg.get("backends") or BACKENDS) if b in BACKENDS]
    ready = []
    for name in order:
        if name == "vertex" and gcfg.get("project_id") and gcfg.get("service_account_key"):
            ready.append(name)
        elif name == "aistudio" and config.secret("GEMINI_API_KEY"):
            ready.append(name)
    return ready


def _raw_client(name: str, cfg: dict):
    from google import genai

    if name == "aistudio":
        return genai.Client(api_key=config.secret("GEMINI_API_KEY"))
    from google.oauth2 import service_account

    gcfg = cfg["gemini"]
    creds = service_account.Credentials.from_service_account_file(
        gcfg["service_account_key"], scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return genai.Client(vertexai=True, project=gcfg["project_id"], location=gcfg["location"],
                        credentials=creds)


def status(cfg: dict) -> list:
    """[{name, ready, skipped_until, reason}] for the dashboard."""
    now = time.time()
    ready = available(cfg)
    out = []
    for name in (cfg["gemini"].get("backends") or BACKENDS):
        until, reason = _down.get(name, (0, ""))
        out.append({"name": name, "ready": name in ready,
                    "skipped_until": until if until > now else None, "reason": reason if until > now else ""})
    return out


class _Models:
    def __init__(self, owner):
        self._owner = owner

    def __getattr__(self, method):
        def call(*args, **kwargs):
            return self._owner._call(method, args, kwargs)
        return call


class Client:
    """Looks like genai.Client for what Clipper uses (`.models.<method>(...)`)."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.models = _Models(self)
        self.used = None  # the backend that answered last
        # One genai.Client per route, kept alive while this Client lives: google-genai closes
        # its HTTP connection when a genai.Client is garbage-collected, so taking `.models`
        # from a throwaway one fails with "the client has been closed".
        self._raw = {}

    def _client(self, name: str):
        if name not in self._raw:
            self._raw[name] = _raw_client(name, self.cfg)
        return self._raw[name]

    def _call(self, method: str, args, kwargs):
        names = available(self.cfg)
        if not names:
            raise NoGemini("No Gemini access is set up: add your Vertex project and key in config.yaml, "
                           "or a free Gemini API key on the Connections page")
        now = time.time()
        live = [n for n in names if _down.get(n, (0, ""))[0] <= now]
        # if every backend is resting, try them anyway rather than fail without asking
        errors = []
        for name in live or names:
            try:
                result = getattr(self._client(name).models, method)(*args, **kwargs)
            except Exception as err:  # noqa: BLE001
                if not account_problem(err):
                    raise
                with _lock:
                    _down[name] = (time.time() + rest_for(err), str(err)[:300])
                log.warning("Gemini via %s unavailable (%s); trying the next one", name, str(err)[:200])
                errors.append(f"{name}: {str(err)[:200]}")
                continue
            with _lock:
                _down.pop(name, None)
            if self.used != name:
                log.info("Gemini via %s", name)
            self.used = name
            return result
        raise NoGemini("Every Gemini route refused: " + " | ".join(errors))


def make_client(cfg: dict) -> Client:
    return Client(cfg)
