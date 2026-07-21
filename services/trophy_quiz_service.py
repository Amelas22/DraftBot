"""Pure logic for the Trophy Quiz: record labels, scoring, and (Task 2/3) record
computation + deck selection. No Discord objects here."""

from services.draft_log_store import render_pool, map_discord_to_draftmancer

DIRECTION_POINTS = 4
EXACT_POINTS = 3     # flat: same for any exactly-correct record (keeps max constant)
EXTREME_TARGET_RATE = 1 / 2   # extreme and middle equally likely (no-hedge point under flat scoring)
CHANGE_COST = 2  # points deducted (floored at 0) when a player pays to change their answer


def apply_change_cost(base_total: int, changed: bool) -> int:
    """Final quiz score after an optional pay-to-change-answer penalty, floored at 0."""
    return max(0, base_total - CHANGE_COST) if changed else base_total


def record_label(wins: int) -> str:
    """'3-0', '2-1', '1-2', '0-3' for a 3-round record."""
    return f"{wins}-{3 - wins}"


def is_extreme(wins: int) -> bool:
    """True for a 3-0 or 0-3 — used by selection biasing, not scoring."""
    return wins in (0, 3)


def score_submission(guessed_wins: list, actual_wins: list) -> dict:
    """Score a 2-deck submission. guessed/actual are [winsA, winsB].

    +DIRECTION_POINTS if the guessed better deck (more guessed wins) matches the
    actual better deck; a tie guess scores 0 for direction. Each exactly-correct
    record adds a flat +EXACT_POINTS, so the max is constant regardless of the
    records (a shared total can't reveal whether the quiz held a trophy).
    """
    if guessed_wins[0] == guessed_wins[1]:
        guessed_better = None
    else:
        guessed_better = 0 if guessed_wins[0] > guessed_wins[1] else 1
    actual_better = 0 if actual_wins[0] > actual_wins[1] else 1

    direction_correct = guessed_better == actual_better
    direction_points = DIRECTION_POINTS if direction_correct else 0
    exact_points = [
        EXACT_POINTS if g == a else 0
        for g, a in zip(guessed_wins, actual_wins)
    ]
    return {
        "direction_correct": direction_correct,
        "direction_points": direction_points,
        "exact_points": exact_points,
        "total": direction_points + sum(exact_points),
    }


def compute_records(match_results) -> dict:
    """Per-player {wins, matches, reported} from a draft's match results. matches
    counts appearances; wins counts winner_id; reported counts the player's
    matches that are decided (non-null winner_id)."""
    recs: dict = {}

    def _entry(pid):
        return recs.setdefault(pid, {"wins": 0, "matches": 0, "reported": 0})

    for m in match_results:
        decided = m.winner_id is not None
        for pid in (m.player1_id, m.player2_id):
            if pid is None:
                continue
            entry = _entry(pid)
            entry["matches"] += 1
            if decided:
                entry["reported"] += 1
        if decided:
            _entry(m.winner_id)["wins"] += 1
    return recs


def _pick_from_bucket(bucket, rng):
    """Pick one drafter from a bucket, choosing an extreme with probability
    EXTREME_TARGET_RATE when the bucket offers both an extreme and a middle."""
    extremes = [d for d in bucket if is_extreme(d["wins"])]
    middles = [d for d in bucket if not is_extreme(d["wins"])]
    if extremes and middles:
        group = extremes if rng.random() < EXTREME_TARGET_RATE else middles
    else:
        group = extremes or middles
    return rng.choice(group)


def select_two_decks(draft_data, sign_ups, match_results, rng):
    """Pick one better-bucket (wins>=2) and one worse-bucket (wins<=1) deck from
    a pod that has >=1 extreme (3-0/0-3), or None if ineligible.

    A qualifying drafter has a clean mapping, exactly 3 FULLY-REPORTED matches, and
    a non-empty pool. Selection is biased toward extremes (EXTREME_TARGET_RATE),
    not forced. The stored deck keeps its rendered `pool` text.
    """
    mapping = map_discord_to_draftmancer(draft_data, sign_ups)
    if not mapping:
        return None
    records = compute_records(match_results)

    qualifying = []
    for discord_id, dm_id in mapping.items():
        rec = records.get(discord_id)
        if not rec or rec["matches"] != 3 or rec["reported"] != 3:
            continue
        pool = render_pool(draft_data, dm_id)
        if not pool:
            continue
        qualifying.append({"drafter_id": discord_id, "wins": rec["wins"], "pool": pool})

    better = [q for q in qualifying if q["wins"] >= 2]
    worse = [q for q in qualifying if q["wins"] <= 1]
    has_extreme = any(is_extreme(q["wins"]) for q in qualifying)
    if not better or not worse or not has_extreme:
        return None

    pair = [_pick_from_bucket(better, rng), _pick_from_bucket(worse, rng)]
    rng.shuffle(pair)
    return pair
