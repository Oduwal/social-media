import pytest

from clipper import config, gemini

CFG = config._merge(config.DEFAULTS, {"gemini": {"project_id": "p", "service_account_key": "k.json"}})


class Refused(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


class Fake:
    def __init__(self, route, calls, fail=None):
        self.route, self.base = route, route.split("#")[0]
        self.calls, self.fail = calls, fail
        self.models = self

    def generate_content(self, **kw):
        self.calls.append(self.base)   # base name ("aistudio") so tests don't track key indexes
        if self.fail:
            raise self.fail
        return f"answer from {self.base}"


@pytest.fixture
def routes(monkeypatch):
    gemini._down.clear()
    monkeypatch.setenv("GEMINI_API_KEY", "free-key")
    calls, fails = [], {}
    # fails can be keyed by a specific route ("aistudio#1") or a whole backend ("aistudio")
    monkeypatch.setattr(gemini, "_raw_client",
                        lambda route, cfg: Fake(route, calls, fails.get(route, fails.get(route.split("#")[0]))))
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
    assert client.used.startswith("aistudio")
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


def test_a_timeout_stays_on_vertex(routes, monkeypatch):
    calls, fails = routes
    monkeypatch.setattr(gemini.time, "sleep", lambda s: None)
    fails["vertex"] = Refused(504, "504 DEADLINE_EXCEEDED. Deadline expired before operation could complete.")
    with pytest.raises(Refused):
        gemini.make_client(CFG).models.generate_content(model="m")
    # retried on vertex (a temporary error), but never bounced to the free-tier key, and not rested
    assert set(calls) == {"vertex"} and "aistudio" not in calls and not gemini.status(CFG)[0]["skipped_until"]


def test_a_busy_429_rests_vertex_only_briefly(routes):
    _, fails = routes
    fails["vertex"] = Refused(429, "429 Resource exhausted. Please try again later.")
    import time
    gemini.make_client(CFG).models.generate_content(model="m")
    assert gemini.status(CFG)[0]["skipped_until"] - time.time() <= gemini.SHORT_REST_S + 1


def test_the_google_client_stays_open_while_the_call_runs(monkeypatch):
    """google-genai closes its connection when a genai.Client is garbage-collected; taking
    `.models` from a throwaway client broke every highlight with "client has been closed"."""
    import gc

    gemini._down.clear()

    class Models:
        def __init__(self, owner):
            self.owner_alive = owner

        def generate_content(self, **kw):
            gc.collect()
            if self.owner_alive() is None:
                raise RuntimeError("Cannot send a request, as the client has been closed.")
            return "ok"

    class ClosingClient:
        def __init__(self):
            import weakref
            self.models = Models(weakref.ref(self))

    monkeypatch.setattr(gemini, "_raw_client", lambda name, cfg: ClosingClient())
    client = gemini.make_client(CFG)
    assert client.models.generate_content(model="m") == "ok"
    assert client.models.generate_content(model="m") == "ok"   # and it is reused, not rebuilt


def test_a_temporary_503_is_retried_then_succeeds(routes, monkeypatch):
    calls, fails = routes
    monkeypatch.setattr(gemini.time, "sleep", lambda s: None)   # no real waiting in tests
    hits = {"n": 0}

    class Flaky(Exception):
        code = 503

    def flaky_generate(self, **kw):
        calls.append(self.base)
        hits["n"] += 1
        if hits["n"] < 3:                       # overloaded twice, then works
            raise Flaky("503 UNAVAILABLE. This model is currently experiencing high demand.")
        return "finally ok"

    monkeypatch.setattr(gemini.Fake if hasattr(gemini, "Fake") else Fake, "generate_content", flaky_generate)
    assert gemini.make_client(CFG).models.generate_content(model="m") == "finally ok"
    assert calls == ["vertex", "vertex", "vertex"]   # same route, retried, not bounced to aistudio


def test_transient_classifier():
    class E(Exception):
        def __init__(self, code=None):
            self.code = code
    assert gemini.transient(E(503)) and gemini.transient(E(500))
    assert gemini.transient(Exception("[Errno 11001] getaddrinfo failed"))
    assert gemini.transient(Exception("high demand, try again later"))
    assert not gemini.transient(E(400))          # a bad request is not retried
    assert not gemini.transient(Exception("invalid argument"))


def test_keys_parsing_splits_and_dedupes(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "key1, key2\n key2  key3")
    assert gemini.keys() == ["key1", "key2", "key3"]
    monkeypatch.setenv("GEMINI_API_KEY", "solo")
    assert gemini.keys() == ["solo"]


def test_keys_rotate_when_one_is_over_its_limit(routes, monkeypatch):
    calls, fails = routes
    monkeypatch.setenv("GEMINI_API_KEY", "key1, key2")   # two accounts
    cfg = config._merge(CFG, {"gemini": {"backends": ["aistudio"]}})
    fails["aistudio#0"] = Refused(429, "quota exceeded for the free tier")  # first key used up today
    assert gemini.available(cfg) == ["aistudio#0", "aistudio#1"]
    client = gemini.make_client(cfg)
    assert client.models.generate_content(model="m") == "answer from aistudio"
    assert client.used == "aistudio#1"                   # rotated to the second account
    client.models.generate_content(model="m")            # first key is resting; skipped
    assert calls == ["aistudio", "aistudio", "aistudio"]
    st = {r["name"]: r for r in gemini.status(cfg)}
    assert st["aistudio#0"]["skipped_until"] and not st["aistudio#1"]["skipped_until"]
    assert st["aistudio#0"]["label"] == "Free Gemini key #1"


def test_a_busy_model_rotates_to_the_next_free_key(routes, monkeypatch):
    calls, fails = routes
    monkeypatch.setattr(gemini.time, "sleep", lambda s: None)
    monkeypatch.setenv("GEMINI_API_KEY", "key1, key2, key3")
    cfg = config._merge(CFG, {"gemini": {"backends": ["aistudio"]}})
    busy = Refused(503, "503 UNAVAILABLE. This model is currently experiencing high demand.")
    fails["aistudio#0"] = busy
    fails["aistudio#1"] = busy
    client = gemini.make_client(cfg)
    assert client.models.generate_content(model="m") == "answer from aistudio"
    assert client.used == "aistudio#2"                     # two keys stayed busy; the third answered
    assert not any(r["skipped_until"] for r in gemini.status(cfg))   # a busy model isn't a used-up key


def test_a_busy_model_on_the_last_key_still_raises(routes, monkeypatch):
    calls, fails = routes
    monkeypatch.setattr(gemini.time, "sleep", lambda s: None)
    monkeypatch.setenv("GEMINI_API_KEY", "key1, key2")
    cfg = config._merge(CFG, {"gemini": {"backends": ["aistudio"]}})
    fails["aistudio"] = Refused(503, "503 UNAVAILABLE. high demand")
    with pytest.raises(Refused):
        gemini.make_client(cfg).models.generate_content(model="m")


def test_falls_back_to_another_model_when_the_first_fails_on_every_key(monkeypatch):
    seen = []

    class ModelAware:
        def __init__(self, route):
            self.models = self

        def generate_content(self, **kw):
            seen.append(kw["model"])
            if kw["model"] == "main":
                raise Refused(429, "quota exceeded for the free tier")
            return f"answer from {kw['model']}"

    gemini._down.clear()
    monkeypatch.setenv("GEMINI_API_KEY", "key1, key2")
    monkeypatch.setattr(gemini, "_raw_client", lambda route, cfg: ModelAware(route))
    cfg = config._merge(CFG, {"gemini": {"backends": ["aistudio"], "fallback_models": ["second", "third"]}})
    assert gemini.make_client(cfg).models.generate_content(model="main") == "answer from second"
    assert seen == ["main", "main", "second"]            # both keys tried on main, then the fallback model
    seen.clear()
    cfg = config._merge(CFG, {"gemini": {"backends": ["aistudio"], "fallback_models": []}})
    with pytest.raises(gemini.NoGemini):
        gemini.make_client(cfg).models.generate_content(model="main")
    gemini._down.clear()


def test_a_hung_request_moves_to_the_next_key_without_retrying(routes, monkeypatch):
    calls, fails = routes
    slept = []
    monkeypatch.setattr(gemini.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setenv("GEMINI_API_KEY", "key1, key2")
    cfg = config._merge(CFG, {"gemini": {"backends": ["aistudio"]}})

    class ReadTimeout(Exception):
        pass

    fails["aistudio#0"] = ReadTimeout("The read operation timed out")
    client = gemini.make_client(cfg)
    assert client.models.generate_content(model="m") == "answer from aistudio"
    assert client.used == "aistudio#1" and slept == []        # no backoff waits on a hung key
    assert gemini.timed_out(ReadTimeout("x")) and not gemini.timed_out(Refused(503, "high demand"))
