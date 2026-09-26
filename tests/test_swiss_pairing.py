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


# ---- power pairing and the bracket boundary ----------------------------------
#
# Teams arrive in PAIRING ORDER (best first); `pair_round` no longer decides
# rank for itself. What it decides is where rank is allowed to matter:
# everywhere when `power_pair`, and only at the two boundary seats otherwise.

def test_the_lowest_of_a_bracket_pairs_down_onto_the_highest_below():
    """The down-pair is the whole point of ranking a normal round.

    Left random, the contender forced across the boundary is drawn uniformly
    from its bracket and lands on a uniformly random opponent below -- so the
    team with the most to lose can be sent against the weakest team with
    nothing to play for. Lowest-of-upper onto highest-of-lower is the pairing
    both halves have the least cause to complain about.
    """
    teams = [team("A", 6), team("B", 6), team("C", 6),      # C is lowest of the 6s
             team("D", 3), team("E", 3), team("F", 3)]      # D is highest of the 3s

    for seed in range(50):
        pairs, _ = pair_round(teams, set(), rng(seed))
        crossing = [p for p in pairs if {p[0], p[1]} & {"A", "B", "C"}
                    and {p[0], p[1]} & {"D", "E", "F"}]
        assert crossing == [("C", "D")], f"seed {seed}: {pairs}"


def test_a_normal_round_still_varies_inside_the_bracket():
    """Only the two boundary seats are pinned. Rank deciding every seat is a
    bigger change than the problem asked for, and it costs early rounds the
    variety that makes a league worth playing."""
    teams = [team(n, 3) for n in "ABCDE"] + [team("Z", 0)]

    seen = {tuple(sorted(map(tuple, pair_round(teams, set(), rng(s))[0])))
            for s in range(50)}

    assert len(seen) > 1, "the middle of a bracket must not be fixed"


def test_power_pairing_pairs_rank_adjacent_and_does_not_vary():
    """The final round, where the standings are the thing being decided."""
    teams = [team(n, 3) for n in "ABCD"]

    for seed in range(50):
        pairs, _ = pair_round(teams, set(), rng(seed), power_pair=True)
        assert sorted(map(tuple, pairs)) == [("A", "B"), ("C", "D")], seed


def test_power_pairing_still_refuses_a_rematch():
    """Rank orders the field; it does not override the one hard constraint."""
    teams = [team(n, 3) for n in "ABCD"]
    history = {frozenset(("A", "B")), frozenset(("C", "D"))}

    pairs, _ = pair_round(teams, history, rng(), power_pair=True)

    assert all(frozenset(p) not in history for p in pairs), pairs


# ---- weighted matching: the pairing is OPTIMAL, not merely plausible --------
#
# The previous implementation took the first rematch-free matching its greedy
# search happened to reach, and the commit claimed that amounted to pairing the
# lowest team of a bracket down onto the highest team below. It did not: the
# search abandons the boundary whenever backtracking takes it elsewhere, even
# when a rematch-free matching preserving the boundary exists. These pin the
# property properly -- by scoring every alternative.

def _every_matching(ids):
    """Every perfect matching of `ids`, as lists of pairs."""
    if not ids:
        yield []
        return
    first, rest = ids[0], ids[1:]
    for i, partner in enumerate(rest):
        for tail in _every_matching(rest[:i] + rest[i + 1:]):
            yield [(first, partner)] + tail


def test_a_feasible_boundary_is_never_abandoned():
    """Codex's counterexample, as a regression.

    Upper bracket A-E, lower F-H, with C-D, D-F and F-H already played. The
    old search returned D-H -- the fourth team of the upper bracket against the
    LAST team of the lower one, the worst boundary available -- while
    A-C, B-D, E-F, G-H is rematch-free and pairs the lowest of the upper
    bracket onto the highest of the lower.
    """
    teams = [team(c, 6 if c in "ABCDE" else 3) for c in "ABCDEFGH"]
    history = set(map(frozenset, ["CD", "DF", "FH"]))

    for pp in (False, True):
        pairs, _ = pair_round(teams, history, rng(5), power_pair=pp)
        crossing = [p for p in pairs
                    if ({p[0], p[1]} & set("ABCDE")) and ({p[0], p[1]} & set("FGH"))]
        assert crossing == [("E", "F")], f"power_pair={pp}: {pairs}"
        assert not [p for p in pairs if frozenset(p) in history], pairs


@pytest.mark.parametrize("power_pair", [False, True])
def test_the_matching_returned_is_the_best_one_available(power_pair):
    """Exhaustive check, at a size where exhaustive is possible.

    For every field below, score EVERY perfect matching against the same
    weights the pairer uses and confirm the one it returned ties the maximum.
    This is what the old implementation could not offer: it is a proof for
    these fields rather than a spot check of cases somebody thought of.
    """
    from draft_organization.swiss import pairing_weights
    import random as _r

    for seed in range(300):
        r = _r.Random(seed)
        n = r.choice([4, 6, 8, 10])
        teams = [team(i, 3 * r.randint(0, 3)) for i in range(n)]
        history = {frozenset(p) for p in
                   (tuple(r.sample(range(n), 2)) for _ in range(r.randint(0, n)))}

        weights = pairing_weights(teams, history, _r.Random(seed), power_pair)
        pairs, _ = pair_round(teams, history, _r.Random(seed), power_pair=power_pair)

        score = lambda m: sum(weights[frozenset(p)] for p in m)
        best = max(score(m) for m in _every_matching([t["id"] for t in teams]))
        assert score(pairs) == best, f"seed {seed}: {pairs} scored {score(pairs)} < {best}"


