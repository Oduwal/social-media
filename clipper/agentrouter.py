"""Agent Router: an OpenAI-compatible gateway to Claude and OpenAI models (agentrouter.org).

Used as an alternative highlight engine: instead of Gemini watching the video, a Claude/OpenAI
model reads a timestamped transcript of it and picks the moments. Only the chat call lives here;
the highlight prompt and parsing stay in highlight.py so both engines share the same schema.
"""

import json
import logging
import time

import requests

from . import config, gemini

log = logging.getLogger(__name__)

TIMEOUT = 180


class AgentRouterError(RuntimeError):
    pass


def settings(cfg: dict) -> dict:
    return cfg.get("highlight", {}).get("agentrouter", {}) or {}


def available(cfg: dict) -> bool:
    s = settings(cfg)
    return bool(config.secret("AGENTROUTER_API_KEY") and s.get("base_url") and s.get("model"))


def status(cfg: dict) -> dict:
    s = settings(cfg)
    return {"engine": cfg.get("highlight", {}).get("engine", "gemini"),
            "key": bool(config.secret("AGENTROUTER_API_KEY")),
            "base_url": s.get("base_url", ""), "model": s.get("model", ""),
            "ready": available(cfg)}


def chat_json(cfg: dict, system: str, user: str, max_tokens: int = 8000) -> str:
    """One chat completion that must return JSON; retries temporary errors like gemini does.
    Returns the assistant's message content (a JSON string)."""
    if not available(cfg):
        raise AgentRouterError("Agent Router isn't set up: paste your key on the Connections page "
                               "and set highlight.agentrouter base_url and model in config.yaml")
    s = settings(cfg)
    url = s["base_url"].rstrip("/") + "/chat/completions"
    body = {"model": s["model"], "temperature": 0.3, "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    headers = {"Authorization": f"Bearer {config.secret('AGENTROUTER_API_KEY')}",
               "Content-Type": "application/json"}
    last = ""
    for attempt in range(len(gemini.TRANSIENT_BACKOFF) + 1):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=TIMEOUT)
        except requests.RequestException as err:
            last = str(err)
            if attempt < len(gemini.TRANSIENT_BACKOFF):
                time.sleep(gemini.TRANSIENT_BACKOFF[attempt])
                continue
            raise AgentRouterError(f"Couldn't reach Agent Router: {err}") from err
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < len(gemini.TRANSIENT_BACKOFF):
            log.info("Agent Router %s: temporary, retrying in %ds", resp.status_code,
                     gemini.TRANSIENT_BACKOFF[attempt])
            time.sleep(gemini.TRANSIENT_BACKOFF[attempt])
            last = f"HTTP {resp.status_code}: {resp.text[:200]}"
            continue
        if resp.status_code == 401:
            raise AgentRouterError("Agent Router rejected the key (401): check it on the Connections page")
        if resp.status_code != 200:
            raise AgentRouterError(f"Agent Router HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError) as err:
            raise AgentRouterError(f"Agent Router sent an unexpected response: {resp.text[:300]}") from err
        return content
    raise AgentRouterError(f"Agent Router kept failing: {last}")
