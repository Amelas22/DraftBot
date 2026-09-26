"""Tests for the pure OMW% ranking functions in draft_organization/swiss.py."""
from types import SimpleNamespace

import pytest

from draft_organization.swiss import (
    match_win_percentage,
    omw_percentages,
    rank_standings,
)

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


# ---- omw_percentages: the same numbers, exposed for display ----------------------

def test_omw_percentages_averages_real_opponents():
    # T1 played one 1-0 opponent (MWP 1.0) and one 0-1 opponent (floored at 1/3).
    p = participant(1, points=3, w=1, l=1)
    strong = participant(2, points=3, w=1)
    weak = participant(3, points=0, l=1)
    matches = [match(1, 2), match(1, 3)]

    omw = omw_percentages([p, strong, weak], matches)

    assert omw[1] == pytest.approx((1.0 + FLOOR) / 2)


def test_omw_percentages_ignores_byes():
    # A bye is not an opponent, so a team whose only other game was a bye
    # still sits at the floor rather than being credited with one.
    p = participant(1, points=3, w=1)
    matches = [match(1, None, is_bye=True)]

    assert omw_percentages([p], matches)[1] == pytest.approx(FLOOR)


# ---- rank_standings: teams part-way through a round ------------------------------

def test_fewer_losses_outranks_at_equal_points_mid_round():
    """A 2-1 team ranks above a 2-2 team, even with a worse OMW%.

    Standings update live, so the field is comparing teams that have played
    different numbers of rounds. Both of these hold 6 points; the one that
    still has a round in hand is ahead on the only reading that matters --
    it cannot yet have lost twice.
    """
    ahead = participant(1, points=6, w=2, l=1, gw=10, gl=6, name="RoundInHand")
    behind = participant(2, points=6, w=2, l=2, gw=10, gl=6, name="Complete")
    weak = participant(3, points=0, l=3, name="Weak")
    strong = participant(4, points=9, w=3, name="Strong")
    # behind played the stronger opponent, so OMW alone would put it first.
    matches = [match(1, 3), match(2, 4)]

    ranked = rank_standings([behind, ahead, weak, strong], matches)

    assert ranked.index(ahead) < ranked.index(behind)


def test_a_round_in_hand_beats_more_rounds_played_even_with_draws():
    """The tiebreak is rounds played, not losses -- they diverge on a draw.

    Both hold 3 points. The first has played two rounds and still has one in
    hand; the second has spent three rounds to get there. Ranking on losses
    inverts this pair, because three draws cost no losses at all -- and draws
    are reachable: _apply_result records one whenever a team match ends level.
    """
    in_hand = participant(1, points=3, w=1, l=1, name="RoundInHand")
    played_more = participant(2, points=3, d=3, name="PlayedMore")

    ranked = rank_standings([played_more, in_hand], [])

    assert ranked.index(in_hand) < ranked.index(played_more)


# ---- pairing_order: rank for PAIRING, which is not rank for display ----------

def _order(participants, matches, seed=0):
    import random
    from draft_organization.swiss import pairing_order
    return [p.id for p in pairing_order(participants, matches, random.Random(seed))]


def test_pairing_order_leads_with_points():
    low, high = participant(1, points=3), participant(2, points=6)

    assert _order([low, high], []) == [2, 1]


def test_pairing_order_uses_the_same_tiebreaks_the_board_shows():
    """Two teams level on points, one with a tougher road. The board already
    ranks them; pairing must agree, or the down-pair contradicts the standings
    players are reading it from."""
    strong = participant(1, points=3, w=1, gw=2, name="Strong")
    weak = participant(2, points=3, w=1, gw=2, name="Weak")
    a_winner = participant(3, points=3, w=1, gw=2)      # strong's opponent
    a_loser = participant(4, points=0, l=1, gl=2)       # weak's opponent
    matches = [match(1, 3), match(2, 4)]

    # Asserted as a relative order, not an index: `strong` and `a_winner` are
    # level on every key including OMW, so which of them leads is the random
    # tiebreak doing its job -- and pinning it would test the seed.
    order = _order([weak, strong, a_winner, a_loser], matches)

    assert order.index(1) < order.index(2), \
        "the team that beat a winner outranks the team that beat a loser"


def test_an_exact_tie_is_broken_randomly_not_alphabetically():
    """Round one, where nobody has played and every tiebreak is level.

    The display sort ends on team_name so the board holds still between
    refreshes. Pairing must NOT: alphabetical pairings are fixed before a card
    is drawn, and anyone who notices can pick their team name to choose an
    opponent.
    """
    field = [participant(i, name=chr(ord("A") + i)) for i in range(8)]

    seen = {tuple(_order(field, [], seed=s)) for s in range(30)}

    assert len(seen) > 1, "round one pairing order must not be deterministic"


def test_the_board_still_breaks_that_same_tie_by_name():
    """The other half of the split: rank_standings stays stable."""
    field = [participant(i, name=chr(ord("Z") - i)) for i in range(4)]

    twice = [[p.id for p in rank_standings(field, [])] for _ in range(2)]

    assert twice[0] == twice[1]
    assert [p.team_name for p in rank_standings(field, [])] == ["W", "X", "Y", "Z"]


# ---- every ranking key has to actually decide something --------------------
#
# Each of these was, until it was written, a key the suite would let you delete
# in silence. They assert the order is the same for EVERY seed, not for one:
# delete the key and the two teams become exactly tied, at which point the
# random tiebreak returns the expected order about half the time -- so a
# single-seed assertion here passes by luck and proves nothing.

SEEDS = range(40)


def _always(participants, matches, expected):
    orders = {tuple(_order(participants, matches, seed=s)) for s in SEEDS}
    assert orders == {tuple(expected)}, f"not decided by rank: {orders}"


def test_a_round_in_hand_outranks_a_round_already_spent():
    """Standings update live. Two teams on the same points, one of whom has
    not played this round yet, are not equal -- the one with a round in hand
    got there in fewer games and must not be ranked beneath the other."""
    _always([participant(1, points=3, w=1, l=1),
             participant(2, points=3, w=1)], [], [2, 1])


def test_game_differential_separates_teams_level_on_everything_else():
    same = dict(points=3, w=1, l=0)
    _always([participant(1, gw=2, gl=1, **same),
             participant(2, gw=2, gl=0, **same)], [], [2, 1])


def test_the_omw_handed_in_is_the_omw_ranked_on():
    """`pairing_order` takes a precomputed OMW map because the caller has to
    compute it over the WHOLE field -- dropped teams included -- while ranking
    only the teams being paired. If the map were quietly recomputed from the
    teams handed in, every dropped opponent would vanish from the tiebreak and
    the bug that argument exists to prevent would be back.

    Proved by handing in a map that contradicts what recomputation would give:
    the order must follow the map, for every seed.
    """
    import random
    from draft_organization.swiss import pairing_order
    field = [participant(1, points=3, w=1), participant(2, points=3, w=1)]

    orders = {tuple(p.id for p in
                    pairing_order(field, [], random.Random(s), omw={1: 0.0, 2: 1.0}))
              for s in SEEDS}

    assert orders == {(2, 1)}, f"the supplied map must decide the order: {orders}"