def test_an_unavoidable_rematch_is_minimised_not_surrendered_to():
    """The old code gave up and paired in listed order once no rematch-free
    matching existed, which could repeat several pairings at once. One
    unavoidable rematch should cost exactly one."""
    teams = [team(i) for i in range(4)]
    # 0 can only face 3; 1 and 2 have played each other. One rematch is forced.
    history = {frozenset((0, 1)), frozenset((0, 2)), frozenset((1, 2))}

    pairs, _ = pair_round(teams, history, rng())

    assert sum(1 for p in pairs if frozenset(p) in history) == 1, pairs


def _criteria(teams, history, pairs, power_pair):
    """The pairing rules as a plain tuple, strongest first -- written out
    independently of the weights, so it can check them."""
    rank = {t["id"]: i for i, t in enumerate(teams)}
    points = {t["id"]: t["points"] for t in teams}
    order = sorted({t["points"] for t in teams}, reverse=True)
    members = {}
    for t in teams:
        members.setdefault(t["points"], []).append(t["id"])
    from_top = {m: i for g in members.values() for i, m in enumerate(g)}
    from_bottom = {m: len(g) - 1 - i for g in members.values() for i, m in enumerate(g)}

    def boundary(a, b):
        """How close a crossing sits to the bracket edge; 0 inside a bracket."""
        if points[a] == points[b]:
            return 0
        upper, lower = (a, b) if points[a] > points[b] else (b, a)
        return -(from_bottom[upper] + from_top[lower])

    legal = sum(0 if frozenset(p) in history else 1 for p in pairs)
    edge = sum(boundary(a, b) for a, b in pairs)
    if power_pair:
        return (legal, sum(len(teams) - abs(rank[a] - rank[b]) for a, b in pairs), edge)
    return (legal,
            sum(1 for a, b in pairs if points[a] == points[b]),
            -sum(abs(order.index(points[a]) - order.index(points[b])) for a, b in pairs),
            edge)


@pytest.mark.parametrize("power_pair", [False, True])
def test_the_weights_really_do_rank_the_criteria_in_order(power_pair):
    """The other optimality test scores the result against the same weights it
    was chosen by, so it cannot catch the weights themselves being wrong.

    The criteria are a strict hierarchy: one unit of a stronger criterion must
    beat every weaker one added together across the whole round. That holds
    only if each level's declared maximum really bounds it -- and if one is
    too small, a weak criterion quietly overturns a strong one and every
    weight-based test still passes. So this scores the alternatives against
    the rules written out separately, as a tuple compared left to right.
    """
    import random as _r

    for seed in range(250):
        r = _r.Random(seed + (9000 if power_pair else 0))
        n = r.choice([4, 6, 8])
        teams = [team(i, 3 * r.randint(0, 2)) for i in range(n)]
        history = {frozenset(p) for p in
                   (tuple(r.sample(range(n), 2)) for _ in range(r.randint(0, n)))}

        pairs, _ = pair_round(teams, history, _r.Random(seed), power_pair=power_pair)
        best = max(_criteria(teams, history, m, power_pair)
                   for m in _every_matching([t["id"] for t in teams]))

        assert _criteria(teams, history, pairs, power_pair) == best, \
            f"seed {seed}: {pairs}"


# ---- separating teams that are out on points alone -------------------------
#
# Rank distance and the boundary score can both be blind to the same exchange:
# with both upper teams ahead of both lower ones, swapping their opponents
# leaves the distance sum identical, and the boundary score adds its two
# endpoints separately so it cannot see the pairing either. The tie then falls
# to a criterion that does not care, and can land on the worse answer.

CODEX_TIE = [{"id": c, "points": p, "byes": 0} for c, p in
             zip("ABCDEFGH", [12, 12, 9, 9, 9, 9, 3, 3])]
CODEX_TIE_HISTORY = set(map(frozenset, ["CF", "DF", "EF", "EH", "FH"]))


def test_a_tie_on_distance_is_broken_towards_separating_the_eliminated():
    """G and H top out at 6 with six teams already on 9 or more, so for a top
    four they are out on arithmetic that cannot change -- no tiebreak, no
    judgement. Two rematch-free matchings tie on rank distance; one hands two
    live teams an opponent with nothing to play for and the other hands them
    none. The engine used to take the first."""
    pairs, _ = pair_round(CODEX_TIE, CODEX_TIE_HISTORY, rng(0),
                          power_pair=True, cut_to=4)

    out = {"G", "H"}
    assert sum(1 for a, b in pairs if (a in out) != (b in out)) == 0, pairs
    assert not [p for p in pairs if frozenset(p) in CODEX_TIE_HISTORY], pairs


