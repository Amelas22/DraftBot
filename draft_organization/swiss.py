"""Pure team-based Swiss pairing functions.

Adapted from the player-based algorithm in draft_organization/tournament.py,
generalized to any team count with global backtracking (so down-pairing across
points groups works) and an injectable RNG for deterministic tests.

Teams are plain dicts: {"id": <participant id>, "points": int, "byes": int}.
``previous_matchups`` is a set of frozenset({id_a, id_b}).
"""


MWP_FLOOR = 1 / 3


def match_win_percentage(match_points, rounds_played, floor=MWP_FLOOR):
    """A participant's match-win percentage, floored (MTR convention).

    match_points are 3 per win / 1 per draw; the denominator is 3 per round
    played. Zero rounds returns the floor.
    """
    if rounds_played <= 0:
        return floor
    return max(floor, match_points / (3 * rounds_played))


def rank_standings(participants, matches):
    """Sort participants by points, then OMW%, then game diff, then name.

    OMW% is the average match-win percentage of each participant's *real*
    opponents (byes excluded). Participants with no real opponents get the
    floor. Pure: ``participants`` and ``matches`` are read-only.
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

    def omw(p):
        opp_ids = opponents[p.id]
        if not opp_ids:
            return MWP_FLOOR
        return sum(mwp(by_id[oid]) for oid in opp_ids) / len(opp_ids)

    return sorted(
        participants,
        key=lambda p: (-p.points, -omw(p), -(p.game_wins - p.game_losses), p.team_name),
    )


def assign_bye(teams, rng):
    """Pick the bye recipient: fewest byes first, then lowest points, then random."""
    fewest_byes = min(t["byes"] for t in teams)
    candidates = [t for t in teams if t["byes"] == fewest_byes]
    lowest_points = min(t["points"] for t in candidates)
    finalists = [t["id"] for t in candidates if t["points"] == lowest_points]
    return rng.choice(finalists)


def pair_round(teams, previous_matchups, rng):
    """Pair a round of Swiss. Returns (pairs, bye_id).

    pairs is a list of (id_a, id_b); bye_id is None for even team counts.
    Teams are ordered by points (shuffled within equal-points groups), then
    paired top-down with backtracking to avoid rematches. If no rematch-free
    configuration exists, rematches are allowed rather than failing the round.
    """
    teams = list(teams)
    bye_id = None
    if len(teams) % 2 == 1:
        bye_id = assign_bye(teams, rng)
        teams = [t for t in teams if t["id"] != bye_id]

    groups = {}
    for t in teams:
        groups.setdefault(t["points"], []).append(t["id"])
    ordered = []
    for points in sorted(groups, reverse=True):
        group = groups[points]
        rng.shuffle(group)
        ordered.extend(group)

    pairs = []
    if _pair_backtracking(ordered, previous_matchups, pairs):
        return pairs, bye_id

    # No rematch-free configuration exists: pair in standings order anyway.
    pairs = [(ordered[i], ordered[i + 1]) for i in range(0, len(ordered), 2)]
    return pairs, bye_id


def _pair_backtracking(ordered, previous_matchups, pairs):
    if not ordered:
        return True
    first = ordered[0]
    for i in range(1, len(ordered)):
        opponent = ordered[i]
        if frozenset((first, opponent)) in previous_matchups:
            continue
        pairs.append((first, opponent))
        if _pair_backtracking(ordered[1:i] + ordered[i + 1:], previous_matchups, pairs):
            return True
        pairs.pop()
    return False
