"""
Unit tests for the Top Elo leaderboard category.

The board ranks a guild's highest-rated players who have drafted recently.
Qualifying requires all three of:
  - a display rating strictly above TOP_ELO_MIN_RATING
  - a draft within TOP_ELO_ACTIVE_DAYS (via PlayerStats.last_draft_timestamp)
  - enough rated games to be established (helpers.skill.is_established)
"""

import os
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from database.models_base import Base
from leaderboard_config import (
    ALL_CATEGORIES, CATEGORY_CONFIGS, CROWN_ELIGIBLE_CATEGORIES,
)
from models.leaderboard_message import LeaderboardMessage
from models.player import PlayerStats
import services.leaderboard_service as leaderboard_service
from helpers.skill import (
    ESTABLISHED_GAMES, PRIOR_MU, PRIOR_SIGMA, RATING_ANCHOR,
    RATING_POINTS_PER_MU, RATING_SHRINK_GAMES, skill_rating,
)
from services.leaderboard_service import (
    TOP_ELO_ACTIVE_DAYS, TOP_ELO_LIMIT, TOP_ELO_MIN_RATING,
    get_top_elo_leaderboard_data,
)


GUILD = "789"
CATEGORY = "top_elo"


@pytest_asyncio.fixture
async def test_db():
    """Create a temporary test database and return a test session factory."""
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{temp_db.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    test_session_factory = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    yield test_session_factory

    await engine.dispose()
    os.unlink(temp_db.name)


def mu_for_rating(target, games):
    """The TrueSkill mu that makes skill_rating() return exactly `target`.

    Inverts round(1500 + (mu-25) * g/(g+30) * 95). The assert guarantees the
    fixture really is the rating the test claims, so a change to the display
    formula surfaces here rather than silently shifting every boundary case.
    """
    weight = games / (games + RATING_SHRINK_GAMES)
    mu = PRIOR_MU + (target - RATING_ANCHOR) / (weight * RATING_POINTS_PER_MU)
    assert skill_rating(mu, PRIOR_SIGMA, games) == target
    return mu


def player(player_id, name, rating, games=30, drafted_days_ago=1, guild=GUILD):
    """A PlayerStats row whose display rating is exactly `rating`."""
    won = games // 2
    return PlayerStats(
        player_id=player_id,
        guild_id=guild,
        display_name=name,
        games_won=won,
        games_lost=games - won,
        true_skill_mu=mu_for_rating(rating, games),
        true_skill_sigma=PRIOR_SIGMA,
        last_draft_timestamp=datetime.now() - timedelta(days=drafted_days_ago),
    )


@pytest.mark.asyncio
async def test_ranks_qualifying_players_by_rating_descending(test_db):
    """The board lists qualifying players strongest first, with their rating."""
    async with test_db() as session:
        session.add_all([
            player("111", "Middle", 1700),
            player("222", "Strongest", 1800),
            player("333", "Weakest", 1660),
        ])
        await session.commit()

        data = await get_top_elo_leaderboard_data(GUILD, "lifetime", 20, session)

        assert [p["player_id"] for p in data] == ["222", "111", "333"]
        assert [p["rating"] for p in data] == [1800, 1700, 1660]
        assert data[0]["display_name"] == "Strongest"


@pytest.mark.asyncio
async def test_excludes_rating_exactly_at_threshold(test_db):
    """The floor is strict: a rating equal to the minimum does not qualify."""
    async with test_db() as session:
        session.add(player("111", "OnTheLine", TOP_ELO_MIN_RATING))
        await session.commit()

        data = await get_top_elo_leaderboard_data(GUILD, "lifetime", 20, session)

        assert data == []


@pytest.mark.asyncio
async def test_includes_rating_one_point_above_threshold(test_db):
    """One point over the floor is enough to qualify."""
    async with test_db() as session:
        session.add(player("111", "JustOver", TOP_ELO_MIN_RATING + 1))
        await session.commit()

        data = await get_top_elo_leaderboard_data(GUILD, "lifetime", 20, session)

        assert [p["player_id"] for p in data] == ["111"]


@pytest.mark.asyncio
async def test_excludes_player_whose_last_draft_predates_window(test_db):
    """A strong player who stopped drafting drops off the board."""
    async with test_db() as session:
        session.add(player("111", "Lapsed", 1800,
                           drafted_days_ago=TOP_ELO_ACTIVE_DAYS + 1))
        await session.commit()

        data = await get_top_elo_leaderboard_data(GUILD, "lifetime", 20, session)

        assert data == []


@pytest.mark.asyncio
async def test_includes_player_who_drafted_inside_window(test_db):
    """Drafting just inside the window still counts as active."""
    async with test_db() as session:
        session.add(player("111", "StillHere", 1800,
                           drafted_days_ago=TOP_ELO_ACTIVE_DAYS - 1))
        await session.commit()

        data = await get_top_elo_leaderboard_data(GUILD, "lifetime", 20, session)

        assert [p["player_id"] for p in data] == ["111"]


@pytest.mark.asyncio
async def test_excludes_player_with_no_recorded_draft(test_db):
    """A null last_draft_timestamp is not activity."""
    async with test_db() as session:
        never = player("111", "NeverDrafted", 1800)
        never.last_draft_timestamp = None
        session.add(never)
        await session.commit()

        data = await get_top_elo_leaderboard_data(GUILD, "lifetime", 20, session)

        assert data == []


@pytest.mark.asyncio
async def test_excludes_provisional_player_under_established_games(test_db):
    """A high rating on too few rated games does not qualify."""
    async with test_db() as session:
        session.add(player("111", "HotStart", 1800, games=ESTABLISHED_GAMES - 1))
        session.add(player("222", "Proven", 1700, games=ESTABLISHED_GAMES))
        await session.commit()

        data = await get_top_elo_leaderboard_data(GUILD, "lifetime", 20, session)

        assert [p["player_id"] for p in data] == ["222"]


@pytest.mark.asyncio
async def test_returns_at_most_ten_players(test_db):
    """The board is a top ten even when more players qualify."""
    async with test_db() as session:
        for i in range(12):
            session.add(player(f"p{i:02d}", f"Player{i}", 1700 + i))
        await session.commit()

        data = await get_top_elo_leaderboard_data(GUILD, "lifetime", 20, session)

        assert len(data) == TOP_ELO_LIMIT
        assert data[0]["rating"] == 1711


@pytest.mark.asyncio
async def test_breaks_rating_ties_by_rated_games_then_player_id(test_db):
    """Equal ratings order deterministically, so refreshes don't reshuffle."""
    async with test_db() as session:
        session.add_all([
            player("300", "FewerGames", 1700, games=30),
            player("200", "MoreGamesB", 1700, games=40),
            player("100", "MoreGamesA", 1700, games=40),
        ])
        await session.commit()

        data = await get_top_elo_leaderboard_data(GUILD, "lifetime", 20, session)

        assert [p["player_id"] for p in data] == ["100", "200", "300"]


@pytest.mark.asyncio
async def test_excludes_players_from_other_guilds(test_db):
    """The board is scoped to the requested guild."""
    async with test_db() as session:
        session.add(player("111", "Ours", 1700))
        session.add(player("222", "Theirs", 1900, guild="other-guild"))
        await session.commit()

        data = await get_top_elo_leaderboard_data(GUILD, "lifetime", 20, session)

        assert [p["player_id"] for p in data] == ["111"]


# ============================================================================
# CATEGORY REGISTRATION
# ============================================================================

class TestCategoryRegistration:
    def test_category_is_registered(self):
        """Unregistered, the board would never render anywhere."""
        assert CATEGORY in CATEGORY_CONFIGS
        assert CATEGORY in ALL_CATEGORIES

    def test_description_does_not_reveal_the_rating_floor(self):
        """The cutoff is deliberately unadvertised (owner's call)."""
        config = CATEGORY_CONFIGS[CATEGORY]
        description = config["description_template"]
        assert str(TOP_ELO_MIN_RATING) not in description
        assert "2 weeks" in description

    def test_does_not_award_a_crown(self):
        """An activity-gated board churns as players go quiet, like hot_streak."""
        assert CATEGORY not in CROWN_ELIGIBLE_CATEGORIES

    def test_formatter_renders_name_and_rating(self):
        line = CATEGORY_CONFIGS[CATEGORY]["formatter"](
            {"display_name": "Ringbearer", "rating": 1784, "rated_games": 60}, 1)
        assert "Ringbearer" in line
        assert "1784" in line

    def test_leaderboard_message_has_the_category_columns(self):
        """Two columns per category persist the posted message and its
        timeframe; without them the board can't be tracked or edited."""
        columns = LeaderboardMessage.__table__.columns
        assert f"{CATEGORY}_view_message_id" in columns
        assert f"{CATEGORY}_timeframe" in columns


@pytest.mark.asyncio
async def test_get_leaderboard_data_dispatches_to_the_top_elo_query(test_db, monkeypatch):
    """The category must be wired into the dedicated-query dispatch, or the
    generic path would fold the match ledger and return the wrong board."""
    async with test_db() as session:
        session.add(player("111", "Champ", 1800))
        session.add(player("222", "Unrated", 1500))
        await session.commit()

        @asynccontextmanager
        async def fake_db_session():
            yield session

        monkeypatch.setattr(leaderboard_service, "db_session", fake_db_session)

        data = await leaderboard_service.get_leaderboard_data(
            GUILD, category=CATEGORY, limit=20)

        assert [p["player_id"] for p in data] == ["111"]
        assert data[0]["rating"] == 1800
