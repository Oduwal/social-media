import pytest

from clipper.cutter import choose_cut


def test_snaps_start_to_end_of_pause_and_end_to_start_of_pause():
    silences = [(9.0, 10.2), (39.5, 40.4)]
    start, end = choose_cut(10.8, 38.6, duration=60, silences=silences, min_s=15, max_s=45)
    assert start == pytest.approx(10.2 - 0.3)
    assert end == pytest.approx(39.5 + 0.3)


def test_no_pause_nearby_keeps_gemini_times():
    start, end = choose_cut(10, 28, duration=60, silences=[(50, 51)], min_s=15, max_s=45)
    assert (start, end) == (10, 28)


def test_clamps_to_video_length():
    start, end = choose_cut(-3, 80, duration=38, silences=[], min_s=15, max_s=45)
    assert (start, end) == (0, 38)


def test_extends_short_clip_to_minimum():
    start, end = choose_cut(20, 25, duration=60, silences=[], min_s=15, max_s=45)
    assert end - start == pytest.approx(15)
    assert start == 20


def test_extends_backwards_near_the_end():
    start, end = choose_cut(55, 59, duration=60, silences=[], min_s=15, max_s=45)
    assert (start, end) == (45, 60)


def test_trims_long_clip_on_a_pause():
    silences = [(30.0, 30.5), (40.0, 40.6)]
    start, end = choose_cut(0, 58, duration=60, silences=silences, min_s=15, max_s=45)
    assert start == 0
    # the pause at 40.0 (+0.3 pad) would leave a 0.3 s sliver of a 5th segment
    assert end == pytest.approx(39.9)
    assert end - start <= 45


def test_video_shorter_than_minimum_uses_whole_video():
    assert choose_cut(2, 5, duration=12, silences=[], min_s=15, max_s=45) == (0.0, 12)


def test_rejects_nonsense_range():
    with pytest.raises(ValueError):
        choose_cut(30, 30.2, duration=60, silences=[], min_s=15, max_s=45)


def test_trims_sliver_over_a_segment_multiple():
    start, end = choose_cut(0, 30.4, duration=60, silences=[], min_s=15, max_s=45)
    assert (start, end) == (0, 29.9)


def test_keeps_sliver_when_trimming_would_go_under_minimum():
    start, end = choose_cut(0, 10.5, duration=60, silences=[], min_s=10.5, max_s=45)
    assert (start, end) == (0, 10.5)
