"""The Google Flow robot: its decisions (offline) and how the transform step uses it.
No browser is opened and no credits are spent: flow_robot.edit is replaced by a fake."""

import time

import pytest

from clipper import flow_robot, media, transform_adapter
from tests.conftest import make_clip, needs_ffmpeg
from tests.test_transform_adapter import STUB

REAL_EDIT_MANY = flow_robot.edit_many


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(flow_robot, "STATE_PATH", tmp_path / "flow_state.json")
    return tmp_path


CFG = {"transform": {"backend": "flow"}, "flow": {"daily_max_edits": 3, "min_gap_s": 60, "max_gap_s": 60}}


# ------------------------------------------------------------------ decisions

def test_page_messages_are_sorted_into_what_to_do():
    assert flow_robot.classify("warning\nFailed\nSorry, this video failed to generate.") is flow_robot.Failed
    assert flow_robot.classify("We noticed unusual activity. Try again later") is flow_robot.Paused
    assert flow_robot.classify("This prompt violates our policies") is flow_robot.Refused
    assert flow_robot.classify("You don't have enough credits") is flow_robot.NeedsYou
    assert flow_robot.classify("45%\nApplying albino pigmentation") is None


def test_agents_long_narration_does_not_trigger_a_pause():
    # the Flow Agent narrates its plan in one long line; a trouble word buried in it is not a banner
    narration = ("I'm going to edit the attached image to give the person an albino appearance. "
                 "I will not change the daily limit of detail or the rate limit of motion, and I'll keep "
                 "everything else exactly the same across every frame of the video as instructed.")
    assert len(narration) > flow_robot.AGENT_PROSE_LEN
    assert flow_robot.classify(narration) is None
    # a real short banner on its own line is still caught, even next to the narration
    assert flow_robot.classify(narration + "\nWe noticed unusual activity.") is flow_robot.Paused


def test_only_new_lines_count_and_our_prompt_echo_is_ignored():
    prompt = "Make him albino. Nothing is allowed to change; this is not allowed: hats."
    before = "Sorry, this video failed to generate.\nAll media"
    after = before + "\nMake him albino. Nothing is allowed to change; this is not…\n12%"
    fresh = flow_robot.new_lines(before, after, prompt)
    assert fresh == "12%"  # the old failure and the prompt caption don't trigger anything
    assert flow_robot.classify(fresh) is None


def test_credit_balance_is_read_from_flows_answer():
    body = ')]}\'\n\n149\n[["wrb.fr","nzlxg","[886,1,2,2,null,886,[1791326551,164394000]]",null]]'
    assert flow_robot.credits_from(body) == 886
    assert flow_robot.credits_from('[["wrb.fr","jwpduf","[null]"]]') is None


def test_daily_cap_pause_and_low_credits_stop_new_edits(state):
    flow_robot.check_allowed(CFG)  # fresh: fine
    flow_robot.save_state(day=time.strftime("%Y-%m-%d"), edits_today=3)
    with pytest.raises(flow_robot.Paused, match="Daily cap"):
        flow_robot.check_allowed(CFG)
    flow_robot.save_state(day="2000-01-01")  # a new day resets the count
    flow_robot.check_allowed(CFG)
    flow_robot.pause("unusual activity", 30)
    with pytest.raises(flow_robot.Paused, match="paused until"):
        flow_robot.check_allowed(CFG)
    flow_robot.clear_pause()
    flow_robot.save_state(credits=20, credits_at=time.time())
    with pytest.raises(flow_robot.NeedsYou, match="credits"):
        flow_robot.check_allowed(CFG)


def test_edits_are_spaced_out(state):
    assert flow_robot.gap_wait(CFG) == 0
    flow_robot.save_state(last_edit_at=time.time())
    assert 55 < flow_robot.gap_wait(CFG) <= 60


def test_length_note_matches_the_piece():
    assert flow_robot.length_note(9.98) == \
        " The edited video must be the full length of the attached video (10 seconds)."


