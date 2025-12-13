"""
Unit tests for leaderboard functionality.

Tests cover:
- Win streak leaderboard with multiple timeframes
- Minimum requirements enforcement
- Active vs completed streak filtering
- Sorting and ranking logic
- Display formatting
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
from services.leaderboard_service import get_win_streak_leaderboard_data
from services.leaderboard_formatter import create_leaderboard_embed, LEADERBOARD_CATEGORIES


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


# ============================================================================
# WIN STREAK LEADERBOARD TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_leaderboard_sorts_by_streak_length(test_db):
    """Leaderboard should sort by streak length descending."""
    async with test_db() as session:
        # Arrange - Create players with different streak lengths
        player1 = PlayerStats(
            player_id="111",
            guild_id="789",
            display_name="BigStreak",
            games_won=50,
            games_lost=20,
            current_win_streak=15,
            current_win_streak_started_at=datetime.now(),
            longest_win_streak=15
        )
        player2 = PlayerStats(
            player_id="222",
            guild_id="789",
            display_name="MediumStreak",
            games_won=30,
            games_lost=15,
            current_win_streak=10,
            current_win_streak_started_at=datetime.now(),
            longest_win_streak=10
        )
        player3 = PlayerStats(
            player_id="333",
            guild_id="789",
            display_name="SmallStreak",
            games_won=20,
            games_lost=10,
            current_win_streak=6,
            current_win_streak_started_at=datetime.now(),
            longest_win_streak=6
        )
        session.add_all([player1, player2, player3])
        await session.commit()

        # Act
        data = await get_win_streak_leaderboard_data("789", "active", 20, session)

        # Assert - Sorted by streak length descending
        assert len(data) == 3
        assert data[0]["player_id"] == "111"  # 15-win streak first
        assert data[1]["player_id"] == "222"  # 10-win streak second
        assert data[2]["player_id"] == "333"  # 6-win streak third


@pytest.mark.asyncio
async def test_leaderboard_uses_win_percentage_as_tiebreaker(test_db):
    """When streaks are equal, sort by win percentage."""
    async with test_db() as session:
        # Arrange - Two players with same streak but different win %
        player1 = PlayerStats(
            player_id="111",
            guild_id="789",
            display_name="HighWinRate",
            games_won=80,  # 80/(80+20) = 80%
            games_lost=20,
            current_win_streak=10,
            current_win_streak_started_at=datetime.now(),
            longest_win_streak=10
        )
        player2 = PlayerStats(
            player_id="222",
            guild_id="789",
            display_name="LowWinRate",
            games_won=50,  # 50/(50+50) = 50%
            games_lost=50,
            current_win_streak=10,
            current_win_streak_started_at=datetime.now(),
            longest_win_streak=10
        )
        session.add_all([player1, player2])
        await session.commit()

        # Act
        data = await get_win_streak_leaderboard_data("789", "active", 20, session)

        # Assert - Same streak, higher win % comes first
        assert len(data) == 2
        assert data[0]["player_id"] == "111"  # 80% win rate first
        assert data[1]["player_id"] == "222"  # 50% win rate second


@pytest.mark.asyncio
async def test_leaderboard_respects_limit(test_db):
    """Leaderboard should respect the limit parameter."""
    async with test_db() as session:
        # Arrange - Create 5 players with streaks
        for i in range(5):
            player = PlayerStats(
                player_id=f"{i}",
                guild_id="789",
                display_name=f"Player{i}",
                games_won=20,
                games_lost=10,
                current_win_streak=10 - i,  # Decreasing streaks
                current_win_streak_started_at=datetime.now(),
                longest_win_streak=10 - i
            )
            session.add(player)
        await session.commit()

        # Act - Request only top 3
        data = await get_win_streak_leaderboard_data("789", "active", limit=3, session=session)

        # Assert - Only 3 results
        assert len(data) == 3
        assert data[0]["longest_win_streak"] == 10
        assert data[1]["longest_win_streak"] == 9
        assert data[2]["longest_win_streak"] == 8


@pytest.mark.asyncio
async def test_empty_leaderboard_returns_empty_list(test_db):
    """When no players meet minimum, return empty list."""
    async with test_db() as session:
        # Arrange - Player with 2-win streak (below all minimums)
        player = PlayerStats(
            player_id="111",
            guild_id="789",
            display_name="TooShort",
            games_won=2,
            games_lost=0,
            current_win_streak=2,
            current_win_streak_started_at=datetime.now(),
            longest_win_streak=2
        )
        session.add(player)
        await session.commit()

        # Act - Request active (min 6)
        data = await get_win_streak_leaderboard_data("789", "active", 20, session)

        # Assert - Empty
        assert len(data) == 0


@pytest.mark.asyncio
async def test_leaderboard_config_exists_for_all_categories(test_db):
    """All categories should have proper configuration."""
    from cogs.leaderboard import LEADERBOARD_CATEGORIES as category_list
    from services.leaderboard_formatter import LEADERBOARD_CATEGORIES as category_configs

    # Assert - All categories in the list have config
    for category in category_list:
        assert category in category_configs, f"Category {category} should have config"
        config = category_configs[category]

        assert "title" in config
        assert "description_template" in config
        assert "color" in config
        assert "formatter" in config


@pytest.mark.asyncio
async def test_active_streak_indicator_in_output(test_db):
    """Active streaks should have is_active flag set correctly."""
    async with test_db() as session:
        # Arrange - One active, one completed (both meet lifetime min of 12)
        active_player = PlayerStats(
            player_id="111",
            guild_id="789",
            display_name="Active",
            games_won=13,
            games_lost=0,
            current_win_streak=13,
            current_win_streak_started_at=datetime.now(),
            longest_win_streak=13
        )
        completed_player = PlayerStats(
            player_id="222",
            guild_id="789",
            display_name="Completed",
            games_won=20,
            games_lost=5,
            current_win_streak=0,
            longest_win_streak=15
        )
        completed_streak = WinStreakHistory(
            player_id="222",
            guild_id="789",
            streak_length=15,
            started_at=datetime.now() - timedelta(days=20),
            ended_at=datetime.now() - timedelta(days=5)
        )
        session.add_all([active_player, completed_player, completed_streak])
        await session.commit()

        # Act
        data = await get_win_streak_leaderboard_data("789", "lifetime", 20, session)

        # Assert - Check is_active flags
        assert len(data) == 2

        active_entry = next(e for e in data if e["player_id"] == "111")
        completed_entry = next(e for e in data if e["player_id"] == "222")

        assert active_entry["is_active"] is True
        assert completed_entry["is_active"] is False


@pytest.mark.asyncio
async def test_mixed_active_and_completed_streaks(test_db):
    """Leaderboard should correctly combine active and completed streaks."""
    async with test_db() as session:
        # Arrange - 2 active, 2 completed (all different players)
        active1 = PlayerStats(
            player_id="111",
            guild_id="789",
            display_name="Active1",
            games_won=20,
            games_lost=5,
            current_win_streak=20,
            current_win_streak_started_at=datetime.now() - timedelta(days=10),
            longest_win_streak=20
        )
        active2 = PlayerStats(
            player_id="222",
            guild_id="789",
            display_name="Active2",
            games_won=15,
            games_lost=3,
            current_win_streak=15,
            current_win_streak_started_at=datetime.now() - timedelta(days=5),
            longest_win_streak=15
        )
        completed1 = PlayerStats(
            player_id="333",
            guild_id="789",
            display_name="Completed1",
            games_won=30,
            games_lost=10,
            current_win_streak=0,
            longest_win_streak=18
        )
        completed2 = PlayerStats(
            player_id="444",
            guild_id="789",
            display_name="Completed2",
            games_won=25,
            games_lost=8,
            current_win_streak=0,
            longest_win_streak=13
        )

        streak1 = WinStreakHistory(
            player_id="333",
            guild_id="789",
            streak_length=18,
            started_at=datetime.now() - timedelta(days=30),
            ended_at=datetime.now() - timedelta(days=15)
        )
        streak2 = WinStreakHistory(
            player_id="444",
            guild_id="789",
            streak_length=13,
            started_at=datetime.now() - timedelta(days=25),
            ended_at=datetime.now() - timedelta(days=10)
        )

        session.add_all([active1, active2, completed1, completed2, streak1, streak2])
        await session.commit()

        # Act - Lifetime should show all 4
        data = await get_win_streak_leaderboard_data("789", "lifetime", 20, session)

        # Assert
        assert len(data) == 4

        # Verify correct sorting: 20 (active) > 18 (completed) > 15 (active) > 13 (completed)
        assert data[0]["longest_win_streak"] == 20
        assert data[0]["is_active"] is True
        assert data[1]["longest_win_streak"] == 18
        assert data[1]["is_active"] is False
        assert data[2]["longest_win_streak"] == 15
        assert data[2]["is_active"] is True
        assert data[3]["longest_win_streak"] == 13
        assert data[3]["is_active"] is False
