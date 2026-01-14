"""
Unit tests for ring bearer functionality.

Tests cover:
- Ring bearer state creation and updates (database model)
- Role transfers via leaderboard priority
- Role transfers via match defeats
- Tie handling and streak extensions
- Sequential timing of operations
- Configuration and edge cases
- Discord role sync operations
"""

import pytest
import pytest_asyncio
import tempfile
import os
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
import discord

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select

from database.models_base import Base
from models.ring_bearer_state import RingBearerState
from services.ring_bearer_service import (
    update_ring_bearer_for_guild,
    check_match_defeat_transfer,
    transfer_ring_bearer,
    sync_ring_bearer_role
)


# ==================== FIXTURES ====================

@pytest_asyncio.fixture
async def test_db():
    """Create a temporary test database with all tables."""
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


@pytest.fixture
def mock_bot():
    """Create mock Discord bot."""
    bot = MagicMock()
    bot.get_guild = MagicMock()
    return bot


@pytest.fixture
def mock_guild(guild_id="123456"):
    """Create mock Discord guild."""
    guild = MagicMock(spec_set=discord.Guild)
    guild.id = int(guild_id)
    guild.get_member = MagicMock()
    guild.roles = []
    guild.text_channels = []
    return guild


@pytest.fixture
def mock_member():
    """Create mock Discord member factory."""
    def _create_member(member_id="999999", display_name="TestMember"):
        member = MagicMock(spec_set=discord.Member)
        member.id = int(member_id)
        member.display_name = display_name
        member.roles = []
        member.add_roles = AsyncMock()
        member.remove_roles = AsyncMock()
        return member
    return _create_member


@pytest.fixture
def mock_config():
    """Create mock config with ring bearer enabled and correct priority order."""
    return {
        "ring_bearer": {
            "enabled": True,
            "role_name": "ring bearer",
            "icon": "<:coveted_jewel:1460802711694999613>",
            "streak_categories": [
                "perfect_streak",        # FIRST - hardest (all 2-0 wins)
                "longest_win_streak",    # SECOND - match wins
                "draft_win_streak"       # THIRD - draft wins
            ]
        },
        "crown_roles": {
            "timeframe": "30d"
        },
        "channels": {
            "draft_results": "draft-results"
        }
    }


# ==================== HELPER FUNCTIONS ====================

async def create_ring_bearer_state(guild_id, bearer_id, acquired_via, session):
    """Helper to create ring bearer state in test DB."""
    state = RingBearerState(
        guild_id=guild_id,
        current_bearer_id=bearer_id,
        acquired_at=datetime.now(),
        acquired_via=acquired_via,
        previous_bearer_id=None
    )
    session.add(state)
    await session.commit()
    return state


def create_leaderboard_result(player_id, streak_length, win_rate=0.60, category="longest_win_streak"):
    """Helper to create mock leaderboard result."""
    result = {
        "player_id": player_id,
        "display_name": f"Player{player_id}",
        "games_won": 100,
        "games_lost": 50,
        "completed_matches": 150
    }

    if category == "longest_win_streak":
        result["longest_win_streak"] = streak_length
    elif category == "perfect_streak":
        result["longest_perfect_streak"] = streak_length
    elif category == "draft_win_streak":
        result["longest_draft_win_streak"] = streak_length

    return result


# ==================== CATEGORY 1: DATABASE MODEL TESTS ====================

@pytest.mark.asyncio
async def test_get_ring_bearer_no_bearer(test_db):
    """Guild with no ring bearer should return None."""
    async with test_db() as session:
        # Act
        state = await RingBearerState.get_ring_bearer("guild_123", session)

        # Assert
        assert state is None


@pytest.mark.asyncio
async def test_set_ring_bearer_creates_new_state(test_db):
    """First set_ring_bearer should create new database entry."""
    async with test_db() as session:
        # Act
        state = await RingBearerState.set_ring_bearer(
            guild_id="guild_123",
            bearer_id="user_456",
            acquired_via="perfect_streak",
            previous_bearer_id=None,
            session=session
        )

        # Assert
        assert state.current_bearer_id == "user_456"
        assert state.acquired_via == "perfect_streak"
        assert state.previous_bearer_id is None
        assert isinstance(state.acquired_at, datetime)

        # Verify persisted
        state2 = await RingBearerState.get_ring_bearer("guild_123", session)
        assert state2.current_bearer_id == "user_456"


