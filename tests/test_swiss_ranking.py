"""Tests for the pure OMW% ranking functions in draft_organization/swiss.py."""
from types import SimpleNamespace

import pytest

from draft_organization.swiss import match_win_percentage, rank_standings

FLOOR = 1 / 3


def participant(pid, points=0, w=0, l=0, d=0, gw=0, gl=0, name=None):
    return SimpleNamespace(
        id=pid, points=points, match_wins=w, match_losses=l, match_draws=d,
        game_wins=gw, game_losses=gl, team_name=name or f"T{pid}",
    )


def match(a, b, is_bye=False):
    return SimpleNamespace(team_a_participant_id=a, team_b_participant_id=b, is_bye=is_bye)


# ---- match_win_percentage ---------------------------------------------------------

def test_mwp_normal():
    # 2 wins (6 pts) over 3 rounds -> 6/9
    assert match_win_percentage(6, 3) == pytest.approx(6 / 9)


def test_mwp_floor_for_winless():
    assert match_win_percentage(0, 3) == pytest.approx(FLOOR)


def test_mwp_zero_rounds_returns_floor():
    assert match_win_percentage(0, 0) == pytest.approx(FLOOR)


# ---- rank_standings: OMW% as the tiebreaker --------------------------------------

def test_omw_breaks_a_points_tie():
    # P_strong and P_weak both 3 pts; P_strong played a winner, P_weak a loser.
    p_strong = participant(1, points=3, w=1, gw=2, name="Strong")
    p_weak = participant(2, points=3, w=1, gw=2, name="Weak")
    o_good = participant(3, points=3, w=1, gw=2, name="Good")
    o_bad = participant(4, points=0, l=1, gl=2, name="Bad")
    parts = [p_weak, p_strong, o_bad, o_good]  # unsorted input
    matches = [match(1, 3), match(2, 4)]

    ranked = rank_standings(parts, matches)
    # Strong (OMW 1.0) outranks Weak (OMW 0.33) despite equal points
    assert ranked.index(p_strong) < ranked.index(p_weak)


def test_byes_excluded_from_opponents():
    # P took a bye and a real match vs a strong opponent.
    p = participant(1, points=6, w=2, gw=2, name="P")
    opp = participant(2, points=3, w=1, gw=2, name="Opp")
    other = participant(3, points=0, l=1, gl=2, name="Other")
    matches = [match(1, None, is_bye=True), match(1, 2), match(3, None, is_bye=True)]

    ranked = rank_standings([p, opp, other], matches)
    # P's OMW% must come only from Opp (1.0), not diluted by the bye.
    assert ranked[0] is p


def test_no_real_opponents_uses_floor():
    # A team whose only game was a bye has no opponents -> OMW% floors, doesn't crash.
    only_bye = participant(1, points=3, w=1, name="ByeOnly")
    played = participant(2, points=3, w=1, gw=2, name="Played")
    opp = participant(3, points=0, l=1, gl=2, name="Opp")
    matches = [match(1, None, is_bye=True), match(2, 3)]

    ranked = rank_standings([only_bye, played, opp], matches)
    # Played (OMW 0.33 from a 0-pt opp) ties only_bye (OMW floor 0.33) on OMW;
    # both 3 pts, so fall through to game diff: Played (+2) over only_bye (0).
    assert ranked.index(played) < ranked.index(only_bye)


def test_falls_through_to_game_diff_then_name():
    # Equal points and equal OMW% -> game diff, then name.
    a = participant(1, points=3, w=1, gw=2, gl=0, name="Alpha")
    b = participant(2, points=3, w=1, gw=2, gl=1, name="Bravo")
    oa = participant(3, points=0, l=1, name="OppA")
    ob = participant(4, points=0, l=1, name="OppB")
    matches = [match(1, 3), match(2, 4)]

    ranked = rank_standings([b, a, ob, oa], matches)
    # a and b: equal pts(3), equal OMW(0.33). a has better game diff(+2 vs +1).
    assert ranked.index(a) < ranked.index(b)
