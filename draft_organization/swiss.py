"""Pure team-based Swiss pairing functions.

A round is paired as the maximum-weight perfect matching over `pairing_weights`,
which states the pairing rules as a strict hierarchy of criteria -- the way FIDE
specifies Swiss, and the way the engines that pair most chess tournaments solve
it. The RNG is injectable so tests are deterministic.

Teams are plain dicts: {"id": <participant id>, "points": int, "byes": int}.
``previous_matchups`` is a set of frozenset({id_a, id_b}).
"""
import random
from typing import Any




def round_robin_schedule(team_ids, rng=None):
    """Full single round-robin schedule via the circle method.

    Returns a list of rounds; each round is a list of (a, b) pairs. Every team
    plays every other exactly once. For an odd team count one team sits out each
    round (no bye *match* is emitted). An optional rng shuffles the seeding so
    the schedule isn't always in team order.
    """
    teams = list(team_ids)
    if rng is not None:
        rng.shuffle(teams)
    sit_out = object()  # sentinel occupying the odd slot; never emitted
    if len(teams) % 2 == 1:
        teams.append(sit_out)

    n = len(teams)
    arr = teams[:]
    rounds = []
    for _ in range(n - 1):
        pairs = []
        for i in range(n // 2):
            a, b = arr[i], arr[n - 1 - i]
            if a is not sit_out and b is not sit_out:
                pairs.append((a, b))
        rounds.append(pairs)
        # Fix arr[0], rotate the rest one position.
        arr = [arr[0]] + [arr[-1]] + arr[1:-1]
    return rounds


MWP_FLOOR = 1 / 3


def match_win_percentage(match_points, rounds_played, floor=MWP_FLOOR):
    """A participant's match-win percentage, floored (MTR convention).

    match_points are 3 per win / 1 per draw; the denominator is 3 per round
    played. Zero rounds returns the floor.
    """
    if rounds_played <= 0:
        return floor
    return max(floor, match_points / (3 * rounds_played))


def omw_percentages(participants, matches):
    """{participant id: OMW%} -- the average match-win percentage of each
    participant's *real* opponents (byes excluded).

    Participants with no real opponents get the floor. Pure: ``participants``
    and ``matches`` are read-only.

    Split out of rank_standings so a caller that has to *show* the tiebreak
    (the public league page) reads the same numbers the sort used, instead of
    reimplementing them and drifting.
    """
    by_id = {p.id: p for p in participants}
    opponents = {p.id: [] for p in participants}
    for m in matches:
        if m.is_bye or m.team_a_participant_id is None or m.team_b_participant_id is None:
            continue
        a, b = m.team_a_participant_id, m.team_b_participant_id
        if a in opponents and b in opponents:
            opponents[a].append(b)
            opponents[b].append(a)

    def mwp(p):
        rounds = p.match_wins + p.match_losses + p.match_draws
        return match_win_percentage(p.points, rounds)

    return {
        p.id: (sum(mwp(by_id[oid]) for oid in opponents[p.id]) / len(opponents[p.id])
               if opponents[p.id] else MWP_FLOOR)
        for p in participants
    }


def _ranking_key(participant: Any, omw: "dict[Any, float]") -> "tuple[Any, ...]":
    """What separates two teams, strongest signal first.

    Shared by the board and by pairing so the two cannot drift. They differ
    only in what happens once this is exhausted: the board appends the team
    name, so it holds still between refreshes, and pairing appends nothing and
    lets the caller's shuffle decide -- see `pairing_order` for why a name must
    never choose an opponent.
    """
    return (-participant.points,
            participant.match_wins + participant.match_losses + participant.match_draws,
            -omw[participant.id],
            -(participant.game_wins - participant.game_losses))


def rank_standings(participants, matches, omw=None):
    """Sort by points, then fewest rounds played, then OMW%, then game diff, then name.

    Rounds played comes before OMW% because standings update live: a team that
    has not played this round yet is compared against teams that have. Both
    hold the same points, but the one that spent fewer rounds getting them has
    a round in hand, and ranking it below a team that has already played that
    round reads as the board being wrong.

    Rounds played, not losses. The two agree only while no draw exists, and a
    draw is reachable -- `_apply_result` records one whenever a team match ends
    level. On losses, 0-0-3 (three rounds spent) outranks 1-1-0 (two rounds,
    one in hand) at equal points, inverting the very comparison this exists to
    fix. Ordering by match-win percentage instead would rank the top identically
    and wreck the bottom, where the MWP floor collapses 1-2, 1-3, 0-2, 0-3 and
    0-4 onto one value; used inside an equal-points group, rounds played never
    reaches the floor at all.

    ``omw`` may be a precomputed map from ``omw_percentages`` over the same
    arguments -- a caller that also displays the tiebreak passes the map it
    shows, so the board cannot rank on one set of numbers and print another.

    Pure: ``participants`` and ``matches`` are read-only.
    """
    if omw is None:
        omw = omw_percentages(participants, matches)
    return sorted(participants, key=lambda p: (*_ranking_key(p, omw), p.team_name))


def pairing_order(participants: "list[Any]", matches: "list[Any]",
                  rng: random.Random,
                  omw: "dict[Any, float] | None" = None) -> "list[Any]":
    """The field in the order a round should PAIR it, best first.

    The same keys `rank_standings` ranks on, with one deliberate difference:
    an exact tie is broken by the rng rather than by team name.

    That difference is the whole reason this exists separately. The board ends
    on team_name so it holds still between refreshes, which is right for
    something people read. Pairing must not: in round one nobody has played,
    every other key is level for everybody, and ranking on name would pair the
    first round alphabetically -- fixed before a card is drawn, and choosable
    by anyone willing to rename their team.

    Pure apart from consuming `rng`. ``participants`` and ``matches`` are
    read-only.
    """
    if omw is None:
        omw = omw_percentages(participants, matches)
    shuffled = list(participants)
    rng.shuffle(shuffled)
    return sorted(shuffled, key=lambda p: _ranking_key(p, omw))


def assign_bye(teams, rng):
    """Pick the bye recipient: fewest byes first, then lowest points, then random."""
    fewest_byes = min(t["byes"] for t in teams)
    candidates = [t for t in teams if t["byes"] == fewest_byes]
    lowest_points = min(t["points"] for t in candidates)
    finalists = [t["id"] for t in candidates if t["points"] == lowest_points]
    return rng.choice(finalists)


def _certainly_out(teams: "list[dict[str, Any]]", bye_id: Any, cut_to: "int | None",
                   points_for_win: int) -> "set[Any]":
    """Teams that cannot reach the top `cut_to`, on arithmetic that cannot move.

    A team's ceiling is winning its last game. A rival's floor is losing
    theirs -- except one already awarded a bye, whose win is banked. Count the
    rivals whose floor is strictly above this team's ceiling: once `cut_to` of
    them exist, the seats are gone however everything else falls.

    Points only. No tiebreak enters, which is the point: OMW has not settled
    when a round is paired -- earlier opponents play again, the last opponent
    joins the average -- so a verdict resting on it can be wrong, and a wrong
    verdict is worse than none when it decides who is grouped with whom. This
    one cannot be wrong. It is also far weaker, naming only the teams nobody
    would argue about, which is why it is used to break ties rather than to
    drive the pairing.
    """
    if not cut_to:
        return set()
    floor = {t["id"]: t["points"] + (points_for_win if t["id"] == bye_id else 0)
             for t in teams}
    out = set()
    for team in teams:
        ceiling = team["points"] + points_for_win
        gone = sum(1 for other in teams
                   if other["id"] != team["id"] and floor[other["id"]] > ceiling)
        if gone >= cut_to:
            out.add(team["id"])
    return out


def _pack(levels: "list[tuple[int, int]]", pairs_in_round: int) -> int:
    """One integer expressing a strict priority order over `levels`.

    `levels` are (value, max_value) from HIGHEST priority to lowest. Each is
    given a radix exceeding the largest total every lower level could reach
    across a whole round, so one unit of a higher criterion outweighs every
    lower criterion added together. Integers, not floats: Python integers are
    exact at any magnitude, and the separation between levels grows fast.
    """
    total, below = 0, 0
    for value, maxv in reversed(levels):
        # The separation between levels is only exact while every value stays
        # inside the bound declared for it. A value outside it can carry into
        # the level above and quietly overturn a stronger criterion, which no
        # weight-based test can see -- the weights are what it would be
        # checked against. Cheap enough to assert on every pair.
        if not 0 <= value <= maxv:
            raise ValueError(f"pairing criterion {value} outside its bound {maxv}")
        radix = below + 1
        total += value * radix
        below += pairs_in_round * maxv * radix
    return total


def pairing_weights(teams: "list[dict[str, Any]]",
                    previous_matchups: "set[frozenset[Any]]",
                    rng: random.Random,
                    power_pair: bool = False,
                    eliminated: "set[Any] | None" = None
                    ) -> "dict[frozenset[Any], int]":
    """How good every possible pairing would be: {frozenset(pair): int}.

    `teams` arrive in pairing order, best first. The criteria, strongest
    first:

      1. not a rematch
      2. both teams in the same points bracket -- i.e. fewest floaters
      3. if they must cross, cross the smallest gap
      4. and cross at the boundary: the lowest team of the upper bracket onto
         the highest team of the lower one
      5. final round only: pair rank-adjacent, so the teams still playing for
         a cut place meet each other rather than teams with nothing to play for
      6. ordinary round only: a random nudge, so the round keeps its variety

    A strict hierarchy of criteria is how FIDE specifies Swiss pairing, and
    the engines that pair most chess tournaments resolve it as a
    maximum-weight matching rather than by searching for something
    acceptable. `pair_round` does the same with these numbers. Criterion 5 is
    this league's own, not FIDE's.

    A rematch is priced rather than forbidden, so a field that cannot avoid
    one gets the matching with the FEWEST rematches instead of no matching.
    """
    ids = [t["id"] for t in teams]
    n = len(ids)
    eliminated = set() if eliminated is None else eliminated
    pairs_in_round = max(1, n // 2)
    rank = {t["id"]: i for i, t in enumerate(teams)}
    points = {t["id"]: t["points"] for t in teams}
    brackets = {p: i for i, p in
                enumerate(sorted({t["points"] for t in teams}, reverse=True))}

    members = {}
    for t in teams:                       # already in rank order
        members.setdefault(t["points"], []).append(t["id"])
    from_top, from_bottom = {}, {}
    for group in members.values():
        for i, m in enumerate(group):
            from_top[m] = i
            from_bottom[m] = len(group) - 1 - i

    max_gap = max(1, len(brackets) - 1)
    max_dist = 2 * max(1, n)              # from_bottom + from_top can reach 2n-2

    weights = {}
    for i in range(n):
        for j in range(i + 1, n):
            a, b = ids[i], ids[j]
            gap = abs(brackets[points[a]] - brackets[points[b]])
            if gap:
                upper, lower = ((a, b) if brackets[points[a]] < brackets[points[b]]
                                else (b, a))
                boundary = max_dist - (from_bottom[upper] + from_top[lower])
            else:
                boundary = max_dist       # nothing to prefer inside a bracket
            legal = (0 if frozenset((a, b)) in previous_matchups else 1, 1)
            if power_pair:
                # The final round wants teams with similar prospects of making
                # the cut to face each other, and rank -- points, then OMW --
                # orders them by exactly that. Pairing rank-adjacent therefore
                # tends to keep a team still playing for a seat away from one
                # with nothing left to play for.
                #
                # A PROXY for that, deliberately. Deciding who is actually
                # eliminated needs OMW that has not settled: it moves as the
                # round resolves, as earlier opponents play again and the last
                # opponent joins the average. So any verdict of "out" can be
                # wrong, and a wrong verdict used to GROUP teams is worse
                # than no verdict at all -- it seats a real contender with the
                # eliminated and calls it fair. Rank never claims anyone is
                # out, so it cannot make THAT mistake. It is not a fairness
                # guarantee either way: who a team is given still affects
                # whether it qualifies, whatever the algorithm calls anyone.
                #
                # The cost of the proxy, stated plainly: it measures closeness
                # at every rank boundary while the thing worth minimising is
                # one boundary, so it does not always produce the fewest
                # mixed pairings. Nor does it preserve the minimum number of
                # bracket crossings -- with 9, 6, 6, 3 it returns two
                # crossings where one exists.
                #
                # Above the boundary rule, which is actively wrong here:
                # pairing the bottom of one bracket onto the top of the next
                # aims at the widest gap in cut prospects inside a bracket.
                # It stays on only to break ties between equally adjacent
                # matchings.
                # Below rank proximity, above the boundary score. Both of
                # those can be blind to the same exchange -- swap two lower
                # teams between two upper ones and the distance sum is
                # unchanged, while the boundary score adds its endpoints
                # separately and cannot see the pairing at all. The tie then
                # went to whichever the solver reached, which is how a
                # matching that hands two live teams an opponent with nothing
                # to play for beat one that hands them none, at equal
                # distance. Only a tiebreak: it can never pull two distant
                # teams together to tidy the groups.
                apart = 1 if (a in eliminated) == (b in eliminated) else 0
                levels = [legal, (n - abs(rank[a] - rank[b]), n), (apart, 1),
                          (boundary, max_dist)]
            else:
                # An ordinary round wants opponents on the same score, floats
                # kept few and short, and a sensible landing when one is
                # unavoidable -- then variety among everything still equal.
                levels = [legal, (1 if gap == 0 else 0, 1), (max_gap - gap, max_gap),
                          (boundary, max_dist), (rng.randrange(n + 1), n)]
            weights[frozenset((a, b))] = _pack(levels, pairs_in_round)
    return weights


def pair_round(teams: "list[dict[str, Any]]",
               previous_matchups: "set[frozenset[Any]]",
               rng: random.Random,
               power_pair: bool = False,
               cut_to: "int | None" = None,
               points_for_win: int = 3
               ) -> "tuple[list[tuple[Any, Any]], Any]":
    """Pair a round of Swiss. Returns (pairs, bye_id).

    `teams` arrive in PAIRING ORDER, best first -- this does not rank them.
    Rank belongs to the caller, because the tiebreaks need the whole match
    history, and because pairing order and DISPLAY order are deliberately
    different: the board settles an exact tie by name so it holds still
    between refreshes, and pairing settles it at random, or round one would be
    paired alphabetically.

    The pairing is the maximum-weight perfect matching over
    `pairing_weights`, so it is the best pairing available under those
    criteria rather than the first acceptable one a search happens to reach.
    That distinction is not academic: a search that stops at the first
    rematch-free matching will abandon the boundary it was supposed to
    preserve whenever backtracking takes it elsewhere, even when a matching
    that keeps the boundary exists.
    """
    teams = list(teams)
    bye_id = None
    if len(teams) % 2 == 1:
        bye_id = assign_bye(teams, rng)
    # Worked out BEFORE the bye recipient leaves the field: their win is
    # already banked, so they fill a seat the teams still playing have to get
    # past, even though nobody is pairing them.
    eliminated = (_certainly_out(teams, bye_id, cut_to, points_for_win)
                  if power_pair else set())
    teams = [t for t in teams if t["id"] != bye_id]
    if not teams:
        return [], bye_id

    # Imported here, not at module scope. The service unit starts the bot with
    # `pipenv run` and no install step, so a dependency missing on the box takes
    # down whatever imports it -- and at module scope that is the whole bot, for
    # a feature only tournaments use. This way a missing networkx breaks
    # pairing, loudly, and leaves everything else running.
    import networkx

    weights = pairing_weights(teams, previous_matchups, rng, power_pair,
                              eliminated)
    graph = networkx.Graph()
    graph.add_nodes_from(t["id"] for t in teams)
    for pair, weight in weights.items():
        a, b = tuple(pair)
        graph.add_edge(a, b, weight=weight)

    order = {t["id"]: i for i, t in enumerate(teams)}
    matched = networkx.max_weight_matching(graph, maxcardinality=True)
    pairs = [tuple(sorted(edge, key=order.__getitem__)) for edge in matched]
    pairs.sort(key=lambda p: order[p[0]])
    return pairs, bye_id
