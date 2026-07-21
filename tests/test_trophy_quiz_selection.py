import random
from services.trophy_quiz_service import select_two_decks


def _draft_data(dm_ids):
    users, carddata = {}, {}
    for i, dm in enumerate(dm_ids):
        cid = f"c{i}"
        users[dm] = {"seatNum": i, "cards": [cid], "isBot": False}
        carddata[cid] = {"name": f"Card{i}"}
    return {"users": users, "carddata": carddata}


def _matches(pairs):
    from types import SimpleNamespace
    return [SimpleNamespace(player1_id=a, player2_id=b, winner_id=w) for a, b, w in pairs]


# 6 drafters, 3 rounds; d0 goes 3-0 (extreme), d5 goes 0-3 (extreme), rest middle.
_WITH_EXTREME = [
    ("d0", "d1", "d0"), ("d2", "d3", "d2"), ("d4", "d5", "d4"),
    ("d0", "d2", "d0"), ("d1", "d4", "d1"), ("d3", "d5", "d3"),
    ("d0", "d3", "d0"), ("d1", "d5", "d1"), ("d2", "d4", "d2"),
]
# 4 drafters, 3 rounds, all middle (d0,d1 = 2-1; d2,d3 = 1-2) -> no extreme.
_NO_EXTREME = [
    ("d0", "d2", "d0"), ("d0", "d3", "d0"), ("d1", "d0", "d1"),
    ("d1", "d3", "d1"), ("d2", "d1", "d2"), ("d3", "d2", "d3"),
]


def _setup(n, rounds):
    sign_ups = {f"d{i}": f"n{i}" for i in range(n)}
    return sign_ups, _draft_data([f"dm{i}" for i in range(n)]), _matches(rounds)


def test_picks_one_from_each_bucket():
    sign_ups, draft_data, matches = _setup(6, _WITH_EXTREME)
    decks = select_two_decks(draft_data, sign_ups, matches, random.Random(0))
    assert decks is not None and len(decks) == 2
    wins = sorted(d["wins"] for d in decks)
    assert wins[0] <= 1 and wins[1] >= 2          # one worse, one better
    assert all(d["pool"] for d in decks)


def test_ineligible_when_no_extreme():
    sign_ups, draft_data, matches = _setup(4, _NO_EXTREME)
    assert select_two_decks(draft_data, sign_ups, matches, random.Random(0)) is None


def test_extreme_not_forced_into_pair():
    # Extreme/middle equally likely (~1/2), but not forced: some pairs are middle-only.
    sign_ups, draft_data, matches = _setup(6, _WITH_EXTREME)
    saw_no_extreme = False
    for seed in range(30):
        decks = select_two_decks(draft_data, sign_ups, matches, random.Random(seed))
        assert decks is not None
        if all(d["wins"] not in (0, 3) for d in decks):
            saw_no_extreme = True
    assert saw_no_extreme


def test_excludes_drafter_with_unreported_match():
    # d0 would be 3-0 but one match is unreported -> d0 dropped; d3 (0-3) keeps
    # the pod eligible. d0 must never appear in the shown decks.
    sign_ups = {f"d{i}": f"n{i}" for i in range(4)}
    draft_data = _draft_data([f"dm{i}" for i in range(4)])
    matches = _matches([
        ("d0", "d1", "d0"), ("d2", "d3", "d2"),
        ("d0", "d2", None),                       # d0's match UNREPORTED
        ("d1", "d3", "d1"),
        ("d0", "d3", "d0"), ("d1", "d2", "d1"),
    ])
    for seed in range(10):
        decks = select_two_decks(draft_data, sign_ups, matches, random.Random(seed))
        assert decks is not None
        assert all(d["drafter_id"] != "d0" for d in decks)
