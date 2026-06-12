"""Pure team-based Swiss pairing functions.

Adapted from the player-based algorithm in draft_organization/tournament.py,
generalized to any team count with global backtracking (so down-pairing across
points groups works) and an injectable RNG for deterministic tests.

Teams are plain dicts: {"id": <participant id>, "points": int, "byes": int}.
``previous_matchups`` is a set of frozenset({id_a, id_b}).
"""


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
