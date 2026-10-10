"""The Agent Router highlight engine: the transcript path and the HTTP client, all offline
(requests and transcription are faked, so no key is used and nothing is sent)."""

import json

import pytest

from clipper import agentrouter, config, highlight


def _cfg(**highlight_over):
    cfg = config._merge(config.DEFAULTS, {"creator": {"handle": "someone"}})
    cfg["highlight"].update(highlight_over)
    return cfg


WORDS = [{"text": "Time", "start": 0.2, "end": 0.5}, {"text": "to", "start": 0.6, "end": 0.7},
         {"text": "go.", "start": 0.8, "end": 1.0}, {"text": "What", "start": 4.0, "end": 4.3},
         {"text": "is", "start": 4.4, "end": 4.5}, {"text": "a", "start": 4.6, "end": 4.7},
         {"text": "status?", "start": 4.8, "end": 5.3}]

ANSWER = {"video_duration_s": 30.0, "clips": [
    {"title": "Time to go", "spoken_words": "Time to go.", "reason": "punchy opener", "hook": "Time to go",
     "start_mmss": "00:00", "end_mmss": "00:06", "start_s": 0.0, "end_s": 6.0, "score": 7,
     "people_count": 1, "creator_description": "", "caption": "Let's move", "hashtags": ["#fyp"],
     "music_mood": "hype"},
    {"title": "Status question", "spoken_words": "What is a status?", "reason": "relatable", "hook": "What is a status",
     "start_mmss": "00:10", "end_mmss": "00:20", "start_s": 10.0, "end_s": 20.0, "score": 8,
     "people_count": 1, "creator_description": "", "caption": "Define it", "hashtags": ["#dating"],
     "music_mood": "funny"}]}


# ------------------------------------------------------------------ helpers

def test_transcript_lines_group_on_pauses_and_sentences():
    out = highlight.transcript_lines(WORDS)
    assert out == "[0.2] Time to go.\n[4.0] What is a status?"


def test_json_block_handles_fences_and_prose():
    assert json.loads(highlight._json_block('```json\n{"a": 1}\n```')) == {"a": 1}
    assert json.loads(highlight._json_block('Sure! Here it is: {"a": 2} hope that helps')) == {"a": 2}
    assert json.loads(highlight._json_block('{"a": 3}')) == {"a": 3}


def test_available_and_status(monkeypatch):
    monkeypatch.delenv("AGENTROUTER_API_KEY", raising=False)
    assert not agentrouter.available(_cfg())
    monkeypatch.setenv("AGENTROUTER_API_KEY", "k")
    cfg = _cfg(engine="agentrouter")
    assert agentrouter.available(cfg)
    st = agentrouter.status(cfg)
    assert st["ready"] and st["engine"] == "agentrouter" and "agentrouter.org" in st["base_url"]


# ------------------------------------------------------------------ the HTTP client

class Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _ok(content):
    return Resp(200, {"choices": [{"message": {"content": content}}]})


def test_chat_json_returns_content(monkeypatch):
    monkeypatch.setenv("AGENTROUTER_API_KEY", "k")
    sent = {}

    def post(url, headers, json, timeout):
        sent["url"], sent["model"], sent["auth"] = url, json["model"], headers["Authorization"]
        return _ok('{"clips": []}')

    monkeypatch.setattr(agentrouter.requests, "post", post)
    out = agentrouter.chat_json(_cfg(engine="agentrouter"), "sys", "user")
    assert out == '{"clips": []}'
    assert sent["url"].endswith("/v1/chat/completions") and sent["auth"] == "Bearer k"


def test_chat_json_retries_503_then_succeeds(monkeypatch):
    monkeypatch.setenv("AGENTROUTER_API_KEY", "k")
    monkeypatch.setattr(agentrouter.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def post(url, headers, json, timeout):
        calls["n"] += 1
        return _ok('{"ok": 1}') if calls["n"] >= 2 else Resp(503, text="overloaded")

    monkeypatch.setattr(agentrouter.requests, "post", post)
    assert agentrouter.chat_json(_cfg(engine="agentrouter"), "s", "u") == '{"ok": 1}'
    assert calls["n"] == 2


def test_chat_json_rejects_bad_key(monkeypatch):
    monkeypatch.setenv("AGENTROUTER_API_KEY", "k")
    monkeypatch.setattr(agentrouter.requests, "post", lambda url, **kw: Resp(401, text="unauthorized"))
    with pytest.raises(agentrouter.AgentRouterError, match="401"):
        agentrouter.chat_json(_cfg(engine="agentrouter"), "s", "u")


def test_chat_json_needs_setup(monkeypatch):
    monkeypatch.delenv("AGENTROUTER_API_KEY", raising=False)
    with pytest.raises(agentrouter.AgentRouterError, match="isn't set up"):
        agentrouter.chat_json(_cfg(engine="agentrouter"), "s", "u")


# ------------------------------------------------------------------ the transcript highlight path

def test_pick_via_transcript_returns_clean_clips(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTROUTER_API_KEY", "k")
    monkeypatch.setattr(highlight.media, "duration", lambda p: 30.0)
    from clipper import captions
    monkeypatch.setattr(captions, "words_for", lambda video, cache: WORDS)
    seen = {}

    def chat_json(cfg, system, user, max_tokens=8000):
        seen["user"] = user
        return "```json\n" + json.dumps(ANSWER) + "\n```"

    monkeypatch.setattr(agentrouter, "chat_json", chat_json)
    src = tmp_path / "source.mp4"
    src.write_bytes(b"x")
    data = highlight.pick(src, tmp_path, _cfg(engine="agentrouter"), caption="my caption")
    assert data["engine"] == "agentrouter" and len(data["clips"]) == 2
    assert data["clips"][0]["start_s"] == 0.0 and data["clips"][1]["start_s"] == 10.0  # timeline order
    assert "[0.2] Time to go." in seen["user"] and "my caption" in seen["user"]
    assert json.loads((tmp_path / "highlight.json").read_text())["engine"] == "agentrouter"


def test_pick_dispatches_by_engine(tmp_path, monkeypatch):
    called = {}
    monkeypatch.setattr(highlight, "pick_via_transcript",
                        lambda *a, **k: called.setdefault("router", True) or {"clips": []})
    monkeypatch.setattr(highlight, "pick_via_gemini",
                        lambda *a, **k: called.setdefault("gemini", True) or {"clips": []})
    src = tmp_path / "source.mp4"
    src.write_bytes(b"x")
    highlight.pick(src, tmp_path, _cfg(engine="agentrouter"))
    assert called == {"router": True}
    highlight.pick(src, tmp_path / "g", _cfg(engine="gemini"))
    assert called == {"router": True, "gemini": True}
