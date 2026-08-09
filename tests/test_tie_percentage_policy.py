"""One tie policy everywhere: percentages count tied team drafts in the
denominator (stats_core's contract, same as /record's draws handling).

Fixtures are complete sessions (teams, victory message, reported matches)
so the test holds across both the session-metadata and ledger-fold
implementations of the leaderboard assembly.
"""
import pytest

from database.db_session import AsyncSessionLocal
from models.draft_session import DraftSession
from models.match import MatchResult


async def _team_session(session_id, winners_a, start):
    """A completed 1v1-team session: player '1' and '9' on opposing sides,
    two matches; winners_a of them won by side A ('1')."""
    from datetime import datetime
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(
            session_id=session_id, guild_id="g", session_type="staked",
            session_stage="completed", victory_message_id_results_channel="v",
            team_a=["1"], team_b=["9"],
            teams_start_time=datetime(2026, 1, start),
            draft_start_time=datetime(2026, 1, start), cube="C",
            sign_ups={"1": "One", "9": "Nine"}))
        for i in range(2):
            winner = "1" if i < winners_a else "9"
            loser = "9" if winner == "1" else "1"
            s.add(MatchResult(session_id=session_id, match_number=i + 1,
                              player1_id="1", player2_id="9", winner_id=winner))
        await s.commit()


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

    # Vault/Key partnerships share stats_core's tie-inclusive formula with
    # the 95% assertion above (one owner, no separate partnership wrapper);
    # the 1v1 fixture has no teammates, so this exercises the path only.
    await get_leaderboard_data("g", category="time_vault_and_key",
                               limit=5, timeframe="lifetime")
