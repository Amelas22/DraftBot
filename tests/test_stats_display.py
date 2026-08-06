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
from models import QuizStats, TrophyQuizSession, TrophyQuizSubmission
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

    @pytest.mark.asyncio
    async def test_player_skill_rating_established(self, test_db):
        from stats_display import _player_skill_rating
        async with AsyncSessionLocal() as session:
            session.add(PlayerStats(
                player_id="555", guild_id="g", display_name="P",
                true_skill_mu=30.0, true_skill_sigma=1.0,
                games_won=15, games_lost=10))          # 25 >= 20 -> established
            await session.commit()
        rating, provisional = await _player_skill_rating("555", "g")
        assert rating == 1716    # 1500 + (30-25) * (25/55) * 95
        assert provisional is False

    @pytest.mark.asyncio
    async def test_player_skill_rating_provisional(self, test_db):
        from stats_display import _player_skill_rating
        async with AsyncSessionLocal() as session:
            session.add(PlayerStats(
                player_id="556", guild_id="g", display_name="P",
                true_skill_mu=30.0, true_skill_sigma=1.0,
                games_won=3, games_lost=2))            # 5 < 20 -> provisional
            await session.commit()
        rating, provisional = await _player_skill_rating("556", "g")
        assert rating == 1568    # 1500 + (30-25) * (5/35) * 95
        assert provisional is True

    @pytest.mark.asyncio
    async def test_player_skill_rating_none_when_no_row(self, test_db):
        from stats_display import _player_skill_rating
        rating, provisional = await _player_skill_rating("999", "g")
        assert rating is None and provisional is None


def _trophy_quiz_session(quiz_id, guild_id, display_id=1):
    return TrophyQuizSession(
        quiz_id=quiz_id, display_id=display_id, guild_id=guild_id, channel_id="c",
        draft_session_id=f"d-{quiz_id}", posted_by="mod",
        decks=[{"slot": "A", "drafter_id": "u1", "wins": 3},
               {"slot": "B", "drafter_id": "u2", "wins": 0}],
    )


def _trophy_submission(quiz_id, player_id, points, direction_correct=True, finalized=True):
    return TrophyQuizSubmission(
        quiz_id=quiz_id, player_id=player_id, display_name="P",
        guesses=[3, 0], direction_correct=direction_correct,
        exact_points=[3, 3], points_earned=points, finalized=finalized,
    )


class TestPlayerQuizStats:
    """Tests for _player_quiz_stats and the quiz field on the /stats embed."""

    @pytest.mark.asyncio
    async def test_none_when_no_quiz_history(self, test_db):
        from stats_display import _player_quiz_stats
        pick, trophy = await _player_quiz_stats("123", "g")
        assert pick is None and trophy is None

    @pytest.mark.asyncio
    async def test_pick_quiz_reads_aggregated_row(self, test_db):
        from stats_display import _player_quiz_stats
        async with AsyncSessionLocal() as session:
            session.add(QuizStats(
                player_id="123", guild_id="g", display_name="P",
                total_quizzes=12, total_picks_attempted=48, total_picks_correct=23,
                accuracy_percentage=47.9, total_points=156, highest_quiz_score=20,
                current_perfect_streak=2, longest_perfect_streak=4))
            await session.commit()
        pick, trophy = await _player_quiz_stats("123", "g")
        assert pick == {"played": 12, "accuracy": 47.9, "points": 156, "best": 20}
        assert trophy is None

    @pytest.mark.asyncio
    async def test_trophy_aggregates_finalized_own_guild_only(self, test_db):
        from stats_display import _player_quiz_stats
        async with AsyncSessionLocal() as session:
            session.add(_trophy_quiz_session("q1", "g", display_id=1))
            session.add(_trophy_quiz_session("q2", "g", display_id=2))
            session.add(_trophy_quiz_session("q3", "g", display_id=3))
            session.add(_trophy_quiz_session("q4", "g", display_id=4))
            session.add(_trophy_quiz_session("q-other", "other-guild"))
            session.add(_trophy_submission("q1", "123", points=10, direction_correct=True))
            session.add(_trophy_submission("q2", "123", points=7, direction_correct=True))
            session.add(_trophy_submission("q3", "123", points=3, direction_correct=False))
            session.add(_trophy_submission("q4", "123", points=8, finalized=False))   # pending: excluded
            session.add(_trophy_submission("q-other", "123", points=10))              # other guild: excluded
            session.add(_trophy_submission("q1", "456", points=10))                   # other player: excluded
            await session.commit()
        pick, trophy = await _player_quiz_stats("123", "g")
        assert pick is None
        # direction_correct must be a COUNT (2), not a boolean coerced to True —
        # summing the raw Boolean column regresses to True and 2 catches it.
        assert trophy == {"played": 3, "points": 20, "direction_correct": 2}

    @pytest.mark.asyncio
    async def test_embed_shows_quiz_field_with_both_types(self, test_db):
        async with AsyncSessionLocal() as session:
            session.add(QuizStats(
                player_id="123456789", guild_id="test_guild", display_name="P",
                total_quizzes=5, accuracy_percentage=50.0, total_points=40,
                highest_quiz_score=12, current_perfect_streak=1, longest_perfect_streak=2))
            session.add(_trophy_quiz_session("q1", "test_guild"))
            session.add(_trophy_submission("q1", "123456789", points=10))
            await session.commit()

        mock_bot = AsyncMock()
        mock_user = MagicMock()
        mock_user.id = 123456789
        mock_user.display_name = "TestPlayer"
        mock_user.avatar = None
        mock_bot.fetch_user.return_value = mock_user

        embed = await get_stats_embed_for_player(
            bot=mock_bot, player_id="123456789", guild_id="test_guild",
            display_name="TestPlayer")

        quiz_fields = [f for f in embed.fields if "Quiz" in f.name]
        assert len(quiz_fields) == 1
        value = quiz_fields[0].value
        assert "Pick Quiz** (5 played)" in value
        assert "50% of picks guessed right" in value
        assert "streak" not in value.lower()
        assert "Trophy Quiz** (1 played)" in value and "10 pts" in value
        assert "directionally right 100%" in value

    @pytest.mark.asyncio
    async def test_embed_omits_quiz_field_without_quiz_history(self, test_db):
        mock_bot = AsyncMock()
        mock_user = MagicMock()
        mock_user.id = 123456789
        mock_user.display_name = "TestPlayer"
        mock_user.avatar = None
        mock_bot.fetch_user.return_value = mock_user

        embed = await get_stats_embed_for_player(
            bot=mock_bot, player_id="123456789", guild_id="test_guild",
            display_name="TestPlayer")

        assert not any("Quiz" in f.name for f in embed.fields)
