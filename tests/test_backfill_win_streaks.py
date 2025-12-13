"""
Unit tests for win streak backfill functionality.

Tests validate that the backfill script correctly reconstructs
streak history from existing match results.
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
from models.match import MatchResult
from models.draft_session import DraftSession
from models.win_streak_history import WinStreakHistory

# Import backfill logic
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))
from scripts.backfill_win_streaks import backfill_player_streaks


# ============================================================================
# FIXTURES
# ============================================================================

@pytest_asyncio.fixture
async def test_db():
    """Create a temporary test database."""
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


async def create_test_match_history(session, player_id, guild_id, match_pattern):
    """
    Create test match history for a player.

    match_pattern: List of 'W' (win), 'L' (loss), 'D' (draw)
    Example: ['W', 'W', 'L', 'W', 'W', 'W', 'D', 'L']
    """
    # Create player
    player = PlayerStats(
        player_id=player_id,
        guild_id=guild_id,
        display_name=f"Player{player_id}"
    )
    opponent = PlayerStats(
        player_id="999",
        guild_id=guild_id,
        display_name="Opponent"
    )
    session.add_all([player, opponent])
    await session.commit()

    # Create draft sessions and matches
    base_time = datetime.now() - timedelta(days=100)  # Start 100 days ago

    for i, result in enumerate(match_pattern):
        # Create draft session for this match
        draft = DraftSession(
            session_id=f"{guild_id}-draft{i}",
            guild_id=guild_id,
            session_type="random",
            teams_start_time=base_time + timedelta(days=i),
            draft_start_time=base_time + timedelta(days=i)
        )
        session.add(draft)

        # Determine winner based on pattern
        if result == 'W':
            winner_id = player_id
        elif result == 'L':
            winner_id = "999"
        else:  # Draw
            winner_id = None

        # Create match result
        match = MatchResult(
            session_id=f"{guild_id}-draft{i}",
            match_number=1,
            player1_id=player_id,
            player2_id="999",
            winner_id=winner_id,
            guild_id=guild_id
        )
        session.add(match)

    await session.commit()


# ============================================================================
# BACKFILL TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_backfill_simple_streak(test_db):
    """Backfill should correctly identify a simple win streak."""
    async with test_db() as session:
        # Arrange - Player wins 3, loses, wins 2
        await create_test_match_history(
            session, "111", "789",
            ['W', 'W', 'W', 'L', 'W', 'W']
        )

        # Act
        longest = await backfill_player_streaks("111", "789", session)

        # Assert - Should find two completed streaks
        history_stmt = select(WinStreakHistory).where(
            WinStreakHistory.player_id == "111"
        ).order_by(WinStreakHistory.streak_length)
        history_result = await session.execute(history_stmt)
        streaks = history_result.scalars().all()

        assert len(streaks) == 1, "Should have 1 completed streak (3-win)"
        assert streaks[0].streak_length == 3

        # Check PlayerStats
        player_stmt = select(PlayerStats).where(PlayerStats.player_id == "111")
        player_result = await session.execute(player_stmt)
        player = player_result.scalar_one()

        assert player.current_win_streak == 2, "Last 2 matches were wins"
        assert player.longest_win_streak == 3, "Best streak was 3"
        assert longest == 3


@pytest.mark.asyncio
async def test_backfill_with_draws(test_db):
    """Draws should not affect streak calculation."""
    async with test_db() as session:
        # Arrange - Win 3, Draw, Win 2, Loss
        await create_test_match_history(
            session, "111", "789",
            ['W', 'W', 'W', 'D', 'W', 'W', 'L']
        )

        # Act
        await backfill_player_streaks("111", "789", session)

        # Assert - Draws don't break streaks, should be one 5-win streak
        history_stmt = select(WinStreakHistory).where(WinStreakHistory.player_id == "111")
        history_result = await session.execute(history_stmt)
        streaks = history_result.scalars().all()

        assert len(streaks) == 1
        assert streaks[0].streak_length == 5, "3 wins + draw (ignored) + 2 wins = 5-win streak"


@pytest.mark.asyncio
async def test_backfill_multiple_streaks(test_db):
    """Should record all completed streaks."""
    async with test_db() as session:
        # Arrange - Multiple streaks of different lengths
        await create_test_match_history(
            session, "111", "789",
            ['W', 'W', 'L', 'W', 'W', 'W', 'W', 'L', 'W', 'L']
        )

        # Act
        await backfill_player_streaks("111", "789", session)

        # Assert - Should have 3 completed streaks: 2, 4, 1
        history_stmt = select(WinStreakHistory).where(
            WinStreakHistory.player_id == "111"
        ).order_by(WinStreakHistory.streak_length)
        history_result = await session.execute(history_stmt)
        streaks = history_result.scalars().all()

        assert len(streaks) == 3
        assert streaks[0].streak_length == 1
        assert streaks[1].streak_length == 2
        assert streaks[2].streak_length == 4

        # Check longest
        player_stmt = select(PlayerStats).where(PlayerStats.player_id == "111")
        player_result = await session.execute(player_stmt)
        player = player_result.scalar_one()

        assert player.longest_win_streak == 4


@pytest.mark.asyncio
async def test_backfill_active_streak_not_in_history(test_db):
    """Active streaks should not be in history table."""
    async with test_db() as session:
        # Arrange - Ends with active 3-win streak
        await create_test_match_history(
            session, "111", "789",
            ['W', 'W', 'L', 'W', 'W', 'W']
        )

        # Act
        await backfill_player_streaks("111", "789", session)

        # Assert - Only 1 completed streak in history
        history_stmt = select(WinStreakHistory).where(WinStreakHistory.player_id == "111")
        history_result = await session.execute(history_stmt)
        streaks = history_result.scalars().all()

        assert len(streaks) == 1, "Only completed streak (2-win) should be in history"
        assert streaks[0].streak_length == 2

        # Active streak should be in PlayerStats
        player_stmt = select(PlayerStats).where(PlayerStats.player_id == "111")
        player_result = await session.execute(player_stmt)
        player = player_result.scalar_one()

        assert player.current_win_streak == 3, "Last 3 matches were wins (active)"
        assert player.current_win_streak_started_at is not None


@pytest.mark.asyncio
async def test_backfill_all_losses_no_streaks(test_db):
    """Player with no wins should have no streaks."""
    async with test_db() as session:
        # Arrange - All losses
        await create_test_match_history(
            session, "111", "789",
            ['L', 'L', 'L', 'L']
        )

        # Act
        await backfill_player_streaks("111", "789", session)

        # Assert - No streaks
        history_stmt = select(WinStreakHistory).where(WinStreakHistory.player_id == "111")
        history_result = await session.execute(history_stmt)
        streaks = history_result.scalars().all()

        assert len(streaks) == 0

        player_stmt = select(PlayerStats).where(PlayerStats.player_id == "111")
        player_result = await session.execute(player_stmt)
        player = player_result.scalar_one()

        assert player.current_win_streak == 0
        assert player.longest_win_streak == 0


@pytest.mark.asyncio
async def test_backfill_streak_timestamps(test_db):
    """Backfill should set correct started_at and ended_at timestamps."""
    async with test_db() as session:
        # Arrange - Win 3, Loss
        await create_test_match_history(
            session, "111", "789",
            ['W', 'W', 'W', 'L']
        )

        # Act
        await backfill_player_streaks("111", "789", session)

        # Assert - Check timestamps
        history_stmt = select(WinStreakHistory).where(WinStreakHistory.player_id == "111")
        history_result = await session.execute(history_stmt)
        streak = history_result.scalar_one()

        # Started_at should be from first match (100 days ago)
        # Ended_at should be from loss match (97 days ago)
        assert streak.started_at is not None
        assert streak.ended_at is not None
        assert streak.ended_at > streak.started_at, "Streak should end after it starts"


@pytest.mark.asyncio
async def test_backfill_idempotent(test_db):
    """Running backfill twice should produce same result."""
    async with test_db() as session:
        # Arrange
        await create_test_match_history(
            session, "111", "789",
            ['W', 'W', 'W', 'L', 'W', 'W']
        )

        # Act - Run backfill twice
        await backfill_player_streaks("111", "789", session)
        first_count_stmt = select(WinStreakHistory).where(WinStreakHistory.player_id == "111")
        first_result = await session.execute(first_count_stmt)
        first_streaks = first_result.scalars().all()

        await backfill_player_streaks("111", "789", session)
        second_count_stmt = select(WinStreakHistory).where(WinStreakHistory.player_id == "111")
        second_result = await session.execute(second_count_stmt)
        second_streaks = second_result.scalars().all()

        # Assert - Same result both times
        assert len(first_streaks) == len(second_streaks)
        assert first_streaks[0].streak_length == second_streaks[0].streak_length