# ------------------------------------------------------------------ the transform step with Flow

@pytest.fixture
def stub(tmp_path):
    script_dir = tmp_path / "script"
    script_dir.mkdir()
    script = script_dir / "transform_video.py"
    script.write_text(STUB + '''
def edit_segment(client, segment_path, index, verbose):
    raise AssertionError("Vertex must not be used with the Flow backend")
def make_client():
    raise AssertionError("Vertex must not be used with the Flow backend")
''')
    yield script
    transform_adapter._modules.clear()


@pytest.fixture(autouse=True)
def batch_through_fake_edit(monkeypatch):
    """The transform step sends a clip's pieces to edit_many; here that runs each piece
    through flow_robot.edit, which the tests below replace with a fake."""
    def many(pieces, instruction, cfg):
        out = {}
        for key, segment, dst, base in pieces:
            try:
                out[key] = flow_robot.edit(segment, instruction, dst, base, cfg)
            except (flow_robot.Paused, flow_robot.NeedsYou):
                raise
            except flow_robot.FlowError as err:
                out[key] = err
        return out
    monkeypatch.setattr(flow_robot, "edit_many", many)


def _flow_cfg(script):
    return {"transform": {"enabled": True, "script_path": str(script), "variant": "albinism", "backend": "flow",
                          "model": ["gemini-omni-1.1-flash"], "resolution": "1080p", "colour_match": False}}


def _fake_flow(calls, behaviour=None):
    """Stands in for flow_robot.edit: writes what Flow returns (720p, 24 fps, a bit long)."""
    def edit(segment, instruction, out, name, cfg):
        calls.append({"segment": segment, "instruction": instruction, "name": name})
        if behaviour:
            behaviour(len(calls))
        make_clip(out, seconds=media.duration(segment) * 1.04, size="360x640", tone=False)
        return out
    return edit


@needs_ffmpeg
def test_flow_piece_is_conformed_to_the_original_and_cached(tmp_path, stub, monkeypatch):
    calls = []
    monkeypatch.setattr(flow_robot, "edit", _fake_flow(calls))
    clip = make_clip(tmp_path / "highlight.mp4", seconds=4, size="720x1280")
    info = transform_adapter.run(clip, tmp_path, _flow_cfg(stub))
    assert info["backend"] == "flow" and info["fallback_segments"] == [] and not info["refused"]
    piece = tmp_path / info["pieces"][0]["cache"]
    assert piece.name.startswith("edited_001_")
    assert media.video_size(piece) == (720, 1280)  # back to the original size, not Flow's
    assert abs(media.duration(piece) - 4) < 0.1  # stretched to the original length
    assert media.has_audio(piece)  # with the original sound
    assert calls[0]["name"].startswith(f"clipper_{tmp_path.name}_p01_")
    assert info["original_audio"]
    # run again (e.g. after a re-mix): the cached piece is reused, no second edit
    transform_adapter.run(clip, tmp_path, _flow_cfg(stub))
    assert len(calls) == 1


@needs_ffmpeg
def test_flow_refusal_keeps_original_footage_and_stops_sending(tmp_path, stub, monkeypatch):
    def refuse(n):
        raise flow_robot.Refused("Flow: This prompt violates our policies")
    monkeypatch.setattr(flow_robot, "edit", _fake_flow([], refuse))
    clip = make_clip(tmp_path / "highlight.mp4", seconds=3)
    info = transform_adapter.run(clip, tmp_path, _flow_cfg(stub))
    assert info["refused"] and info["blocked"] and info["fallback_segments"] == [1]


@needs_ffmpeg
def test_robot_needing_you_stops_the_clip_instead_of_posting_half_albino(tmp_path, stub, monkeypatch):
    def signed_out(n):
        raise flow_robot.NeedsYou("The Flow robot isn't signed in")
    monkeypatch.setattr(flow_robot, "edit", _fake_flow([], signed_out))
    clip = make_clip(tmp_path / "highlight.mp4", seconds=3)
    with pytest.raises(flow_robot.NeedsYou):
        transform_adapter.run(clip, tmp_path, _flow_cfg(stub))
    assert not (tmp_path / "transformed.mp4").exists()