@pytest.mark.asyncio
async def test_set_ring_bearer_updates_existing(test_db):
    """Subsequent set_ring_bearer should update same row, not create new."""
    async with test_db() as session:
        # Arrange - create initial state
        await RingBearerState.set_ring_bearer(
            guild_id="guild_123",
            bearer_id="user_111",
            acquired_via="perfect_streak",
            previous_bearer_id=None,
            session=session
        )

        # Act - update to new bearer
        state = await RingBearerState.set_ring_bearer(
            guild_id="guild_123",
            bearer_id="user_222",
            acquired_via="match_defeat",
            previous_bearer_id="user_111",
            session=session
        )

        # Assert
        assert state.current_bearer_id == "user_222"
        assert state.previous_bearer_id == "user_111"
        assert state.acquired_via == "match_defeat"

        # Verify only ONE row exists
        result = await session.execute(
            select(RingBearerState).where(RingBearerState.guild_id == "guild_123")
        )
        all_states = result.scalars().all()
        assert len(all_states) == 1


# ==================== CATEGORY 2: LEADERBOARD PRIORITY TESTS ====================

@pytest.mark.asyncio
async def test_refresh_first_category_wins(test_db, mock_bot, mock_config):
    """When multiple players are #1 on different leaderboards, perfect_streak holder gets ring."""
    guild_id = "123456"

    # Setup mock guild
    mock_guild_obj = MagicMock(spec_set=discord.Guild)
    mock_guild_obj.id = int(guild_id)
    mock_bot.get_guild.return_value = mock_guild_obj

    async with test_db() as session:
        with patch('services.ring_bearer_service.get_config', return_value=mock_config), \
             patch('services.ring_bearer_service.db_session', return_value=session), \
             patch('services.ring_bearer_service.get_leaderboard_leader') as mock_get_leader, \
             patch('services.ring_bearer_service.transfer_ring_bearer') as mock_transfer:

            # Mock leaderboard leaders
            mock_get_leader.side_effect = [
                create_leaderboard_result("player_A", 5, category="perfect_streak"),  # perfect_streak #1
                create_leaderboard_result("player_B", 10, category="longest_win_streak"),  # match #1
                create_leaderboard_result("player_C", 8, category="draft_win_streak")  # draft #1
            ]

            # Act
            await update_ring_bearer_for_guild(mock_bot, guild_id)

            # Assert - should transfer to player_A (first category)
            mock_transfer.assert_called_once()
            call_args = mock_transfer.call_args
            assert call_args[1]['new_bearer_id'] == "player_A"
            assert call_args[1]['acquired_via'] == "perfect_streak"


@pytest.mark.asyncio
async def test_refresh_second_category_wins_if_first_empty(test_db, mock_bot, mock_config):
    """If perfect_streak has no #1, longest_win_streak holder gets ring."""
    guild_id = "123456"

    mock_guild_obj = MagicMock(spec_set=discord.Guild)
    mock_guild_obj.id = int(guild_id)
    mock_bot.get_guild.return_value = mock_guild_obj

    async with test_db() as session:
        with patch('services.ring_bearer_service.get_config', return_value=mock_config), \
             patch('services.ring_bearer_service.db_session', return_value=session), \
             patch('services.ring_bearer_service.get_leaderboard_leader') as mock_get_leader, \
             patch('services.ring_bearer_service.transfer_ring_bearer') as mock_transfer:

            mock_get_leader.side_effect = [
                None,  # No perfect_streak leader
                create_leaderboard_result("player_B", 10, category="longest_win_streak"),
                create_leaderboard_result("player_C", 8, category="draft_win_streak")
            ]

            await update_ring_bearer_for_guild(mock_bot, guild_id)

            mock_transfer.assert_called_once()
            call_args = mock_transfer.call_args
            assert call_args[1]['new_bearer_id'] == "player_B"
            assert call_args[1]['acquired_via'] == "longest_win_streak"


