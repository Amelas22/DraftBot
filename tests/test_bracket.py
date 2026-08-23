"""Pure bracket maths: layout, advancement, placement."""
import pytest

from draft_organization.bracket import bracket_size, build_bracket, advance_pairs, final_placement


def test_bracket_size_rounds_up_to_a_power_of_two():
    assert bracket_size(4) == 4
    assert bracket_size(5) == 8
    assert bracket_size(6) == 8
    assert bracket_size(8) == 8
    assert bracket_size(9) == 16


def test_top_four_has_no_byes():
    assert build_bracket(4) == [(1, 4), (2, 3)]


def test_top_eight_is_in_bracket_order_not_seed_order():
    # (1,8) (4,5) (2,7) (3,6) -- NOT (1,8) (2,7) (3,6) (4,5). The order is
    # load-bearing: the next round pairs ADJACENT winners, so seed-sorted
    # pairings would put 1 and 2 together in the semifinal.
    assert build_bracket(8) == [(1, 8), (4, 5), (2, 7), (3, 6)]


def test_top_six_gives_byes_to_the_top_two_seeds():
    assert build_bracket(6) == [(1, None), (4, 5), (2, None), (3, 6)]


def test_bracket_needs_at_least_two_seeds():
    with pytest.raises(ValueError):
        build_bracket(1)


def test_advance_pairs_pairs_adjacent_winners_in_order():
    assert advance_pairs(["a", "b", "c", "d"]) == [("a", "b"), ("c", "d")]


def test_advance_pairs_rejects_an_odd_number_of_winners():
    with pytest.raises(ValueError):
        advance_pairs(["a", "b", "c"])


@pytest.mark.parametrize("size", range(4, 17))
def test_seeds_one_and_two_meet_only_in_the_final(size):
    """The property that makes seeding worth earning. Simulate the higher seed
    always winning and assert 1 and 2 first meet in the LAST round.

    This is the test that catches a plausible-looking but wrong pairing order:
    pairings sorted by top seed read more naturally and put 1 and 2 together
    in the semifinal instead.

    Every size the spec allows, not just the powers of two: the non-power
    sizes are the ones where byes shift who meets whom, so they are where a
    bad seat order would actually surface.
    """
    pairs = build_bracket(size)
    # Rounds are set by the FULL bracket, not the entry count: a top 6 plays
    # three rounds (two of its first-round seats are byes), not two.
    total_rounds = bracket_size(size).bit_length() - 1
    met_in = None
    for rnd in range(1, total_rounds + 1):
        for a, b in pairs:
            if {a, b} == {1, 2}:
                met_in = rnd
        # `b is None` is a bye, so `a` walks through.
        winners = [a if b is None else min(a, b) for a, b in pairs]
        if len(winners) > 1:
            pairs = advance_pairs(winners)
    assert met_in == total_rounds


def test_final_placement_orders_by_elimination_depth_then_seed():
    # Four-team bracket: round 1 = (1 beat 4), (2 beat 3); final = 1 beat 2.
    rounds = [[(1, 4), (2, 3)], [(1, 2)]]
    seeds = {1: 1, 2: 2, 3: 3, 4: 4}
    # champion, runner-up, then the two semifinal losers ordered by seed
    assert final_placement(rounds, seeds) == [1, 2, 3, 4]


def test_final_placement_ignores_byes():
    # Six-team bracket: seeds 1 and 2 had byes (loser is None).
    rounds = [[(1, None), (4, 5), (2, None), (3, 6)], [(1, 4), (2, 3)], [(1, 2)]]
    seeds = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}
    assert final_placement(rounds, seeds) == [1, 2, 3, 4, 5, 6]


def test_final_placement_ranks_still_live_teams_above_eliminated_ones():
    # Four-team bracket: semis decided (4 beat 1, 3 beat 2), final not yet
    # played -- only the semis round is passed in. Teams 3 and 4 are still
    # alive and must rank above 1 and 2, who are already eliminated. Ties
    # among the still-alive teams (nobody has lost) break by seed, same as
    # ties among eliminated teams in the same round.
    rounds = [[(4, 1), (3, 2)]]
    seeds = {1: 1, 2: 2, 3: 3, 4: 4}
    assert final_placement(rounds, seeds) == [3, 4, 1, 2]


def test_final_placement_never_lists_a_team_twice():
    """A corrected result can leave one team recorded as the loser of two
    rounds (e.g. an admin rewrites a semifinal after the final was played).
    Placement is a finishing ORDER, so listing that team twice hands the same
    captain two prize slots when compute_allocations walks it."""
    # A lost round 1 to B and round 2 to C -- contradictory, but reachable.
    rounds = [[("B", "A")], [("C", "A")]]
    seeds = {"A": 1, "B": 2, "C": 3}
    order = final_placement(rounds, seeds)
    assert order.count("A") == 1
    assert sorted(order) == ["A", "B", "C"]