@needs_ffmpeg
def test_flow_failing_twice_falls_back_to_original_for_that_piece(tmp_path, stub, monkeypatch):
    def failed(n):
        raise flow_robot.Failed("Flow: Sorry, this video failed to generate.")
    monkeypatch.setattr(flow_robot, "edit", _fake_flow([], failed))
    clip = make_clip(tmp_path / "highlight.mp4", seconds=3)
    info = transform_adapter.run(clip, tmp_path, _flow_cfg(stub))
    assert info["fallback_segments"] == [1] and not info["blocked"]


@needs_ffmpeg
def test_flow_and_vertex_edits_are_cached_apart(tmp_path, stub, monkeypatch):
    calls = []
    monkeypatch.setattr(flow_robot, "edit", _fake_flow(calls))
    clip = make_clip(tmp_path / "highlight.mp4", seconds=3)
    info = transform_adapter.run(clip, tmp_path, _flow_cfg(stub))
    mod = transform_adapter.load_script(stub)
    flow_key = mod.EDIT_VARIANT
    transform_adapter._prepare(mod, {"transform": {**_flow_cfg(stub)["transform"], "backend": "vertex",
                                                   "model": ""}}, tmp_path / "transform_work", tmp_path, "")
    assert mod.EDIT_VARIANT != flow_key
    assert info["pieces"][0]["cache"].endswith(f"{flow_key}.mp4")


def test_look_step_is_not_offered_with_flow(tmp_path, stub):
    with pytest.raises(ValueError, match="only works with Vertex"):
        transform_adapter.make_look(tmp_path / "x.mp4", tmp_path, _flow_cfg(stub))


@needs_ffmpeg
def test_conform_matches_size_and_rate_without_stretching(tmp_path):
    seg = make_clip(tmp_path / "seg.mp4", seconds=5, size="720x1280")
    short = make_clip(tmp_path / "short.mp4", seconds=4.8, size="360x640", tone=False)
    out = transform_adapter.conform(short, seg, tmp_path / "out.mp4")  # 0.2 s short: last frame held
    assert media.video_size(out) == (720, 1280) and transform_adapter._fps(out) == "30/1"
    assert abs(transform_adapter._video_length(out) - 5) < 0.1 and media.has_audio(out)
    long = make_clip(tmp_path / "long.mp4", seconds=5.2, size="360x640", tone=False)
    out2 = transform_adapter.conform(long, seg, tmp_path / "out2.mp4")  # trimmed, not squeezed
    assert abs(transform_adapter._video_length(out2) - 5) < 0.1


@needs_ffmpeg
def test_conform_rejects_a_piece_flow_cut_short(tmp_path):
    seg = make_clip(tmp_path / "seg.mp4", seconds=5)
    cut = make_clip(tmp_path / "cut.mp4", seconds=4, size="360x640", tone=False)
    with pytest.raises(flow_robot.Failed, match="4.0 s long for a 5.0 s"):
        transform_adapter.conform(cut, seg, tmp_path / "out.mp4")
    assert not (tmp_path / "out.mp4").exists() and not (tmp_path / "out.part.mp4").exists()


@needs_ffmpeg
def test_a_downloaded_result_is_reused_not_paid_for_again(tmp_path, stub, monkeypatch):
    calls = []
    monkeypatch.setattr(flow_robot, "edit", _fake_flow(calls))
    clip = make_clip(tmp_path / "highlight.mp4", seconds=3)
    monkeypatch.setattr(transform_adapter, "conform", lambda *a: (_ for _ in ()).throw(RuntimeError("ffmpeg hiccup")))
    transform_adapter.run(clip, tmp_path, _flow_cfg(stub))  # piece falls back; the raw result stays
    monkeypatch.undo()
    monkeypatch.setattr(flow_robot, "edit", _fake_flow(calls))
    info = transform_adapter.run(clip, tmp_path, _flow_cfg(stub))
    assert len(calls) == 1 and info["fallback_segments"] == []


