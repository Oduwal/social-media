"""Library: live cutting progress, and previews that play in place."""

import pytest
from fastapi.testclient import TestClient

from clipper import app as webapp
from clipper import config, db

VID = "7412345678901234567"


@pytest.fixture
def client(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    return TestClient(webapp.app)


def _clips(states):
    db.insert_if_new(VID, "u", state="CLIPPED")
    for n, state in enumerate(states, start=1):
        cid = f"{VID}-{n:02d}"
        db.insert_clip(cid, db.get(VID), {"start_s": 0, "end_s": 20}, info={"title": f"Clip {n}"})
        db.update(cid, state=state)


def test_progress_counts_what_is_still_cutting(client, fresh_db):
    _clips(["PREVIEW", "PREVIEW", "NEW", "NEW", "READY"])
    p = client.get(f"/api/progress?video={VID}").json()
    assert (p["total"], p["cut"], p["cutting"], p["finding"]) == (5, 3, 2, 0)
    db.update(f"{VID}-03", state="PREVIEW")
    assert client.get(f"/api/progress?video={VID}").json()["signature"] != p["signature"]


def test_progress_reports_videos_gemini_is_still_watching(client, fresh_db):
    db.insert_if_new("999", "u", state="DOWNLOADED")
    assert client.get("/api/progress").json()["finding"] == 1
    db.update("999", hold=True)
    assert client.get("/api/progress").json()["finding"] == 0


def test_library_shows_progress_and_playable_previews(client, fresh_db):
    _clips(["PREVIEW", "NEW"])
    page = client.get("/library")
    assert 'id="progress"' in page.text and "Cutting highlights: 1 of 2 ready" in page.text
    assert 'class="thumb playable"' in page.text and "Click to play with sound" in page.text
    assert 'http-equiv="refresh"' not in page.text  # live updates instead of reloading under the user


def test_progress_bar_hidden_when_nothing_is_cutting(client, fresh_db):
    _clips(["PREVIEW"])
    page = client.get(f"/jobs/{VID}")
    assert 'id="progress"' in page.text and " hidden>" in page.text.split('id="progress"')[1].split("</div>")[0]


def test_previews_are_cut_before_slow_transforms(fresh_db):
    _clips(["CUT", "CUT", "NEW", "NEW"])  # two picked for albino, two still uncut
    assert db.next_actionable()["video_id"] == f"{VID}-03"
    db.update(f"{VID}-03", state="PREVIEW")
    assert db.next_actionable()["video_id"] == f"{VID}-04"
    db.update(f"{VID}-04", state="PREVIEW")
    assert db.next_actionable()["video_id"] == f"{VID}-01"


def test_progress_api_rejects_junk(client):
    assert client.get("/api/progress?video=../x").status_code == 404