@pytest.mark.asyncio
async def test_refresh_third_category_wins_if_first_two_empty(test_db, mock_bot, mock_config):
    """If first two categories empty, draft_win_streak holder gets ring."""
    guild_id = "123456"

    mock_guild_obj = MagicMock(spec_set=discord.Guild)
    mock_guild_obj.id = int(guild_id)
    mock_bot.get_guild.return_value = mock_guild_obj

    async with test_db() as session:
        with patch('services.ring_bearer_service.get_config', return_value=mock_config), \
             patch('services.ring_bearer_service.db_session', return_value=session), \
             patch('services.ring_bearer_service.get_leaderboard_leader') as mock_get_leader, \
             patch('services.ring_bearer_service.transfer_ring_bearer') as mock_transfer:

            mock_get_leader.side_effect = [
                None,  # No perfect_streak leader
                None,  # No longest_win_streak leader
                create_leaderboard_result("player_C", 8, category="draft_win_streak")
            ]

            await update_ring_bearer_for_guild(mock_bot, guild_id)

            mock_transfer.assert_called_once()
            call_args = mock_transfer.call_args
            assert call_args[1]['new_bearer_id'] == "player_C"
            assert call_args[1]['acquired_via'] == "draft_win_streak"


@pytest.mark.asyncio
async def test_refresh_current_holder_keeps_ring_if_still_first(test_db, mock_bot, mock_config):
    """Current ring bearer who is still #1 on first category keeps ring (no transfer)."""
    guild_id = "123456"

    mock_guild_obj = MagicMock(spec_set=discord.Guild)
    mock_guild_obj.id = int(guild_id)
    mock_bot.get_guild.return_value = mock_guild_obj

    async with test_db() as session:
        # Create existing ring bearer state
        await create_ring_bearer_state("123456", "player_A", "perfect_streak", session)

        with patch('services.ring_bearer_service.get_config', return_value=mock_config), \
             patch('services.ring_bearer_service.db_session', return_value=session), \
             patch('services.ring_bearer_service.get_leaderboard_leader') as mock_get_leader, \
             patch('services.ring_bearer_service.transfer_ring_bearer') as mock_transfer:

            # Only return for first category - player A is still #1 and has ring
            mock_get_leader.side_effect = [
                create_leaderboard_result("player_A", 5, category="perfect_streak"),  # Same player still #1
            ]

            await update_ring_bearer_for_guild(mock_bot, guild_id)

            # No transfer should occur
            mock_transfer.assert_not_called()


# ==================== CATEGORY 3: MATCH DEFEAT TRANSFER TESTS ====================

@pytest.mark.asyncio
async def test_match_defeat_transfers_ring(test_db, mock_bot, mock_config):
    """When ring bearer loses a match, winner gets ring immediately."""
    guild_id = "123456"

    mock_guild_obj = MagicMock(spec_set=discord.Guild)
    mock_guild_obj.id = int(guild_id)
    mock_bot.get_guild.return_value = mock_guild_obj

    async with test_db() as session:
        # Create ring bearer state
        await create_ring_bearer_state(guild_id, "player_A", "perfect_streak", session)

        with patch('services.ring_bearer_service.get_config', return_value=mock_config), \
             patch('services.ring_bearer_service.db_session', return_value=session), \
             patch('services.ring_bearer_service.transfer_ring_bearer') as mock_transfer:

            await check_match_defeat_transfer(
                mock_bot,
                guild_id,
                winner_id="player_B",
                loser_id="player_A",
                session_id="session_123"
            )

            mock_transfer.assert_called_once()
            call_args = mock_transfer.call_args
            assert call_args[1]['new_bearer_id'] == "player_B"
            assert call_args[1]['acquired_via'] == "match_defeat"
            assert call_args[1]['previous_bearer_id'] == "player_A"


@pytest.mark.asyncio
async def test_match_defeat_no_transfer_if_loser_not_bearer(test_db, mock_bot, mock_config):
    """When non-ring-bearer loses, no transfer occurs."""
    guild_id = "123456"

    async with test_db() as session:
        # Create ring bearer state for player_A
        await create_ring_bearer_state(guild_id, "player_A", "perfect_streak", session)

        with patch('services.ring_bearer_service.get_config', return_value=mock_config), \
             patch('services.ring_bearer_service.db_session', return_value=session), \
             patch('services.ring_bearer_service.transfer_ring_bearer') as mock_transfer:

            # player_B defeats player_C (neither is bearer)
            await check_match_defeat_transfer(
                mock_bot,
                guild_id,
                winner_id="player_B",
                loser_id="player_C",
                session_id="session_123"
            )

            mock_transfer.assert_not_called()


