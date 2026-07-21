from types import SimpleNamespace
from services.trophy_quiz_service import compute_records


def _m(p1, p2, winner):
    return SimpleNamespace(player1_id=p1, player2_id=p2, winner_id=winner)


def test_counts_wins_matches_and_reported():
    matches = [_m("a", "b", "a"), _m("a", "c", "a"), _m("a", "d", "a"),
               _m("b", "c", "b"), _m("b", "d", "b")]
    recs = compute_records(matches)
    assert recs["a"] == {"wins": 3, "matches": 3, "reported": 3}
    assert recs["b"]["wins"] == 2 and recs["b"]["matches"] == 3


def test_unreported_match_lowers_reported_not_matches():
    # a plays 3 but the middle one has no winner -> matches 3, reported 2, wins 2
    matches = [_m("a", "b", "a"), _m("a", "c", None), _m("a", "d", "a")]
    recs = compute_records(matches)
    assert recs["a"] == {"wins": 2, "matches": 3, "reported": 2}
    assert recs["c"] == {"wins": 0, "matches": 1, "reported": 0}