def test_tile_names_reuse_until_a_result_is_used(state):
    assert flow_robot.tile_name("clipper_x_p01_abc") == "clipper_x_p01_abc"
    flow_robot.mark_done("clipper_x_p01_abc")
    assert flow_robot.tile_name("clipper_x_p01_abc") == "clipper_x_p01_abc-2"  # a redo gets a new one


def test_needing_you_holds_everything_until_cleared(state):
    flow_robot.save_state(needs_you="Signed out")
    assert not flow_robot.can_edit(CFG)
    with pytest.raises(flow_robot.NeedsYou, match="Signed out"):
        flow_robot.check_allowed(CFG)
    flow_robot.clear_pause()
    assert flow_robot.can_edit(CFG)


def test_rights_notices_need_you_and_ordinary_policy_links_are_fine():
    assert flow_robot.classify("Rights to use this video. Make sure you have the necessary rights") \
        is flow_robot.NeedsYou
    assert flow_robot.classify("Privacy Policy. Terms") is None
    assert flow_robot.classify("I can't generate audio, so I'll keep the original") is None
    assert flow_robot.classify("This prompt violates our policies") is flow_robot.Refused


# ------------------------------------------------------------------ the worker while the robot can't go on

def test_paused_robot_leaves_the_clip_waiting_not_failed(fresh_db, monkeypatch):
    from clipper import db, worker
    db.insert_if_new("7400000000000000001", "u", state="CLIPPED")
    db.insert_clip("7400000000000000001-01", db.get("7400000000000000001"), {"start_s": 0, "end_s": 20})
    db.update("7400000000000000001-01", state="CUT")

    def paused(job, step, cfg):
        raise flow_robot.Paused("Daily cap reached (15 Flow edits today)")
    monkeypatch.setattr(worker, "run_step", paused)
    worker.advance(db.get("7400000000000000001-01"), {"dashboard": {"host": "h", "port": 1}})
    job = db.get("7400000000000000001-01")
    assert job["state"] == "CUT" and "Waiting for the Flow robot" in job["error"]
    # and the queue doesn't pick it up again while the robot can't edit
    assert db.next_actionable(skip_transforms=True) is None
    assert db.next_actionable()["video_id"] == "7400000000000000001-01"


def test_flows_status_polls_are_read():
    import json
    inner = [None, 826, [["5abcf7ad-99b2-4491-8dd7-7ce1491b5cf3", "proj", "wf", None, None,
                          [[1, 2], "PROMPT", None, None, None, None,
                           [None, [["abra_edit", 4, [10], None, 1, 1], None,
                                   [[None, None, None, "5a477943-b75b-4fef-b882-df33ccfc3eb4"]]]]],
                          None, [3], 1, None, None, None, 3306687]]]
    body = ")]}'\n\n2232\n" + json.dumps([["wrb.fr", "jwpduf", json.dumps(inner), None, None, None, "generic"]])
    [(result, sources, status)] = flow_robot.generations_from(body)
    assert result == "5abcf7ad-99b2-4491-8dd7-7ce1491b5cf3" and status == flow_robot.DONE
    assert "5a477943-b75b-4fef-b882-df33ccfc3eb4" in sources
    assert flow_robot.generations_from('[["wrb.fr","nzlxg","[886]"]]') == []


# ------------------------------------------------------------------ a whole clip at once (fake browser)