@pytest.mark.asyncio
async def test_match_defeat_no_transfer_if_no_bearer(test_db, mock_bot, mock_config):
    """When no ring bearer exists, match defeats don't create one."""
    guild_id = "123456"

    async with test_db() as session:
        # No ring bearer state in DB

        with patch('services.ring_bearer_service.get_config', return_value=mock_config), \
             patch('services.ring_bearer_service.db_session', return_value=session), \
             patch('services.ring_bearer_service.transfer_ring_bearer') as mock_transfer:

            await check_match_defeat_transfer(
                mock_bot,
                guild_id,
                winner_id="player_A",
                loser_id="player_B",
                session_id="session_123"
            )

            mock_transfer.assert_not_called()


# ==================== CATEGORY 4: TIE AND EXTENSION SCENARIOS ====================

@pytest.mark.asyncio
async def test_tie_for_first_transfers_to_most_recent_updater(test_db, mock_bot, mock_config):
    """When two players tie for #1, the most recently updated streak gets the ring."""
    guild_id = "123456"

    mock_guild_obj = MagicMock(spec_set=discord.Guild)
    mock_guild_obj.id = int(guild_id)
    mock_bot.get_guild.return_value = mock_guild_obj

    async with test_db() as session:
        # Current bearer is player_A with older streak
        await create_ring_bearer_state(guild_id, "player_A", "longest_win_streak", session)

        with patch('services.ring_bearer_service.get_config', return_value=mock_config), \
             patch('services.ring_bearer_service.db_session', return_value=session), \
             patch('services.ring_bearer_service.get_leaderboard_leader') as mock_get_leader, \
             patch('services.ring_bearer_service.transfer_ring_bearer') as mock_transfer:

            # Player_B most recently extended to 10 wins (becomes #1 via recency)
            # The leaderboard query should use timestamp-based tiebreaker, returning most recent updater
            mock_get_leader.side_effect = [
                None,  # No perfect_streak
                create_leaderboard_result("player_B", 10, category="longest_win_streak")
            ]

            await update_ring_bearer_for_guild(mock_bot, guild_id)

            # Transfer should occur to player_B (most recently updated)
            mock_transfer.assert_called_once()
            call_args = mock_transfer.call_args
            assert call_args[1]['new_bearer_id'] == "player_B"


@pytest.mark.asyncio
async def test_different_leaderboard_holder_extends_gets_ring(test_db, mock_bot, mock_config):
    """Player #1 on longest_win_streak extends (most recent) - gets ring despite lower category priority."""
    guild_id = "123456"

    mock_guild_obj = MagicMock(spec_set=discord.Guild)
    mock_guild_obj.id = int(guild_id)
    mock_bot.get_guild.return_value = mock_guild_obj

    async with test_db() as session:
        # player_A has ring via perfect_streak (older update)
        await create_ring_bearer_state(guild_id, "player_A", "perfect_streak", session)

        with patch('services.ring_bearer_service.get_config', return_value=mock_config), \
             patch('services.ring_bearer_service.db_session', return_value=session), \
             patch('services.ring_bearer_service.get_leaderboard_leader') as mock_get_leader, \
             patch('services.ring_bearer_service.transfer_ring_bearer') as mock_transfer:

            # Leaderboard returns player_B as #1 (most recently updated, even though longest_win_streak is lower priority)
            # The leaderboard query should sort by recency FIRST, then category priority
            mock_get_leader.side_effect = [
                create_leaderboard_result("player_B", 11, category="longest_win_streak"),  # Most recent update
            ]

            await update_ring_bearer_for_guild(mock_bot, guild_id)

            # Transfer should occur - recency trumps category priority
            mock_transfer.assert_called_once()
            call_args = mock_transfer.call_args
            assert call_args[1]['new_bearer_id'] == "player_B"


