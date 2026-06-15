"""Tests for the pure Swiss pairing engine (draft_organization/swiss.py)."""
import random

import pytest

from draft_organization.swiss import assign_bye, pair_round


def team(team_id, points=0, byes=0):
    return {"id": team_id, "points": points, "byes": byes}


def rng(seed=7):
    return random.Random(seed)


# ---- pair_round: basics -------------------------------------------------------

def test_round_one_pairs_everyone_exactly_once():
    teams = [team(i) for i in range(8)]
    pairs, bye = pair_round(teams, set(), rng())
    assert bye is None
    assert len(pairs) == 4
    seen = {tid for a, b in pairs for tid in (a, b)}
    assert seen == {0, 1, 2, 3, 4, 5, 6, 7}


def test_seeded_rng_makes_pairing_deterministic():
    teams = [team(i) for i in range(8)]
    first, _ = pair_round(teams, set(), rng(42))
    second, _ = pair_round(teams, set(), rng(42))
    assert first == second


def test_odd_count_yields_a_bye():
    teams = [team(i) for i in range(5)]
    pairs, bye = pair_round(teams, set(), rng())
    assert bye is not None
    assert len(pairs) == 2
    paired = {tid for a, b in pairs for tid in (a, b)}
    assert bye not in paired
    assert paired | {bye} == {0, 1, 2, 3, 4}


# ---- pair_round: swiss behaviour ------------------------------------------------

def test_pairs_within_points_groups():
    # Two clear score groups: winners (3 pts) and losers (0 pts)
    teams = [team(1, 3), team(2, 3), team(3, 0), team(4, 0)]
    pairs, _ = pair_round(teams, set(), rng())
    normalized = {frozenset(p) for p in pairs}
    assert frozenset({1, 2}) in normalized
    assert frozenset({3, 4}) in normalized


def test_avoids_rematches_when_possible():
    teams = [team(1, 3), team(2, 3), team(3, 0), team(4, 0)]
    history = {frozenset({1, 2}), frozenset({3, 4})}
    pairs, _ = pair_round(teams, history, rng())
    normalized = {frozenset(p) for p in pairs}
    assert frozenset({1, 2}) not in normalized
    assert frozenset({3, 4}) not in normalized


def test_allows_rematch_when_unavoidable():
    teams = [team(1, 3), team(2, 0)]
    history = {frozenset({1, 2})}
    pairs, bye = pair_round(teams, history, rng())
    assert bye is None
    assert {frozenset(p) for p in pairs} == {frozenset({1, 2})}


def test_no_rematch_across_score_groups_with_backtracking():
    # 1v2 and 3v4 already played; only cross pairings remain legal
    teams = [team(1, 3), team(2, 3), team(3, 3), team(4, 3)]
    history = {frozenset({1, 2}), frozenset({3, 4})}
    pairs, _ = pair_round(teams, history, rng())
    normalized = {frozenset(p) for p in pairs}
    assert frozenset({1, 2}) not in normalized
    assert frozenset({3, 4}) not in normalized
    assert len(normalized) == 2


# ---- assign_bye -----------------------------------------------------------------

def test_bye_goes_to_lowest_points_among_fewest_byes():
    teams = [team(1, 6, byes=0), team(2, 3, byes=0), team(3, 0, byes=1)]
    assert assign_bye(teams, rng()) == 2  # team 3 has a bye already; team 2 is lowest without


def test_bye_prefers_fewest_byes_even_at_higher_points():
    teams = [team(1, 6, byes=0), team(2, 0, byes=1), team(3, 0, byes=1)]
    assert assign_bye(teams, rng()) == 1


def test_everyone_engaged_falls_back_to_lowest_points():
    teams = [team(1, 6, byes=1), team(2, 3, byes=1), team(3, 0, byes=1)]
    assert assign_bye(teams, rng()) == 3
