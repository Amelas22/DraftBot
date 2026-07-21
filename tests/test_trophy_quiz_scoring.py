from services.trophy_quiz_service import score_submission, record_label


def test_all_correct_maxes_at_10():
    r = score_submission([3, 0], [3, 0])
    assert r["direction_correct"] is True
    assert r["direction_points"] == 4
    assert r["exact_points"] == [3, 3]
    assert r["total"] == 10


def test_exact_is_flat_regardless_of_record():
    # The whole point: extreme and middle exacts are worth the SAME (+3), so the
    # max is constant (10) and a shared score can't reveal the records.
    assert score_submission([3, 0], [3, 0])["exact_points"] == [3, 3]
    assert score_submission([2, 1], [2, 1])["exact_points"] == [3, 3]
    assert score_submission([2, 1], [2, 1])["total"] == 10


def test_one_record_off():
    # A=3-0 nailed (+3), B guessed 1-2 but actual 0-3 -> B wrong; direction right.
    r = score_submission([3, 1], [3, 0])
    assert r["direction_correct"] is True
    assert r["exact_points"] == [3, 0]
    assert r["total"] == 7


def test_direction_only_no_exact():
    r = score_submission([2, 1], [3, 0])
    assert r["direction_correct"] is True
    assert r["exact_points"] == [0, 0]
    assert r["total"] == 4


def test_wrong_direction_scores_zero():
    r = score_submission([1, 2], [3, 0])
    assert r["direction_correct"] is False
    assert r["total"] == 0


def test_tie_guess_no_direction_but_exact_still_counts():
    # actual A=2-1, B=1-2; guess 2 & 2 -> tie (no direction), A exact (+3).
    r = score_submission([2, 2], [2, 1])
    assert r["direction_correct"] is False
    assert r["exact_points"] == [3, 0]
    assert r["total"] == 3


def test_record_label():
    assert [record_label(w) for w in (3, 2, 1, 0)] == ["3-0", "2-1", "1-2", "0-3"]