@pytest.mark.asyncio
async def test_category_priority_used_when_same_timestamp(test_db, mock_bot, mock_config):
    """When multiple streaks updated at same time, category priority (perfect → match → draft) is tiebreaker."""
    guild_id = "123456"

    mock_guild_obj = MagicMock(spec_set=discord.Guild)
    mock_guild_obj.id = int(guild_id)
    mock_bot.get_guild.return_value = mock_guild_obj

    async with test_db() as session:
        # No current bearer

        with patch('services.ring_bearer_service.get_config', return_value=mock_config), \
             patch('services.ring_bearer_service.db_session', return_value=session), \
             patch('services.ring_bearer_service.get_leaderboard_leader') as mock_get_leader, \
             patch('services.ring_bearer_service.transfer_ring_bearer') as mock_transfer:

            # After a draft completes, multiple players might have updated streaks at same time
            # Leaderboard query should return perfect_streak #1 (highest priority when timestamps equal)
            mock_get_leader.side_effect = [
                create_leaderboard_result("player_A", 5, category="perfect_streak"),  # Highest priority, same timestamp
            ]

            await update_ring_bearer_for_guild(mock_bot, guild_id)

            # Player A gets ring (category priority tiebreaker)
            mock_transfer.assert_called_once()
            call_args = mock_transfer.call_args
            assert call_args[1]['new_bearer_id'] == "player_A"
            assert call_args[1]['acquired_via'] == "perfect_streak"


@pytest.mark.asyncio
async def test_current_holder_extends_run_keeps_ring(test_db, mock_bot, mock_config):
    """Ring bearer extends their perfect streak, keeps ring."""
    guild_id = "123456"

    mock_guild_obj = MagicMock(spec_set=discord.Guild)
    mock_guild_obj.id = int(guild_id)
    mock_bot.get_guild.return_value = mock_guild_obj

    async with test_db() as session:
        await create_ring_bearer_state(guild_id, "player_A", "perfect_streak", session)

        with patch('services.ring_bearer_service.get_config', return_value=mock_config), \
             patch('services.ring_bearer_service.db_session', return_value=session), \
             patch('services.ring_bearer_service.get_leaderboard_leader') as mock_get_leader, \
             patch('services.ring_bearer_service.transfer_ring_bearer') as mock_transfer:

            # Player A extends streak but still is the leader
            mock_get_leader.side_effect = [
                create_leaderboard_result("player_A", 6, category="perfect_streak")
            ]

            await update_ring_bearer_for_guild(mock_bot, guild_id)

            mock_transfer.assert_not_called()


@pytest.mark.asyncio
async def test_current_holder_extends_but_overtaken(test_db, mock_bot, mock_config):
    """Ring bearer extends streak but another player surpasses them."""
    guild_id = "123456"

    mock_guild_obj = MagicMock(spec_set=discord.Guild)
    mock_guild_obj.id = int(guild_id)
    mock_bot.get_guild.return_value = mock_guild_obj

    async with test_db() as session:
        await create_ring_bearer_state(guild_id, "player_A", "perfect_streak", session)

        with patch('services.ring_bearer_service.get_config', return_value=mock_config), \
             patch('services.ring_bearer_service.db_session', return_value=session), \
             patch('services.ring_bearer_service.get_leaderboard_leader') as mock_get_leader, \
             patch('services.ring_bearer_service.transfer_ring_bearer') as mock_transfer:

            # Player B is now #1
            mock_get_leader.side_effect = [
                create_leaderboard_result("player_B", 7, category="perfect_streak")
            ]

            await update_ring_bearer_for_guild(mock_bot, guild_id)

            mock_transfer.assert_called_once()
            call_args = mock_transfer.call_args
            assert call_args[1]['new_bearer_id'] == "player_B"


# ==================== CATEGORY 5: SEQUENTIAL TIMING TESTS ====================

