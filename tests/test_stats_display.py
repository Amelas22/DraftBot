"""
Unit tests for stats_display module - high-level display functions.
"""
import pytest
import pytest_asyncio
import tempfile
import os
from unittest.mock import AsyncMock, MagicMock
from database.models_base import Base
from database.db_session import AsyncSessionLocal
from sqlalchemy.ext.asyncio import create_async_engine

from stats_display import get_stats_embed_for_player
from models.draft_session import DraftSession
from models.player import PlayerStats
from models.debt_ledger import DebtLedger


@pytest_asyncio.fixture
async def test_db():
    """Create a temporary test database"""
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db.close()

    engine = create_async_engine(f"sqlite+aiosqlite:///{temp_db.name}")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    AsyncSessionLocal.configure(bind=engine)

    yield engine

    await engine.dispose()
    os.unlink(temp_db.name)


class TestGetStatsEmbedForPlayer:
    """Tests for get_stats_embed_for_player function"""

    @pytest.mark.asyncio
    async def test_returns_embed_for_player(self, test_db):
        """Test that function returns a Discord embed"""
        # Create a mock bot
        mock_bot = AsyncMock()
        mock_user = MagicMock()
        mock_user.id = 123456789
        mock_user.display_name = "TestPlayer"
        mock_user.avatar = None
        mock_bot.fetch_user.return_value = mock_user

        # Call the function
        embed = await get_stats_embed_for_player(
            bot=mock_bot,
            player_id="123456789",
            guild_id="test_guild",
            display_name="TestPlayer"
        )

        # Verify embed was created
        assert embed is not None
        assert hasattr(embed, 'title')
        assert hasattr(embed, 'fields')

    @pytest.mark.asyncio
    async def test_handles_missing_user(self, test_db):
        """Test that function handles when Discord user fetch fails"""
        # Create a mock bot that fails to fetch user
        mock_bot = AsyncMock()
        mock_bot.fetch_user.side_effect = Exception("User not found")

        # Call the function with display_name
        embed = await get_stats_embed_for_player(
            bot=mock_bot,
            player_id="999999999",
            guild_id="test_guild",
            display_name="MissingPlayer"
        )

        # Should still return an embed (using MockUser fallback)
        assert embed is not None
        assert hasattr(embed, 'title')

    @pytest.mark.asyncio
    async def test_integrates_weekly_monthly_lifetime_stats(self, test_db):
        """Test that function calls legacy stats for all 3 timeframes"""
        # Create player stats in database
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                player = PlayerStats(
                    player_id="123456789",
                    guild_id="test_guild",
                    display_name="TestPlayer",
                    drafts_participated=10,
                    games_won=20,
                    games_lost=15,
                    team_drafts_won=5,
                    team_drafts_lost=3,
                    team_drafts_tied=2
                )
                db_session.add(player)
                await db_session.commit()

        # Mock bot
        mock_bot = AsyncMock()
        mock_user = MagicMock()
        mock_user.id = 123456789
        mock_user.display_name = "TestPlayer"
        mock_user.avatar = None
        mock_bot.fetch_user.return_value = mock_user

        # Call the function
        embed = await get_stats_embed_for_player(
            bot=mock_bot,
            player_id="123456789",
            guild_id="test_guild",
            display_name="TestPlayer"
        )

        # Verify embed has the expected structure
        assert embed is not None
        assert len(embed.fields) > 0

        # Verify embed contains expected timeframe sections
        field_names = [field.name for field in embed.fields]
        assert any('Weekly' in name or 'Week' in name for name in field_names)
        assert any('Monthly' in name or 'Month' in name for name in field_names)
        assert any('Lifetime' in name or 'All-Time' in name or 'All Time' in name for name in field_names)

    @pytest.mark.asyncio
    async def test_with_no_stats_in_database(self, test_db):
        """Test function works even when player has no stats"""
        # Mock bot
        mock_bot = AsyncMock()
        mock_user = MagicMock()
        mock_user.id = 999999999
        mock_user.display_name = "NewPlayer"
        mock_user.avatar = None
        mock_bot.fetch_user.return_value = mock_user

        # Call function for player with no stats
        embed = await get_stats_embed_for_player(
            bot=mock_bot,
            player_id="999999999",
            guild_id="test_guild",
            display_name="NewPlayer"
        )

        # Should still return embed with zero stats
        assert embed is not None
        assert hasattr(embed, 'fields')

    @pytest.mark.asyncio
    async def test_display_name_optional(self, test_db):
        """Test that display_name parameter is optional"""
        # Mock bot
        mock_bot = AsyncMock()
        mock_user = MagicMock()
        mock_user.id = 123456789
        mock_user.display_name = "FetchedName"
        mock_user.avatar = None
        mock_bot.fetch_user.return_value = mock_user

        # Call without display_name
        embed = await get_stats_embed_for_player(
            bot=mock_bot,
            player_id="123456789",
            guild_id="test_guild"
            # display_name not provided
        )

        assert embed is not None

    @pytest.mark.asyncio
    async def test_embed_footer_present(self, test_db):
        """Test that embed has footer text"""
        # Mock bot
        mock_bot = AsyncMock()
        mock_user = MagicMock()
        mock_user.id = 123456789
        mock_user.display_name = "TestPlayer"
        mock_user.avatar = None
        mock_bot.fetch_user.return_value = mock_user

        # Call function
        embed = await get_stats_embed_for_player(
            bot=mock_bot,
            player_id="123456789",
            guild_id="test_guild",
            display_name="TestPlayer"
        )

        # Verify footer exists
        assert embed.footer is not None
        assert embed.footer.text is not None
        assert len(embed.footer.text) > 0
