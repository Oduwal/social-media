"""Trending TikTok sounds: charts, Gemini's listening picks, choosing, and posting with a sound."""

import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clipper import app as webapp
from clipper import config, db, publisher, trending, worker
from tests.conftest import make_clip, make_tone, needs_ffmpeg

VID = "7412345678901234567"
CID = f"{VID}-01"
CFG = config._merge(config.DEFAULTS, {"upload_post": {"profile": "myclips"}, "posting": {"service": "upload_post"},
                                       "trending": {"countries": ["US", "NG"], "candidates_per_country": 2}})


def _track(i, country):
    return {"id": f"t{i}", "title": f"Song {i}", "artist": f"Artist {i}", "duration": 30, "rank": i,
            "genres": ["POP"], "cover_url": "", "preview_url": f"https://cdn/t{i}.mp3", "country": country}


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "data" / "jobs")
    return tmp_path


def _write_chart(country, tracks, age=0):
    path = trending.folder() / f"chart_{country}.json"
    path.write_text(json.dumps({"fetched_at": time.time() - age, "tracks": tracks, "error": None}))


# ------------------------------------------------------------------- charts

def test_fetch_chart_parses_upload_post(data_dir, monkeypatch):
    monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
    seen = {}

    def fake_get(url, headers, timeout, params):
        seen.update(params=params, auth=headers["Authorization"])
        return SimpleNamespace(status_code=200, json=lambda: {"music": [
            {"id": 111, "title": "Vibin", "artist": "Wxoda", "rank": 1, "preview_url": "u"}, {"title": "no id"}]})

    monkeypatch.setattr(trending.requests, "get", fake_get)
    tracks = trending.fetch_chart("NG", CFG)
    assert seen["params"]["country_code"] == "NG" and seen["params"]["profile"] == "myclips"
    assert seen["auth"] == "Apikey k"
    assert tracks == [{"id": "111", "title": "Vibin", "artist": "Wxoda", "duration": None, "rank": 1,
                       "genres": [], "cover_url": "", "preview_url": "u", "country": "NG"}]


def test_missing_music_permission_is_explained(data_dir, monkeypatch):
    monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
    monkeypatch.setattr(trending.requests, "get",
                        lambda *a, **k: SimpleNamespace(status_code=403, text="forbidden"))
    with pytest.raises(trending.TrendingError, match="music"):
        trending.fetch_chart("US", CFG)


def test_charts_are_cached_and_survive_a_failed_refresh(data_dir, monkeypatch):
    _write_chart("US", [_track(1, "US")])
    _write_chart("NG", [_track(2, "NG")], age=99999)  # stale

    def broken(country, cfg):
        raise trending.TrendingError("down")

    monkeypatch.setattr(trending, "fetch_chart", broken)
    out = trending.charts(CFG)
    assert out["US"]["tracks"][0]["id"] == "t1" and out["US"].get("error") is None  # fresh: not refetched
    assert out["NG"]["tracks"][0]["id"] == "t2" and out["NG"]["error"] == "down"  # stale kept, marked


def test_candidates_merge_songs_trending_in_both_countries():
    charts = {"US": {"tracks": [_track(1, "US"), _track(2, "US"), _track(3, "US")]},
              "NG": {"tracks": [{**_track(2, "NG"), "rank": 1}, _track(4, "NG")]}}
    pool = trending.candidates(charts, per_country=2)
    assert [t["id"] for t in pool] == ["t1", "t2", "t4"]
    both = next(t for t in pool if t["id"] == "t2")
    assert both["countries"] == [{"country": "US", "rank": 2}, {"country": "NG", "rank": 1}]
    assert "#2 in US, #1 in NG" in trending.label(both)


# ------------------------------------------------------------------ gemini

class FakeModels:
    def __init__(self, picks):
        self.picks, self.contents = picks, None

    def generate_content(self, model, contents, config):
        self.contents = contents
        return SimpleNamespace(parsed=trending.Picks(picks=[trending.Pick(**p) for p in self.picks]), text="")


