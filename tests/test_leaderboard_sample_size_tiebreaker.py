"""Equal win percentages rank the larger sample first: a 6-0-0 record
sits above a 5-0-0 one instead of falling back to fold iteration order
(which is meaningless to a reader and made 100% clusters look shuffled).

Fixtures are complete sessions (teams, victory message, reported matches),
seeded inside the 14d window so its lower minimums (5 drafts / 3
partnership drafts) keep the seeding small. test_db and seed_session come
from tests/conftest.py.
"""
from datetime import datetime, timedelta

import pytest

from conftest import seed_session


async def _swept_2v2(session_id, side_a, side_b, start):
    """A completed 2v2-team session swept by side A: each side-A player
    beats their side-B counterpart."""
    matches = [(side_a[0], side_b[0], side_a[0], None),
               (side_a[1], side_b[1], side_a[1], None)]
    await seed_session(
        session_id=session_id, teams=(list(side_a), list(side_b)),
        victory="v", start=start,
        sign_ups={p: f"P{p}" for p in (*side_a, *side_b)},
        cube="C", matches=matches)


async def _seed_three_perfect_pairs():
    """Three all-winning partnerships with different sample sizes -- 6, 5,
    and 3 team drafts -- all against the same losing pair ('8', '9')."""
    now = datetime.now()
    for i in range(6):
        await _swept_2v2(f"big{i}", ("5", "6"), ("8", "9"),
                         now - timedelta(days=1, hours=i))
    for i in range(5):
        await _swept_2v2(f"mid{i}", ("1", "2"), ("8", "9"),
                         now - timedelta(days=2, hours=i))
    for i in range(3):
        await _swept_2v2(f"small{i}", ("3", "4"), ("8", "9"),
                         now - timedelta(days=3, hours=i))


@pytest.mark.asyncio
async def test_vault_key_ties_rank_larger_sample_first(test_db):
    from services.leaderboard_service import get_leaderboard_data
    await _seed_three_perfect_pairs()

    data = await get_leaderboard_data("g", category="time_vault_and_key",
                                      limit=10, timeframe="14d")
    assert all(p["win_percentage"] == 100 for p in data)
    pairs = [tuple(sorted((p["player_id"], p["teammate_id"]))) for p in data]
    # 6-0-0 > 5-0-0 > 3-0-0 despite identical percentages. ('8', '9')
    # is filtered by the 50% gate.
    assert pairs == [("5", "6"), ("1", "2"), ("3", "4")]


@pytest.mark.asyncio
async def test_draft_record_ties_rank_larger_sample_first(test_db):
    from services.leaderboard_service import get_leaderboard_data
    await _seed_three_perfect_pairs()

    data = await get_leaderboard_data("g", category="draft_record",
                                      limit=10, timeframe="14d")
    ids = [p["player_id"] for p in data]
    # '3'/'4' miss the 5-draft minimum and '8'/'9' the 50% gate, leaving
    # two 100% clusters: the 6-draft players above the 5-draft players.
    assert len(ids) == 4
    assert set(ids[:2]) == {"5", "6"}
    assert set(ids[2:]) == {"1", "2"}
