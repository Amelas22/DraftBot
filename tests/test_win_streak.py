"""
Unit tests for win streak tracking functionality.

Tests cover:
- Streak initialization on first win
- Consecutive wins incrementing streaks
- Loss saving to history and resetting current streak
- Draw handling (should not affect streaks)
- Breaking personal records
- Multiple streaks being recorded
"""

import pytest
import pytest_asyncio
import tempfile
import os
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database.models_base import Base
from models.player import PlayerStats
from models.win_streak_history import WinStreakHistory


# ============================================================================
# FIXTURES
# ============================================================================

@pytest_asyncio.fixture
async def test_db():
    """Create a temporary test database and return a test session factory."""
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{temp_db.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    test_session_factory = sessionmaker(
        engine,
        expire_on_commit=False,
        class_=AsyncSession
    )

    yield test_session_factory

    await engine.dispose()
    os.unlink(temp_db.name)


async def apply_match_result(winner, loser, guild_id, session, is_draw=False):
    """
    Apply streak logic for a match result.
    This simulates the logic from utils.py update_player_stats_and_elo.
    """
    if is_draw:
        # Draws don't affect streaks
        return

    # Handle LOSER's streak
    if loser.current_win_streak > 0:
        # Record completed streak to history
        streak_history = WinStreakHistory(
            player_id=loser.player_id,
            guild_id=guild_id,
            streak_length=loser.current_win_streak,
            started_at=loser.current_win_streak_started_at,
            ended_at=datetime.now()
        )
        session.add(streak_history)

        # Update lifetime longest if this was their best
        if loser.current_win_streak > loser.longest_win_streak:
            loser.longest_win_streak = loser.current_win_streak

    # Reset loser's current streak
    loser.current_win_streak = 0
    loser.current_win_streak_started_at = None

    # Handle WINNER's streak
    if winner.current_win_streak == 0:
        # Starting a new streak
        winner.current_win_streak_started_at = datetime.now()

    # Increment winner's streak
    winner.current_win_streak += 1

    # Update lifetime longest if current exceeds it
    if winner.current_win_streak > winner.longest_win_streak:
        winner.longest_win_streak = winner.current_win_streak

    await session.commit()


# ============================================================================
# WIN STREAK TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_first_win_initializes_streak(test_db):
    """First match win should set current=1, longest=1."""
    async with test_db() as session:
        # Arrange
        winner = PlayerStats(player_id="111", guild_id="789", display_name="Winner")
        loser = PlayerStats(player_id="222", guild_id="789", display_name="Loser")
        session.add_all([winner, loser])
        await session.commit()

        # Act
        await apply_match_result(winner, loser, "789", session)

        # Assert
        stmt = select(PlayerStats).where(PlayerStats.player_id == "111")
        result = await session.execute(stmt)
        winner_updated = result.scalar_one()

        assert winner_updated.current_win_streak == 1
        assert winner_updated.longest_win_streak == 1
        assert winner_updated.current_win_streak_started_at is not None


@pytest.mark.asyncio
async def test_consecutive_wins_increment_streak(test_db):
    """Multiple consecutive wins should increment both current and longest."""
    async with test_db() as session:
        # Arrange
        player1 = PlayerStats(player_id="111", guild_id="789", display_name="P1")
        player2 = PlayerStats(player_id="222", guild_id="789", display_name="P2")
        player3 = PlayerStats(player_id="333", guild_id="789", display_name="P3")
        session.add_all([player1, player2, player3])
        await session.commit()

        # Act - Player1 wins 3 consecutive matches
        await apply_match_result(player1, player2, "789", session)
        await apply_match_result(player1, player3, "789", session)
        await apply_match_result(player1, player2, "789", session)

        # Assert
        stmt = select(PlayerStats).where(PlayerStats.player_id == "111")
        result = await session.execute(stmt)
        player1_updated = result.scalar_one()

        assert player1_updated.current_win_streak == 3
        assert player1_updated.longest_win_streak == 3


@pytest.mark.asyncio
async def test_loss_saves_to_history_and_resets(test_db):
    """Loss should save streak to history and reset current to 0."""
    async with test_db() as session:
        # Arrange - Player1 builds a 3-win streak
        player1 = PlayerStats(player_id="111", guild_id="789", display_name="P1")
        player2 = PlayerStats(player_id="222", guild_id="789", display_name="P2")
        session.add_all([player1, player2])
        await session.commit()

        # Build 3-win streak
        await apply_match_result(player1, player2, "789", session)
        await apply_match_result(player1, player2, "789", session)
        await apply_match_result(player1, player2, "789", session)

        # Act - Player1 loses
        await apply_match_result(player2, player1, "789", session)

        # Assert - Player1's streak should be reset
        stmt = select(PlayerStats).where(PlayerStats.player_id == "111")
        result = await session.execute(stmt)
        player1_updated = result.scalar_one()

        assert player1_updated.current_win_streak == 0
        assert player1_updated.longest_win_streak == 3
        assert player1_updated.current_win_streak_started_at is None

        # Assert - Streak saved to history
        history_stmt = select(WinStreakHistory).where(WinStreakHistory.player_id == "111")
        history_result = await session.execute(history_stmt)
        streaks = history_result.scalars().all()

        assert len(streaks) == 1
        assert streaks[0].streak_length == 3
        assert streaks[0].ended_at is not None


@pytest.mark.asyncio
async def test_draw_maintains_streak(test_db):
    """Draw should not affect current streak."""
    async with test_db() as session:
        # Arrange - Player1 has a 2-win streak
        player1 = PlayerStats(player_id="111", guild_id="789", display_name="P1")
        player2 = PlayerStats(player_id="222", guild_id="789", display_name="P2")
        session.add_all([player1, player2])
        await session.commit()

        # Build 2-win streak
        await apply_match_result(player1, player2, "789", session)
        await apply_match_result(player1, player2, "789", session)

        # Act - Draw
        await apply_match_result(player1, player2, "789", session, is_draw=True)

        # Assert - Streak unchanged
        stmt = select(PlayerStats).where(PlayerStats.player_id == "111")
        result = await session.execute(stmt)
        player1_updated = result.scalar_one()

        assert player1_updated.current_win_streak == 2
        assert player1_updated.longest_win_streak == 2


@pytest.mark.asyncio
async def test_breaking_personal_record(test_db):
    """New streak exceeding old record should update longest."""
    async with test_db() as session:
        # Arrange - Player has longest=5 from before, current=0
        player1 = PlayerStats(
            player_id="111",
            guild_id="789",
            display_name="P1",
            current_win_streak=0,
            longest_win_streak=5
        )
        player2 = PlayerStats(player_id="222", guild_id="789", display_name="P2")
        session.add_all([player1, player2])
        await session.commit()

        # Act - Player1 wins 6 consecutive matches (breaks record)
        for _ in range(6):
            await apply_match_result(player1, player2, "789", session)

        # Assert
        stmt = select(PlayerStats).where(PlayerStats.player_id == "111")
        result = await session.execute(stmt)
        player1_updated = result.scalar_one()

        assert player1_updated.current_win_streak == 6
        assert player1_updated.longest_win_streak == 6


@pytest.mark.asyncio
async def test_multiple_streaks_records_all(test_db):
    """Player with multiple streaks should have all completed streaks in history."""
    async with test_db() as session:
        # Arrange
        player1 = PlayerStats(player_id="111", guild_id="789", display_name="P1")
        player2 = PlayerStats(player_id="222", guild_id="789", display_name="P2")
        session.add_all([player1, player2])
        await session.commit()

        # Act - Pattern: Win 3, Lose, Win 5, Lose
        for _ in range(3):
            await apply_match_result(player1, player2, "789", session)
        await apply_match_result(player2, player1, "789", session)  # Loss

        for _ in range(5):
            await apply_match_result(player1, player2, "789", session)
        await apply_match_result(player2, player1, "789", session)  # Loss

        # Assert - 2 completed streaks in history
        history_stmt = select(WinStreakHistory).where(
            WinStreakHistory.player_id == "111"
        ).order_by(WinStreakHistory.streak_length)
        history_result = await session.execute(history_stmt)
        streaks = history_result.scalars().all()

        assert len(streaks) == 2
        assert streaks[0].streak_length == 3
        assert streaks[1].streak_length == 5

        # Assert - Current reset, longest is 5
        stmt = select(PlayerStats).where(PlayerStats.player_id == "111")
        result = await session.execute(stmt)
        player1_updated = result.scalar_one()

        assert player1_updated.current_win_streak == 0
        assert player1_updated.longest_win_streak == 5


@pytest.mark.asyncio
async def test_loser_streak_also_resets(test_db):
    """Loser should also have their streak reset and saved."""
    async with test_db() as session:
        # Arrange - Both players have 2-win streaks
        player1 = PlayerStats(player_id="111", guild_id="789", display_name="P1")
        player2 = PlayerStats(player_id="222", guild_id="789", display_name="P2")
        player3 = PlayerStats(player_id="333", guild_id="789", display_name="P3")
        session.add_all([player1, player2, player3])
        await session.commit()

        # Build streaks for both
        await apply_match_result(player1, player3, "789", session)
        await apply_match_result(player1, player3, "789", session)
        await apply_match_result(player2, player3, "789", session)
        await apply_match_result(player2, player3, "789", session)

        # Act - Player1 beats Player2
        await apply_match_result(player1, player2, "789", session)

        # Assert - Player1 wins (streak = 3), Player2 loses (streak reset)
        stmt1 = select(PlayerStats).where(PlayerStats.player_id == "111")
        result1 = await session.execute(stmt1)
        player1_updated = result1.scalar_one()

        stmt2 = select(PlayerStats).where(PlayerStats.player_id == "222")
        result2 = await session.execute(stmt2)
        player2_updated = result2.scalar_one()

        assert player1_updated.current_win_streak == 3
        assert player2_updated.current_win_streak == 0

        # Player2's 2-win streak should be in history
        history_stmt = select(WinStreakHistory).where(WinStreakHistory.player_id == "222")
        history_result = await session.execute(history_stmt)
        player2_streaks = history_result.scalars().all()

        assert len(player2_streaks) == 1
        assert player2_streaks[0].streak_length == 2


# ============================================================================
# TIMEFRAME FILTERING TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_active_streaks_appear_on_all_timeframes(test_db):
    """Active streaks should appear on all timeframes, even if started >30/90d ago."""
    async with test_db() as session:
        # Arrange - Player has a 50-win streak that started 60 days ago (still active)
        old_start_date = datetime.now() - timedelta(days=60)

        player1 = PlayerStats(
            player_id="111",
            guild_id="789",
            display_name="LongStreaker",
            games_won=50,
            games_lost=10,
            current_win_streak=50,
            current_win_streak_started_at=old_start_date,
            longest_win_streak=50
        )
        session.add(player1)
        await session.commit()

        # Act & Assert - Check this appears on 30d timeframe
        from services.leaderboard_service import get_win_streak_leaderboard_data

        # 30d should include this active streak (even though started 60d ago)
        data_30d = await get_win_streak_leaderboard_data("789", "30d", 20, session)
        assert len(data_30d) == 1, "Active streak should appear on 30d leaderboard"
        assert data_30d[0]["longest_win_streak"] == 50
        assert data_30d[0]["is_active"] is True

        # Lifetime should also include it
        data_lifetime = await get_win_streak_leaderboard_data("789", "lifetime", 20, session)
        assert len(data_lifetime) == 1
        assert data_lifetime[0]["longest_win_streak"] == 50


@pytest.mark.asyncio
async def test_completed_streaks_filter_by_end_date(test_db):
    """Completed streaks should filter by when they ended, not when they started."""
    async with test_db() as session:
        # Arrange - Player had a 15-win streak that ended 20 days ago
        recent_end = datetime.now() - timedelta(days=20)
        old_start = datetime.now() - timedelta(days=50)  # Started 50 days ago

        player1 = PlayerStats(
            player_id="111",
            guild_id="789",
            display_name="RecentBreak",
            games_won=50,
            games_lost=20,
            current_win_streak=0,
            longest_win_streak=15
        )

        # Create completed streak in history
        streak = WinStreakHistory(
            player_id="111",
            guild_id="789",
            streak_length=15,
            started_at=old_start,  # Started 50 days ago
            ended_at=recent_end    # Ended 20 days ago
        )
        session.add_all([player1, streak])
        await session.commit()

        # Act & Assert - Should appear on 30d (ended within window)
        from services.leaderboard_service import get_win_streak_leaderboard_data

        data_30d = await get_win_streak_leaderboard_data("789", "30d", 20, session)
        assert len(data_30d) == 1, "Streak that ended 20d ago should appear on 30d leaderboard"
        assert data_30d[0]["longest_win_streak"] == 15
        assert data_30d[0]["is_active"] is False


@pytest.mark.asyncio
async def test_old_completed_streaks_not_in_recent_timeframes(test_db):
    """Completed streaks that ended >30/90 days ago should not appear on those timeframes."""
    async with test_db() as session:
        # Arrange - Player had a 20-win streak that ended 100 days ago
        old_end = datetime.now() - timedelta(days=100)
        very_old_start = datetime.now() - timedelta(days=120)

        player1 = PlayerStats(
            player_id="111",
            guild_id="789",
            display_name="OldStreaker",
            games_won=80,
            games_lost=30,
            current_win_streak=0,
            longest_win_streak=20
        )

        streak = WinStreakHistory(
            player_id="111",
            guild_id="789",
            streak_length=20,
            started_at=very_old_start,
            ended_at=old_end
        )
        session.add_all([player1, streak])
        await session.commit()

        # Act & Assert
        from services.leaderboard_service import get_win_streak_leaderboard_data

        # Should NOT appear on 30d (ended 100d ago)
        data_30d = await get_win_streak_leaderboard_data("789", "30d", 20, session)
        assert len(data_30d) == 0, "Streak ended 100d ago should NOT appear on 30d"

        # Should NOT appear on 90d (ended 100d ago)
        data_90d = await get_win_streak_leaderboard_data("789", "90d", 20, session)
        assert len(data_90d) == 0, "Streak ended 100d ago should NOT appear on 90d"

        # SHOULD appear on lifetime
        data_lifetime = await get_win_streak_leaderboard_data("789", "lifetime", 20, session)
        assert len(data_lifetime) == 1, "All completed streaks should appear on lifetime"
        assert data_lifetime[0]["longest_win_streak"] == 20


@pytest.mark.asyncio
async def test_active_timeframe_only_shows_active(test_db):
    """Active timeframe should only show active streaks, not completed ones."""
    async with test_db() as session:
        # Arrange
        # Player1 has active 10-win streak
        player1 = PlayerStats(
            player_id="111",
            guild_id="789",
            display_name="Active",
            games_won=10,
            games_lost=0,
            current_win_streak=10,
            current_win_streak_started_at=datetime.now() - timedelta(days=5),
            longest_win_streak=10
        )

        # Player2 had a 15-win streak that ended yesterday (completed)
        player2 = PlayerStats(
            player_id="222",
            guild_id="789",
            display_name="Completed",
            games_won=20,
            games_lost=5,
            current_win_streak=0,
            longest_win_streak=15
        )

        streak2 = WinStreakHistory(
            player_id="222",
            guild_id="789",
            streak_length=15,
            started_at=datetime.now() - timedelta(days=10),
            ended_at=datetime.now() - timedelta(days=1)
        )

        session.add_all([player1, player2, streak2])
        await session.commit()

        # Act
        from services.leaderboard_service import get_win_streak_leaderboard_data
        data_active = await get_win_streak_leaderboard_data("789", "active", 20, session)

        # Assert - Only player1's active streak should appear
        assert len(data_active) == 1, "Active timeframe should only show active streaks"
        assert data_active[0]["player_id"] == "111"
        assert data_active[0]["longest_win_streak"] == 10
        assert data_active[0]["is_active"] is True


@pytest.mark.asyncio
async def test_deduplication_keeps_best_streak(test_db):
    """If player has both active and completed streak in timeframe, keep the better one."""
    async with test_db() as session:
        # Arrange - Player has:
        # 1. Completed 20-win streak that ended 10 days ago
        # 2. Current active 15-win streak
        player1 = PlayerStats(
            player_id="111",
            guild_id="789",
            display_name="Duplicated",
            games_won=50,
            games_lost=10,
            current_win_streak=15,
            current_win_streak_started_at=datetime.now() - timedelta(days=5),
            longest_win_streak=20
        )

        # Completed streak (the better one)
        streak1 = WinStreakHistory(
            player_id="111",
            guild_id="789",
            streak_length=20,
            started_at=datetime.now() - timedelta(days=25),
            ended_at=datetime.now() - timedelta(days=10)
        )

        session.add_all([player1, streak1])
        await session.commit()

        # Act
        from services.leaderboard_service import get_win_streak_leaderboard_data
        data_30d = await get_win_streak_leaderboard_data("789", "30d", 20, session)

        # Assert - Should only appear once, with the better (20-win) streak
        assert len(data_30d) == 1, "Player should appear only once"
        assert data_30d[0]["longest_win_streak"] == 20, "Should show the better completed streak"
        assert data_30d[0]["is_active"] is False


@pytest.mark.asyncio
async def test_minimum_streak_requirements(test_db):
    """Streaks below minimum for timeframe should not appear."""
    async with test_db() as session:
        # Arrange - Player has 7-win active streak
        player1 = PlayerStats(
            player_id="111",
            guild_id="789",
            display_name="MediumStreak",
            games_won=7,
            games_lost=0,
            current_win_streak=7,
            current_win_streak_started_at=datetime.now() - timedelta(days=2),
            longest_win_streak=7
        )
        session.add(player1)
        await session.commit()

        # Act
        from services.leaderboard_service import get_win_streak_leaderboard_data

        # 7-win streak meets active minimum (6+)
        data_active = await get_win_streak_leaderboard_data("789", "active", 20, session)
        assert len(data_active) == 1, "7-win streak should appear on active (min 6)"

        # 7-win streak meets 30d minimum (6+)
        data_30d = await get_win_streak_leaderboard_data("789", "30d", 20, session)
        assert len(data_30d) == 1, "7-win streak should appear on 30d (min 6)"

        # 7-win streak does NOT meet 90d minimum (8+)
        data_90d = await get_win_streak_leaderboard_data("789", "90d", 20, session)
        assert len(data_90d) == 0, "7-win streak should NOT appear on 90d (min 8)"

        # 7-win streak does NOT meet lifetime minimum (10+)
        data_lifetime = await get_win_streak_leaderboard_data("789", "lifetime", 20, session)
        assert len(data_lifetime) == 0, "7-win streak should NOT appear on lifetime (min 10)"