@needs_ffmpeg
def test_gemini_listens_and_picks(data_dir, fresh_db, monkeypatch):
    _write_chart("US", [_track(1, "US"), _track(2, "US")])
    _write_chart("NG", [_track(3, "NG")])
    for i in (1, 2):  # previews already downloaded; t3's download fails
        make_tone(trending.folder() / "previews" / f"t{i}.mp3", seconds=20)
    monkeypatch.setattr(trending.requests, "get", lambda *a, **k: (_ for _ in ()).throw(OSError("no net")))
    db.insert_if_new(VID, "u", state="CLIPPED")
    db.insert_clip(CID, db.get(VID), {"reason": "big reveal", "music_mood": "hype"}, info={"title": "Reveal"})
    make_clip(config.job_dir(CID) / "transformed.mp4", seconds=3)

    fake = FakeModels([{"sound": "s2", "reason": "builds", "fit": 14, "start_s": 3.24},
                       {"sound": "S9", "reason": "made up", "fit": 5, "start_s": 0},
                       {"sound": "S1", "reason": "calm", "fit": 6, "start_s": -2}])
    sound = trending.suggest(db.get(CID), CFG, client=SimpleNamespace(models=fake))
    assert sound["listened_to"] == 2  # t3 had no preview
    assert [(s["id"], s["fit"], s["start_s"]) for s in sound["suggestions"]] == [("t2", 10, 3.2), ("t1", 6, 0.0)]
    audio_parts = [c for c in fake.contents if hasattr(c, "inline_data") and c.inline_data
                   and c.inline_data.mime_type == "audio/mp3"]
    assert len(audio_parts) == 2  # Gemini really got the sounds to listen to


# ------------------------------------------------------------------ dashboard

@pytest.fixture
def client(data_dir, fresh_db, monkeypatch):
    monkeypatch.setattr(config, "MUSIC_DIR", data_dir / "music")
    (data_dir / "music").mkdir()
    monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
    real_load = config.load
    monkeypatch.setattr(config, "load", lambda: config._merge(real_load(), {"posting": {"service": "upload_post"}}))
    return TestClient(webapp.app)


def _ready_clip(sound=None):
    db.insert_if_new(VID, "u", state="CLIPPED")
    db.insert_clip(CID, db.get(VID), {"start_s": 0, "end_s": 10}, platforms=["tiktok", "instagram"],
                   captions={"tiktok": "a", "instagram": "b", "x": "c"}, sound=sound)
    db.update(CID, state="READY")
    make_clip(config.job_dir(CID) / "final.mp4", seconds=1)
    return CID


def test_choose_a_suggestion_or_any_chart_track(client, fresh_db):
    _write_chart("US", [_track(1, "US"), _track(2, "US")])
    _ready_clip({"suggestions": [{**_track(1, "US"), "countries": [{"country": "US", "rank": 1}],
                                  "reason": "fits", "fit": 9, "start_s": 4.0}], "status": "ready"})
    page = client.get(f"/jobs/{CID}")
    assert "Gemini's picks" in page.text and "Song 2" in page.text  # picks + browsable chart
    client.post(f"/jobs/{CID}/sound/choose", data={"track_id": "t1"})
    chosen = db.get(CID)["sound"]["chosen"]
    assert chosen["id"] == "t1" and chosen["start_s"] == 4.0 and chosen["volume"] == 35
    client.post(f"/jobs/{CID}/sound/settings", data={"volume": "60", "original_volume": "90", "start_s": "2.5"})
    client.post(f"/jobs/{CID}/sound/choose", data={"track_id": "t2"})  # from the chart, keeps volumes
    chosen = db.get(CID)["sound"]["chosen"]
    assert chosen["id"] == "t2" and chosen["volume"] == 60 and chosen["original_volume"] == 90
    client.post(f"/jobs/{CID}/sound/clear")
    assert db.get(CID)["sound"]["chosen"] is None
    assert client.post(f"/jobs/{CID}/sound/choose", data={"track_id": "gone"}).status_code == 404


def test_sound_cannot_change_while_posting(client, fresh_db):
    _ready_clip()
    db.update(CID, state="POSTING")
    assert client.post(f"/jobs/{CID}/sound/clear").status_code == 409


# ------------------------------------------------------------------ posting

