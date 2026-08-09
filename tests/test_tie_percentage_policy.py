"""One tie policy everywhere: percentages count tied team drafts in the
denominator (stats_core's contract, same as /record's draws handling).

Fixtures are complete sessions (teams, victory message, reported matches)
so the test holds across both the session-metadata and ledger-fold
implementations of the leaderboard assembly. test_db and seed_session come
from tests/conftest.py.
"""
from datetime import datetime

import pytest

from conftest import seed_session


async def _team_session(session_id, winners_a, start):
    """A completed 1v1-team session: player '1' and '9' on opposing sides,
    two matches; winners_a of them won by side A ('1')."""
    matches = [("1", "9", "1" if i < winners_a else "9", None)
               for i in range(2)]
    await seed_session(
        session_id=session_id, teams=(["1"], ["9"]), victory="v",
        start=datetime(2026, 1, start), sign_ups={"1": "One", "9": "Nine"},
        cube="C", matches=matches)


@pytest.mark.asyncio
async def test_leaderboard_percentages_count_ties(test_db):
    from services.leaderboard_service import get_leaderboard_data
    # 19 side-A wins + 1 tie: clears the lifetime minimums (20 drafts, >=50%)
    # and splits the policies: tie-inclusive = 19/20 = 95%, tie-exclusive = 100%.
    for i in range(19):
        await _team_session(f"w{i}", winners_a=2, start=(i % 27) + 1)
    await _team_session("t", winners_a=1, start=28)

    data = await get_leaderboard_data("g", category="draft_record",
                                      limit=5, timeframe="lifetime")
    p1 = next(p for p in data if p["player_id"] == "1")
    assert p1["team_drafts_won"] == 19 and p1["team_drafts_tied"] == 1
    assert round(p1["team_draft_win_percentage"], 1) == 95.0
