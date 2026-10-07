import pytest

from clipper import config, gemini

CFG = config._merge(config.DEFAULTS, {"gemini": {"project_id": "p", "service_account_key": "k.json"}})


class Refused(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


class Fake:
    def __init__(self, name, calls, fail=None):
        self.name, self.calls, self.fail = name, calls, fail
        self.models = self

    def generate_content(self, **kw):
        self.calls.append(self.name)
        if self.fail:
            raise self.fail
        return f"answer from {self.name}"


@pytest.fixture
def routes(monkeypatch):
    gemini._down.clear()
    monkeypatch.setenv("GEMINI_API_KEY", "free-key")
    calls, fails = [], {}
    monkeypatch.setattr(gemini, "_raw_client", lambda name, cfg: Fake(name, calls, fails.get(name)))
    yield calls, fails
    gemini._down.clear()


def test_uses_vertex_first_when_it_works(routes):
    calls, _ = routes
    assert gemini.make_client(CFG).models.generate_content(model="m") == "answer from vertex"
    assert calls == ["vertex"]


def test_no_credit_on_vertex_falls_back_to_free_key_and_rests_vertex(routes):
    calls, fails = routes
    fails["vertex"] = Refused(403, "BILLING_DISABLED: This API method requires billing to be enabled")
    client = gemini.make_client(CFG)
    assert client.models.generate_content(model="m") == "answer from aistudio"
    assert client.used == "aistudio"
    client.models.generate_content(model="m")  # vertex is resting now: not asked again
    assert calls == ["vertex", "aistudio", "aistudio"]
    assert gemini.status(CFG)[0]["skipped_until"]


def test_a_bad_request_is_not_retried_elsewhere(routes):
    calls, fails = routes
    fails["vertex"] = Refused(400, "INVALID_ARGUMENT: schema")
    with pytest.raises(Refused):
        gemini.make_client(CFG).models.generate_content(model="m")
    assert calls == ["vertex"]


def test_order_and_missing_setup(routes, monkeypatch):
    calls, _ = routes
    cfg = config._merge(CFG, {"gemini": {"backends": ["aistudio", "vertex"]}})
    gemini.make_client(cfg).models.generate_content(model="m")
    assert calls == ["aistudio"]
    monkeypatch.delenv("GEMINI_API_KEY")
    bare = config._merge(config.DEFAULTS, {})
    with pytest.raises(gemini.NoGemini):
        gemini.make_client(bare).models.generate_content(model="m")


def test_both_refusing_says_so(routes):
    _, fails = routes
    fails["vertex"] = Refused(429, "RESOURCE_EXHAUSTED")
    fails["aistudio"] = Refused(429, "quota exceeded for free tier")
    with pytest.raises(gemini.NoGemini, match="vertex.*aistudio"):
        gemini.make_client(CFG).models.generate_content(model="m")


def test_a_timeout_stays_on_vertex(routes):
    calls, fails = routes
    fails["vertex"] = Refused(504, "504 DEADLINE_EXCEEDED. Deadline expired before operation could complete.")
    with pytest.raises(Refused):
        gemini.make_client(CFG).models.generate_content(model="m")
    assert calls == ["vertex"] and not gemini.status(CFG)[0]["skipped_until"]


def test_a_busy_429_rests_vertex_only_briefly(routes):
    _, fails = routes
    fails["vertex"] = Refused(429, "429 Resource exhausted. Please try again later.")
    import time
    gemini.make_client(CFG).models.generate_content(model="m")
    assert gemini.status(CFG)[0]["skipped_until"] - time.time() <= gemini.SHORT_REST_S + 1