@pytest.mark.asyncio
async def test_match_defeat_then_leaderboard_update(test_db, mock_bot, mock_config):
    """Ring bearer defeated in match, then leaderboard updates - demonstrates independent operations."""
    guild_id = "123456"

    mock_guild_obj = MagicMock(spec_set=discord.Guild)
    mock_guild_obj.id = int(guild_id)
    mock_bot.get_guild.return_value = mock_guild_obj

    async with test_db() as session:
        # Initial: player_A has ring
        await create_ring_bearer_state(guild_id, "player_A", "perfect_streak", session)

        # Step 1: Match defeat - player_B defeats player_A
        with patch('services.ring_bearer_service.get_config', return_value=mock_config), \
             patch('services.ring_bearer_service.db_session', return_value=session), \
             patch('services.ring_bearer_service.transfer_ring_bearer') as mock_transfer:

            await check_match_defeat_transfer(
                mock_bot,
                guild_id,
                winner_id="player_B",
                loser_id="player_A",
                session_id="session_123"
            )

            # Should transfer to player_B
            mock_transfer.assert_called_once()
            assert mock_transfer.call_args[1]['new_bearer_id'] == "player_B"

        # Update state manually for next step
        await RingBearerState.set_ring_bearer(
            guild_id, "player_B", "match_defeat", "player_A", session
        )

        # Step 2: Leaderboard update - player_A still #1 on leaderboard
        with patch('services.ring_bearer_service.get_config', return_value=mock_config), \
             patch('services.ring_bearer_service.db_session', return_value=session), \
             patch('services.ring_bearer_service.get_leaderboard_leader') as mock_get_leader, \
             patch('services.ring_bearer_service.transfer_ring_bearer') as mock_transfer2:

            # Player A is back to #1 on leaderboard
            mock_get_leader.side_effect = [
                create_leaderboard_result("player_A", 5, category="perfect_streak")
            ]

            await update_ring_bearer_for_guild(mock_bot, guild_id)

            # Should transfer back to player_A
            mock_transfer2.assert_called_once()
            assert mock_transfer2.call_args[1]['new_bearer_id'] == "player_A"


@pytest.mark.asyncio
async def test_leaderboard_update_then_match_defeat(test_db, mock_bot, mock_config):
    """Leaderboard gives ring to #1, then they lose match - ring transfers again."""
    guild_id = "123456"

    mock_guild_obj = MagicMock(spec_set=discord.Guild)
    mock_guild_obj.id = int(guild_id)
    mock_bot.get_guild.return_value = mock_guild_obj

    async with test_db() as session:
        # Initial: player_B has ring
        await create_ring_bearer_state(guild_id, "player_B", "longest_win_streak", session)

        # Step 1: Leaderboard update - player_A is #1
        with patch('services.ring_bearer_service.get_config', return_value=mock_config), \
             patch('services.ring_bearer_service.db_session', return_value=session), \
             patch('services.ring_bearer_service.get_leaderboard_leader') as mock_get_leader, \
             patch('services.ring_bearer_service.transfer_ring_bearer') as mock_transfer:

            mock_get_leader.side_effect = [
                create_leaderboard_result("player_A", 5, category="perfect_streak")
            ]

            await update_ring_bearer_for_guild(mock_bot, guild_id)

            mock_transfer.assert_called_once()
            assert mock_transfer.call_args[1]['new_bearer_id'] == "player_A"

        # Update state for next step
        await RingBearerState.set_ring_bearer(
            guild_id, "player_A", "perfect_streak", "player_B", session
        )

        # Step 2: Match defeat - player_C defeats player_A
        with patch('services.ring_bearer_service.get_config', return_value=mock_config), \
             patch('services.ring_bearer_service.db_session', return_value=session), \
             patch('services.ring_bearer_service.transfer_ring_bearer') as mock_transfer2:

            await check_match_defeat_transfer(
                mock_bot,
                guild_id,
                winner_id="player_C",
                loser_id="player_A",
                session_id="session_456"
            )

            mock_transfer2.assert_called_once()
            assert mock_transfer2.call_args[1]['new_bearer_id'] == "player_C"
            assert mock_transfer2.call_args[1]['acquired_via'] == "match_defeat"


# ==================== CATEGORY 6: CONFIGURATION AND EDGE CASES ====================

@pytest.mark.asyncio
async def test_feature_disabled_no_transfer(test_db, mock_bot):
    """When ring_bearer.enabled = False, no transfers occur."""
    guild_id = "123456"

    mock_guild_obj = MagicMock(spec_set=discord.Guild)
    mock_guild_obj.id = int(guild_id)
    mock_bot.get_guild.return_value = mock_guild_obj

    disabled_config = {
        "ring_bearer": {
            "enabled": False  # Feature disabled
        }
    }

    async with test_db() as session:
        with patch('services.ring_bearer_service.get_config', return_value=disabled_config), \
             patch('services.ring_bearer_service.db_session', return_value=session), \
             patch('services.ring_bearer_service.transfer_ring_bearer') as mock_transfer:

            await update_ring_bearer_for_guild(mock_bot, guild_id)

            mock_transfer.assert_not_called()