def test_separation_never_outranks_keeping_opponents_close():
    """A tiebreak, deliberately. Rank proximity is the objective; this only
    chooses among matchings that are already equally good on it, so it can
    never drag two distant teams together to tidy the groups."""
    plain, _ = pair_round(CODEX_TIE, CODEX_TIE_HISTORY, rng(0), power_pair=True)
    sorted_, _ = pair_round(CODEX_TIE, CODEX_TIE_HISTORY, rng(0),
                            power_pair=True, cut_to=4)
    dist = lambda m: sum(abs("ABCDEFGH".index(a) - "ABCDEFGH".index(b)) for a, b in m)

    assert dist(sorted_) == dist(plain), (plain, sorted_)


def test_a_team_out_on_points_is_named_only_when_the_arithmetic_is_certain():
    from draft_organization.swiss import _certainly_out

    teams = [{"id": i, "points": p, "byes": 0} for i, p in
             enumerate([12, 12, 9, 6], start=1)]

    # Top two. Team 4 tops out at 9; both 12-point teams are already beyond it
    # whatever they do, so it is out. Team 3 can reach 12 and tie them, and a
    # tie is not a certainty, so it is not named.
    assert _certainly_out(teams, None, cut_to=2, points_for_win=3) == {4}
    assert _certainly_out(teams, None, cut_to=None, points_for_win=3) == set()


def test_a_bye_already_awarded_counts_towards_the_seats_it_fills():
    """A team holding the bye has its win already; treating it as though it
    might lose understates how full the cut is."""
    from draft_organization.swiss import _certainly_out

    teams = [{"id": i, "points": p, "byes": 0} for i, p in
             enumerate([9, 9, 6], start=1)]

    assert _certainly_out(teams, None, cut_to=2, points_for_win=3) == set()
    assert _certainly_out(teams, 1, cut_to=2, points_for_win=3) == set()
    # With BOTH nine-point teams assured of twelve, the six-point team's best
    # of nine cannot reach a top two -- but only one bye exists, so this is the
    # two-seat case where the single bye is what tips it.
    teams2 = [{"id": i, "points": p, "byes": 0} for i, p in
              enumerate([9, 12, 6], start=1)]
    assert _certainly_out(teams2, 1, cut_to=2, points_for_win=3) == {3}


# ---- guards that nothing else was pinning ----------------------------------

def test_a_criterion_outside_its_bound_is_refused():
    """The hierarchy is exact only while every value stays inside the bound
    declared for it; one that escapes carries into the level above and
    reorders the criteria. Nothing else can catch that -- the weights are
    what every other check compares against -- so `_pack` refuses it."""
    from draft_organization.swiss import _pack

    assert _pack([(1, 1)], 4) > 0
    for bad in (-1, 2):
        with pytest.raises(ValueError):
            _pack([(bad, 1)], 4)


def test_the_final_round_does_not_roll_dice():
    """An ordinary round is deliberately varied; the last one must not be.
    Pinned on a field with rematches forcing several equally adjacent
    matchings, so a stray random term would actually show -- an unconstrained
    field has one obvious answer and would hide it.
    """
    seen = {tuple(sorted(map(tuple, pair_round(
        CODEX_TIE, CODEX_TIE_HISTORY, rng(s), power_pair=True, cut_to=4)[0])))
        for s in range(40)}

    assert len(seen) == 1, f"the final round varied across seeds: {seen}"


def test_the_matching_library_is_imported_where_it_is_used():
    """Deliberate, and invisible to every other test because the suite always
    has networkx installed. The bot is started with no install step, so an
    import at module scope takes the WHOLE bot down over a dependency only
    tournaments need."""
    import inspect
    import draft_organization.swiss as mod

    assert not hasattr(mod, "networkx"), "networkx must not be imported at module scope"
    assert "import networkx" in inspect.getsource(mod.pair_round)


def test_the_win_award_is_read_from_the_caller_not_assumed():
    """`_certainly_out` compares a team's ceiling against its rivals' floors,
    and the ceiling is one win away. Hardcoding three points happens to be
    right for this league and silently wrong for any other -- a tournament
    awarding something else would have teams called out on arithmetic that
    does not apply to it."""
    from draft_organization.swiss import _certainly_out

    teams = [{"id": i, "points": p, "byes": 0} for i, p in
             enumerate([10, 10, 4], start=1)]

    # Ceiling 4+5=9, under two floors of 10: out. At a three-point award the
    # ceiling is only 7 -- still out -- so the field has to separate the two
    # awards somewhere they disagree.
    assert _certainly_out(teams, None, cut_to=2, points_for_win=5) == {3}

    bigger = [{"id": i, "points": p, "byes": 0} for i, p in
              enumerate([10, 10, 6], start=1)]
    # Ceiling 6+5=11 clears both floors -> not out. With three it is 9 -> out.
    assert _certainly_out(bigger, None, cut_to=2, points_for_win=5) == set()
    assert _certainly_out(bigger, None, cut_to=2, points_for_win=3) == {3}
