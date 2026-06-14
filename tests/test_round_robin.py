"""Tests for the pure round-robin schedule generator in draft_organization/swiss.py."""
import random
from itertools import combinations

import pytest

from draft_organization.swiss import round_robin_schedule


def all_pairs(rounds):
    return [frozenset(pair) for rnd in rounds for pair in rnd]


@pytest.mark.parametrize("n", [2, 3, 4, 5, 6, 7, 8])
def test_every_pair_plays_exactly_once(n):
    teams = list(range(n))
    rounds = round_robin_schedule(teams)
    pairs = all_pairs(rounds)
    expected = [frozenset(p) for p in combinations(teams, 2)]
    assert sorted(map(tuple, map(sorted, pairs))) == sorted(map(tuple, map(sorted, expected)))
    assert len(pairs) == n * (n - 1) // 2  # C(n,2), no duplicates


@pytest.mark.parametrize("n,expected_rounds", [(2, 1), (4, 3), (6, 5), (8, 7)])
def test_even_team_count_round_structure(n, expected_rounds):
    rounds = round_robin_schedule(list(range(n)))
    assert len(rounds) == expected_rounds
    for rnd in rounds:
        assert len(rnd) == n // 2  # everyone plays each round


@pytest.mark.parametrize("n,expected_rounds", [(3, 3), (5, 5), (7, 7)])
def test_odd_team_count_round_structure(n, expected_rounds):
    rounds = round_robin_schedule(list(range(n)))
    assert len(rounds) == expected_rounds
    for rnd in rounds:
        assert len(rnd) == (n - 1) // 2  # one team sits out each round (no bye match)


@pytest.mark.parametrize("n", [3, 4, 5, 6, 7, 8])
def test_no_team_plays_twice_in_a_round(n):
    rounds = round_robin_schedule(list(range(n)))
    for rnd in rounds:
        seen = [t for pair in rnd for t in pair]
        assert len(seen) == len(set(seen))


def test_rng_varies_pairing_but_keeps_validity():
    teams = list(range(6))
    a = round_robin_schedule(teams, random.Random(1))
    b = round_robin_schedule(teams, random.Random(2))
    # Both still complete and valid, even if the schedules differ.
    assert sorted(map(tuple, map(sorted, all_pairs(a)))) == sorted(map(tuple, map(sorted, all_pairs(b))))


def test_two_teams_single_match():
    rounds = round_robin_schedule([10, 20])
    assert rounds == [[(10, 20)]]