@pytest.mark.asyncio
async def test_guild_not_found_graceful_failure(test_db, mock_bot, mock_config):
    """When bot.get_guild returns None, function returns gracefully."""
    guild_id = "123456"

    # Mock returns None (guild not found)
    mock_bot.get_guild.return_value = None

    async with test_db() as session:
        with patch('services.ring_bearer_service.get_config', return_value=mock_config), \
             patch('services.ring_bearer_service.db_session', return_value=session):

            # Should not raise exception
            await update_ring_bearer_for_guild(mock_bot, guild_id)

            # No error expected


@pytest.mark.asyncio
async def test_all_leaderboards_empty_no_transfer(test_db, mock_bot, mock_config):
    """When all leaderboards return None, no transfer occurs."""
    guild_id = "123456"

    mock_guild_obj = MagicMock(spec_set=discord.Guild)
    mock_guild_obj.id = int(guild_id)
    mock_bot.get_guild.return_value = mock_guild_obj

    async with test_db() as session:
        await create_ring_bearer_state(guild_id, "player_A", "perfect_streak", session)

        with patch('services.ring_bearer_service.get_config', return_value=mock_config), \
             patch('services.ring_bearer_service.db_session', return_value=session), \
             patch('services.ring_bearer_service.get_leaderboard_leader') as mock_get_leader, \
             patch('services.ring_bearer_service.transfer_ring_bearer') as mock_transfer:

            # All leaderboards empty
            mock_get_leader.side_effect = [None, None, None]

            await update_ring_bearer_for_guild(mock_bot, guild_id)

            mock_transfer.assert_not_called()


# ==================== CATEGORY 7: DISCORD ROLE SYNC TESTS ====================

@pytest.mark.asyncio
async def test_role_sync_adds_role_to_new_bearer(mock_member):
    """sync_ring_bearer_role should call member.add_roles()."""
    # Setup
    guild = MagicMock(spec_set=discord.Guild)
    guild.id = 123456

    ring_bearer_role = MagicMock(spec_set=discord.Role)
    ring_bearer_role.name = "ring bearer"
    guild.roles = [ring_bearer_role]

    old_bearer = mock_member("111", "OldBearer")
    old_bearer.roles = [ring_bearer_role]
    new_bearer = mock_member("222", "NewBearer")
    new_bearer.roles = []

    guild.get_member = MagicMock(side_effect=lambda x: old_bearer if x == 111 else new_bearer)

    with patch('discord.utils.get', return_value=ring_bearer_role):
        # Act
        await sync_ring_bearer_role(guild, "222", "111", "ring bearer")

        # Assert
        old_bearer.remove_roles.assert_called_once_with(ring_bearer_role)
        new_bearer.add_roles.assert_called_once_with(ring_bearer_role)


@pytest.mark.asyncio
async def test_role_sync_role_not_found_logs_warning():
    """When ring bearer role doesn't exist, log warning and return."""
    guild = MagicMock(spec_set=discord.Guild)
    guild.id = 123456
    guild.roles = []

    with patch('discord.utils.get', return_value=None):  # Role not found
        # Should not raise exception
        await sync_ring_bearer_role(guild, "222", "111", "ring bearer")

        # No error expected


@pytest.mark.asyncio
async def test_role_sync_member_not_found_logs_warning(mock_member):
    """When member not in guild, log warning and continue."""
    guild = MagicMock(spec_set=discord.Guild)
    guild.id = 123456

    ring_bearer_role = MagicMock(spec_set=discord.Role)
    ring_bearer_role.name = "ring bearer"

    old_bearer = mock_member("111", "OldBearer")
    old_bearer.roles = [ring_bearer_role]

    # New bearer not in guild
    guild.get_member = MagicMock(side_effect=lambda x: old_bearer if x == 111 else None)

    with patch('discord.utils.get', return_value=ring_bearer_role):
        # Should not raise exception
        await sync_ring_bearer_role(guild, "222", "111", "ring bearer")

        # Old bearer role should still be removed
        old_bearer.remove_roles.assert_called_once()