@needs_ffmpeg
def test_tiktok_goes_up_separately_with_the_sound_and_without_editor_music(data_dir, fresh_db, monkeypatch):
    monkeypatch.setattr(config, "MUSIC_DIR", data_dir / "music")
    (data_dir / "music").mkdir()
    make_tone(data_dir / "music" / "beat.mp3", seconds=3)
    cid = _ready_clip({"chosen": {**_track(7, "US"), "start_s": 2.0, "volume": 40, "original_volume": 100}})
    job_dir = config.job_dir(cid)
    make_clip(job_dir / "transformed.mp4", seconds=3)
    db.update(cid, state="APPROVED", edit={"trim": {"start": 0, "end": 3}, "filter": "none", "zoom": "none",
                                           "fade_in": 0, "fade_out": 0, "voice_volume": 1, "texts": [],
                                           "music": {"track": "beat.mp3", "volume": 0.3, "offset": 0,
                                                     "ducking": True, "fade_in": 0.5, "fade_out": 1}})
    calls = []

    def fake_publish(video, vid, attempt, captions, platforms, ai_label, cfg, on_submitted=None,
                     extra=None, key_suffix=""):
        calls.append({"video": video.name, "platforms": platforms, "extra": dict(extra or []), "suffix": key_suffix})
        return {"status": "completed", "request_id": "r", "platforms": {p: {"ok": True} for p in platforms}}

    monkeypatch.setattr(publisher, "publish", fake_publish)
    worker.advance(db.get(cid), CFG)
    assert db.get(cid)["state"] == "POSTED"
    tiktok, rest = calls
    assert tiktok["platforms"] == ["tiktok"] and tiktok["video"] == "final_tiktok.mp4" and tiktok["suffix"] == "-tiktok"
    assert tiktok["extra"]["tiktok_music_id"] == "t7" and tiktok["extra"]["tiktok_music_start"] == "2000"
    assert tiktok["extra"]["tiktok_music_volume"] == "40"
    assert rest["platforms"] == ["instagram"] and rest["video"] == "final.mp4" and rest["suffix"] == ""


def test_tiktok_posted_but_instagram_upload_lost_is_not_reposted(data_dir, fresh_db, monkeypatch):
    cid = _ready_clip({"chosen": {**_track(7, "US"), "start_s": 0, "volume": 35, "original_volume": 100}})
    db.update(cid, state="APPROVED", info={"render_from": "shotcut"})  # shotcut: no separate tiktok render
    calls = []

    def fake_publish(video, vid, attempt, captions, platforms, ai_label, cfg, on_submitted=None,
                     extra=None, key_suffix=""):
        calls.append((attempt, key_suffix, list(platforms)))
        if platforms == ["instagram"] and len(calls) == 2:
            raise ConnectionError("dropped")  # did Instagram's upload arrive? unknown
        return {"status": "completed", "request_id": "r", "platforms": {p: {"ok": True} for p in platforms}}

    monkeypatch.setattr(publisher, "publish", fake_publish)
    worker.advance(db.get(cid), CFG)
    assert db.get(cid)["state"] == "FAILED"
    assert db.get(cid)["post_result"]["platforms"]["tiktok"]["ok"] is True
    db.retry(cid)
    worker.advance(db.get(cid), CFG)
    assert calls == [(1, "-tiktok", ["tiktok"]), (1, "", ["instagram"]), (1, "", ["instagram"])]
    assert db.get(cid)["state"] == "POSTED"


def test_no_sound_means_one_upload_as_before(data_dir, fresh_db, monkeypatch):
    cid = _ready_clip()
    db.update(cid, state="APPROVED")
    calls = []

    def fake_publish(video, vid, attempt, captions, platforms, ai_label, cfg, on_submitted=None):
        calls.append(platforms)
        return {"status": "completed", "request_id": "r", "platforms": {p: {"ok": True} for p in platforms}}

    monkeypatch.setattr(publisher, "publish", fake_publish)
    worker.advance(db.get(cid), CFG)
    assert calls == [["tiktok", "instagram"]]


def test_sound_fields():
    fields = dict(publisher.sound_fields({"id": "t1", "start_s": 1.5, "volume": 30, "original_volume": 90}, 12.0))
    assert fields == {"tiktok_music_id": "t1", "tiktok_music_volume": "30", "tiktok_original_sound_volume": "90",
                      "tiktok_music_start": "1500", "tiktok_music_end": "13500"}


def test_suggestions_run_once_for_tiktok_clips(monkeypatch):
    monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
    job = {"kind": "clip", "platforms": ["tiktok"], "sound": None}
    assert worker.wants_sound_suggestions(job, CFG)
    assert not worker.wants_sound_suggestions({**job, "platforms": ["x"]}, CFG)
    assert not worker.wants_sound_suggestions({**job, "sound": {"suggestions": [1]}}, CFG)
    monkeypatch.delenv("UPLOAD_POST_API_KEY")
    assert not worker.wants_sound_suggestions(job, CFG)
