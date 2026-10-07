"""Auto captions (grouping HyperFrames' word timings) and the caption effects the fast
editor renders with libass. HyperFrames itself isn't run here (conftest blocks it)."""

import json

import pytest

from clipper import captions, media, render
from tests.conftest import make_clip, needs_ffmpeg


def _w(text, start, end):
    return {"text": text, "start": start, "end": end}


WORDS = [_w("Time", 0.24, 0.56), _w("go", 0.72, 0.88), _w("to", 1.04, 1.2), _w("Mangolin.", 1.68, 2.8),
         _w("What", 4.08, 4.3), _w("is", 4.35, 4.5), _w("a", 4.55, 4.6), _w("status", 4.65, 5.0),
         _w("boyfriend?", 5.05, 5.6)]


def test_words_are_grouped_into_short_captions():
    groups = [" ".join(w["text"] for w in g) for g in captions.group(WORDS)]
    # max 3 words, break after punctuation, and after a long pause
    assert groups == ["Time go to", "Mangolin.", "What is a", "status boyfriend?"]


def test_captions_are_timed_from_the_trim_and_carry_word_timings(tmp_path, monkeypatch):
    monkeypatch.setattr(captions, "transcribe", lambda job_dir: WORDS)
    texts = captions.captions_for(tmp_path, 1.0, 6.0)   # trimmed: starts at 1 s
    assert [t["text"] for t in texts][:2] == ["to Mangolin.", "What is a"]  # words before the trim are left out
    first = texts[0]
    assert first["start"] == pytest.approx(0.04) and first["auto"] and first["animation"] == "highlight"
    assert first["words"] == [[pytest.approx(0.04), pytest.approx(0.2)], [pytest.approx(0.68), pytest.approx(1.8)]]
    for a, b in zip(texts, texts[1:]):
        assert a["end"] <= b["start"] + 1e-6           # never overlap
    # every caption survives the editor's own validation, word timings included
    edit = render.clean_edit({**render.default_edit(5.0), "texts": texts}, 5.0, set())
    assert len(edit["texts"]) == len(texts) and edit["texts"][-1]["words"]


def test_transcript_is_reused_until_the_clip_changes(tmp_path, monkeypatch):
    video = tmp_path / "transformed.mp4"
    video.write_bytes(b"x")
    (tmp_path / captions.TRANSCRIPT).write_text(json.dumps(
        {"source_mtime": video.stat().st_mtime, "words": WORDS}), encoding="utf-8")
    monkeypatch.setattr(captions, "_transcribe", lambda *a: pytest.fail("should use the saved transcript"))
    assert captions.transcribe(tmp_path) == WORDS


def test_retyped_caption_drops_word_timings_but_fixing_a_word_keeps_them():
    base = {"start": 0, "end": 2, "words": [[0, 0.5], [0.6, 1.0]]}
    fixed = render.clean_edit({**render.default_edit(5), "texts": [{**base, "text": "Mango lane"}]}, 5, set())
    assert fixed["texts"][0]["words"] == [[0, 0.5], [0.6, 1.0]]
    retyped = render.clean_edit({**render.default_edit(5), "texts": [{**base, "text": "Mango lane road"}]}, 5, set())
    assert retyped["texts"][0]["words"] is None


def _item(**kw):
    item = {"text": "What is a status", "start": 1.0, "end": 3.0, "style": "caption", "position": "bottom",
            "size": 84, "color": "#FFFFFF", "animation": "pop", "accent": "#FFE600",
            "words": [[1.0, 1.3], [1.4, 1.6], [1.7, 1.8], [2.0, 2.6]], "auto": True}
    item.update(kw)
    return item


def test_highlight_lights_up_the_word_being_spoken():
    events = render._events(0, _item(animation="highlight"))
    assert len(events) == 4                                   # one per word, back to back
    assert events[0].startswith("Dialogue: 0,0:00:01.00,0:00:01.40")
    assert "{\\1c&H00E6FF&\\fscx108\\fscy108}status" in events[3]   # yellow on the 4th word
    assert events[3].split(",")[2] == "0:00:03.00"


def test_words_appear_as_they_are_spoken():
    events = render._events(0, _item(animation="words"))
    assert len(events) == 4                                   # one line per word revealed
    assert events[1].startswith("Dialogue: 0,0:00:01.40,0:00:01.70")
    assert events[1].endswith(r"What is {\alpha&HFF&}a status")   # the rest is there, but invisible
    assert "alpha" not in events[-1]


def test_typewriter_slide_and_bounce_tags():
    events = render._events(0, _item(animation="typewriter"))
    assert len(events) == len("What is a status") and events[-1].endswith("}What is a status")
    assert r"\move(540,1640,540,1500,0,260)" in render._events(0, _item(animation="slide"))[0]
    assert r"\fscx118" in render._events(0, _item(animation="bounce"))[0]


@needs_ffmpeg
def test_every_effect_and_style_renders(tmp_path):
    make_clip(tmp_path / "transformed.mp4", seconds=4, size="1080x1920")
    texts = [_item(animation=a, style=s, start=0.2, end=3.5, position=p)
             for a, s, p in zip(render.ANIMATIONS, list(render.TEXT_STYLES) * 2, list(render.POSITIONS) * 3)]
    edit = {**render.default_edit(4.0), "texts": texts}
    out = render.render(render.clean_edit(edit, 4.0, set()), tmp_path)
    assert media.duration(out) == pytest.approx(4.0, abs=0.1)