class FakeRun:
    """Stands in for the robot's browser session: records what it's asked to do."""
    events, polls, fail_once = [], {}, set()

    def __init__(self, cfg):
        self.page = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def open_project(self):
        FakeRun.events.append("open")

    def clear_prompt(self):
        pass

    def upload(self, file, name):
        FakeRun.events.append(f"upload {name}")

    def upload_id(self, name):
        return f"id-{name}"

    def attach(self, name):
        pass

    def agent_on(self):
        pass

    def prompt(self, text):
        FakeRun.events.append(f"prompt {text[-12:]}")

    def click_generate(self):
        return ""

    def _check_pushback(self):
        pass

    def poll(self, p):
        n = FakeRun.polls[p["name"]] = FakeRun.polls.get(p["name"], 0) + 1
        if p["name"] in FakeRun.fail_once:
            FakeRun.fail_once.discard(p["name"])
            raise flow_robot.Failed("Flow: Sorry, this video failed to generate")
        if n >= 2:
            FakeRun.events.append(f"done {p['name']}")
            return f"result-{p['name']}"
        return None

    def fetch(self, name, result, out):
        out.write_bytes(b"video")


@pytest.fixture
def fake_browser(state, monkeypatch):
    FakeRun.events, FakeRun.polls, FakeRun.fail_once = [], {}, set()
    sleeps = []
    monkeypatch.setattr(flow_robot, "_Run", FakeRun)
    monkeypatch.setattr(flow_robot, "_tile_info", lambda page, name: None)
    monkeypatch.setattr(flow_robot.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(flow_robot.media, "duration", lambda p: 10.0)
    monkeypatch.setattr(flow_robot, "edit_many", REAL_EDIT_MANY)  # the real one, not the fake above
    return sleeps


def _pieces(tmp_path, n, clip="c"):
    out = []
    for i in range(1, n + 1):
        seg = tmp_path / f"{clip}{i}.mp4"
        seg.write_bytes(b"x")
        out.append((i, seg, tmp_path / f"{clip}{i}_raw.mp4", f"clipper_{clip}_p{i:02d}"))
    return out


def test_all_pieces_start_before_any_is_collected(tmp_path, fake_browser):
    results = flow_robot.edit_many(_pieces(tmp_path, 3), "Make him albino.", CFG)
    assert sorted(results) == [1, 2, 3] and all(r.exists() for r in results.values())
    first_done = next(i for i, e in enumerate(FakeRun.events) if e.startswith("done"))
    prompts = [i for i, e in enumerate(FakeRun.events) if e.startswith("prompt")]
    assert len(prompts) == 3 and max(prompts) < first_done      # all three generating at once
    assert flow_robot.edits_today() == 3


def test_next_clip_waits_twenty_seconds(tmp_path, fake_browser):
    flow_robot.edit_many(_pieces(tmp_path, 1, "a"), "x", CFG)
    fake_browser.clear()
    flow_robot.edit_many(_pieces(tmp_path, 1, "b"), "x", CFG)
    assert fake_browser and 15 < fake_browser[0] <= 20            # the gap before the next clip


def test_a_failed_piece_gets_one_more_go_and_the_others_are_kept(tmp_path, fake_browser):
    FakeRun.fail_once = {"clipper_c_p02"}
    roomy = {**CFG, "flow": {**CFG["flow"], "daily_max_edits": 10}}
    results = flow_robot.edit_many(_pieces(tmp_path, 3), "x", roomy)
    assert all(not isinstance(r, Exception) for r in results.values())
    assert sum(1 for e in FakeRun.events if e == "open") == 2      # a second round for piece 2 only
    assert [e for e in FakeRun.events if e.startswith("upload")][-1].startswith("upload clipper_c_p02")


def test_daily_cap_mid_clip_finishes_started_pieces_then_waits(tmp_path, fake_browser):
    flow_robot.save_state(day=time.strftime("%Y-%m-%d"), edits_today=2)   # cap is 3 in CFG
    with pytest.raises(flow_robot.Paused, match="Daily cap"):
        flow_robot.edit_many(_pieces(tmp_path, 3), "x", CFG)
    assert (tmp_path / "c1_raw.mp4").exists()                    # the one that started was kept
