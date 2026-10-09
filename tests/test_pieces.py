"""Colour matching between pieces, and redoing a single piece."""

import pytest
from fastapi.testclient import TestClient

from clipper import app as webapp
from clipper import config, db, media, transform_adapter
from tests.conftest import needs_ffmpeg

VID = "7412345678901234567"
CID = f"{VID}-01"


def _grey_clip(path, level, seconds=2):
    """A flat grey clip with a given brightness, standing in for a piece's skin tone."""
    media.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
               f"color=c=0x{level:02x}{level:02x}{level:02x}:s=360x640:d={seconds}:r=30",
               "-f", "lavfi", "-i", f"sine=duration={seconds}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-shortest", path])
    return path


@needs_ffmpeg
def test_colour_matching_pulls_pieces_to_the_middle(tmp_path):
    work = tmp_path / "transform_work"
    work.mkdir()
    pieces = []
    for i, level in enumerate((150, 160, 185), start=1):
        _grey_clip(work / f"e{i}.mp4", level)
        pieces.append({"index": i, "kind": "albino", "file": f"transform_work/e{i}.mp4", "cache": None})
    pieces.append({"index": 4, "kind": "sliver", "file": "transform_work/e1.mp4", "cache": None})
    result = transform_adapter.match_colours(pieces, tmp_path)
    after = [media.skin_stats(tmp_path / p["file"])[0] for p in pieces[:3]]
    assert max(after) - min(after) < 4, after                 # evened out
    assert pieces[1]["file"] == "transform_work/e2.mp4"       # the middle one is the target: untouched
    assert pieces[0]["file"].endswith("matched_001.mp4") and pieces[0]["shift"][0] > 0   # lightened
    assert pieces[2]["shift"][0] < 0                          # darkened
    assert pieces[3]["file"] == "transform_work/e1.mp4" and "shift" not in pieces[3]     # slivers left alone
    assert result["target"][0] == pytest.approx(media.skin_stats(work / "e2.mp4")[0], abs=0.5)


@needs_ffmpeg
def test_shifts_are_gentle(tmp_path):
    work = tmp_path / "transform_work"
    work.mkdir()
    pieces = [{"index": i, "kind": "albino", "file": f"transform_work/e{i}.mp4", "cache": None}
              for i in (1, 2, 3)]
    for p, level in zip(pieces, (60, 200, 210)):
        _grey_clip(tmp_path / p["file"], level)
    transform_adapter.match_colours(pieces, tmp_path)
    assert pieces[0]["shift"][0] == transform_adapter.MAX_LIGHT_SHIFT  # capped, never repainted


def test_nothing_to_match_with_one_piece(tmp_path):
    assert transform_adapter.match_colours([{"index": 1, "kind": "albino", "file": "x", "cache": None}],
                                           tmp_path) is None


@pytest.fixture
def client(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    return TestClient(webapp.app)


@needs_ffmpeg
def test_redo_one_piece_deletes_only_its_cached_edit(client, fresh_db, monkeypatch):
    monkeypatch.setattr(transform_adapter, "backend", lambda cfg: "vertex")  # made and redone on Vertex
    db.insert_if_new(VID, "u", state="CLIPPED")
    db.insert_clip(CID, db.get(VID), {"start_s": 0, "end_s": 20})
    job_dir = config.job_dir(CID)
    edited = job_dir / "transform_work" / "edited"
    edited.mkdir(parents=True)
    for i in (1, 2):
        _grey_clip(edited / f"edited_00{i}_albinism-x.mp4", 150)
    _grey_clip(job_dir / "final.mp4", 150)
    pieces = [{"index": i, "kind": "albino", "file": f"transform_work/edited/edited_00{i}_albinism-x.mp4",
               "cache": f"transform_work/edited/edited_00{i}_albinism-x.mp4"} for i in (1, 2)]
    pieces.append({"index": 3, "kind": "sliver", "file": "final.mp4", "cache": None})
    db.update(CID, state="READY", cut_range={"start": 0, "end": 20},
              transform_info={"enabled": True, "segments": 3, "fallback_segments": [], "refused": False,
                              "pieces": pieces, "colour_match": {"target": [150, 128, 128]}})
    page = client.get(f"/jobs/{CID}").text
    assert "The 3 pieces" in page and "Redo this piece" in page and f"/media/{CID}/piece/1" in page
    assert client.get(f"/media/{CID}/piece/1").status_code == 200
    assert client.post(f"/jobs/{CID}/redo_piece", data={"index": "3"}).status_code == 409  # sliver
    client.post(f"/jobs/{CID}/redo_piece", data={"index": "2"})
    assert not (edited / "edited_002_albinism-x.mp4").exists()
    assert (edited / "edited_001_albinism-x.mp4").exists()   # kept: reused for free
    assert db.get(CID)["state"] == "CUT"


@needs_ffmpeg
def test_redo_one_piece_refuses_after_switching_backend(client, fresh_db, monkeypatch):
    db.insert_if_new(VID, "u", state="CLIPPED")
    db.insert_clip(CID, db.get(VID), {"start_s": 0, "end_s": 20})
    edited = config.job_dir(CID) / "transform_work" / "edited"
    edited.mkdir(parents=True)
    _grey_clip(edited / "edited_001_albinism-x.mp4", 150)
    db.update(CID, state="READY", transform_info={"enabled": True, "backend": "vertex", "pieces": [
        {"index": 1, "kind": "albino", "file": "transform_work/edited/edited_001_albinism-x.mp4",
         "cache": "transform_work/edited/edited_001_albinism-x.mp4"}]})
    monkeypatch.setattr(transform_adapter, "backend", lambda cfg: "flow")
    resp = client.post(f"/jobs/{CID}/redo_piece", data={"index": "1"})
    assert resp.status_code == 409 and "every piece" in resp.text
    assert (edited / "edited_001_albinism-x.mp4").exists() and db.get(CID)["state"] == "READY"


def test_piece_route_rejects_paths_outside_the_clip(client, fresh_db):
    db.insert_if_new(VID, "u", state="CLIPPED")
    db.insert_clip(CID, db.get(VID), {"start_s": 0, "end_s": 20})
    db.update(CID, transform_info={"pieces": [{"index": 1, "kind": "albino", "file": "../../secret.mp4",
                                               "cache": None}]})
    assert client.get(f"/media/{CID}/piece/1").status_code == 404
