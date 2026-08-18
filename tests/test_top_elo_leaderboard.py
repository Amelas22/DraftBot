"""
Unit tests for the Top Elo leaderboard category.

The board ranks a guild's highest-rated players who have drafted recently.
Qualifying requires all three of:
  - a display rating strictly above TOP_ELO_MIN_RATING
  - a draft inside the guild's crown window (crown_roles.timeframe), so the
    board's "recently" matches the cycle players already know from crowns
  - enough rated games to be established (helpers.skill.is_established)
"""

import os
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from database.models_base import Base
import leaderboard_config
from leaderboard_config import (
    ALL_CATEGORIES, CATEGORY_CONFIGS, CROWN_ELIGIBLE_CATEGORIES,
    DEFAULT_CROWN_ACTIVITY_TIMEFRAME, crown_activity_timeframe,
)
from models.leaderboard_message import LeaderboardMessage
from models.player import PlayerStats
import services.leaderboard_service as leaderboard_service
from services.leaderboard_formatter import create_leaderboard_embed
from helpers.skill import (
    ESTABLISHED_GAMES, PRIOR_MU, PRIOR_SIGMA, RATING_ANCHOR,
    RATING_POINTS_PER_MU, RATING_SHRINK_GAMES, skill_rating,
)
from services.leaderboard_service import (
    TOP_ELO_LIMIT, TOP_ELO_MIN_RATING, get_top_elo_leaderboard_data,
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


@pytest.fixture(autouse=True)
def crown_window():
    """Pin the guild's crown timeframe for every test.

    Always patched: a real get_config() call would materialise a config file
    for the fake test guild as a side effect.
    """
    with patch.object(leaderboard_config, "get_config") as get_config:
        get_config.return_value = {"crown_roles": {"timeframe": "30d"}}
        yield get_config


def set_crown_timeframe(crown_window, timeframe):
    crown_window.return_value = {"crown_roles": {"timeframe": timeframe}}


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


async def _fetch(test_db, *players):
    """Seed a database and return the board."""
    async with test_db() as session:
        session.add_all(players)
        await session.commit()
        return await get_top_elo_leaderboard_data(GUILD, "lifetime", 20, session)


# ============================================================================
# RANKING AND ELIGIBILITY
# ============================================================================

@pytest.mark.asyncio
async def test_ranks_qualifying_players_by_rating_descending(test_db):
    """The board lists qualifying players strongest first, with their rating."""
    data = await _fetch(
        test_db,
        player("111", "Middle", 1700),
        player("222", "Strongest", 1800),
        player("333", "Weakest", 1660),
    )

    assert [p["player_id"] for p in data] == ["222", "111", "333"]
    assert [p["rating"] for p in data] == [1800, 1700, 1660]
    assert data[0]["display_name"] == "Strongest"


@pytest.mark.asyncio
async def test_excludes_rating_exactly_at_threshold(test_db):
    """The floor is strict: a rating equal to the minimum does not qualify."""
    data = await _fetch(test_db, player("111", "OnTheLine", TOP_ELO_MIN_RATING))

    assert data == []


@pytest.mark.asyncio
async def test_includes_rating_one_point_above_threshold(test_db):
    """One point over the floor is enough to qualify."""
    data = await _fetch(test_db, player("111", "JustOver", TOP_ELO_MIN_RATING + 1))

    assert [p["player_id"] for p in data] == ["111"]


@pytest.mark.asyncio
async def test_excludes_player_with_no_recorded_draft(test_db):
    """A null last_draft_timestamp is not activity."""
    never = player("111", "NeverDrafted", 1800)
    never.last_draft_timestamp = None

    data = await _fetch(test_db, never)

    assert data == []


@pytest.mark.asyncio
async def test_excludes_provisional_player_under_established_games(test_db):
    """A high rating on too few rated games does not qualify."""
    data = await _fetch(
        test_db,
        player("111", "HotStart", 1800, games=ESTABLISHED_GAMES - 1),
        player("222", "Proven", 1700, games=ESTABLISHED_GAMES),
    )

    assert [p["player_id"] for p in data] == ["222"]


@pytest.mark.asyncio
async def test_returns_at_most_ten_players(test_db):
    """The board is a top ten even when more players qualify."""
    data = await _fetch(
        test_db, *[player(f"p{i:02d}", f"Player{i}", 1700 + i) for i in range(12)])

    assert len(data) == TOP_ELO_LIMIT
    assert data[0]["rating"] == 1711


@pytest.mark.asyncio
async def test_breaks_rating_ties_by_rated_games_then_player_id(test_db):
    """Equal ratings order deterministically, so refreshes don't reshuffle."""
    data = await _fetch(
        test_db,
        player("300", "FewerGames", 1700, games=30),
        player("200", "MoreGamesB", 1700, games=40),
        player("100", "MoreGamesA", 1700, games=40),
    )

    assert [p["player_id"] for p in data] == ["100", "200", "300"]


@pytest.mark.asyncio
async def test_excludes_players_from_other_guilds(test_db):
    """The board is scoped to the requested guild."""
    data = await _fetch(
        test_db,
        player("111", "Ours", 1700),
        player("222", "Theirs", 1900, guild="other-guild"),
    )

    assert [p["player_id"] for p in data] == ["111"]


# ============================================================================
# ACTIVITY WINDOW — follows the guild's crown cycle
# ============================================================================

class TestActivityWindow:
    def test_defaults_to_thirty_days(self, crown_window):
        """The documented crown default, used when a guild sets none."""
        crown_window.return_value = {}
        assert crown_activity_timeframe(GUILD) == "30d"
        assert DEFAULT_CROWN_ACTIVITY_TIMEFRAME == "30d"

    def test_follows_the_configured_crown_timeframe(self, crown_window):
        set_crown_timeframe(crown_window, "90d")
        assert crown_activity_timeframe(GUILD) == "90d"

    def test_falls_back_when_crowns_run_lifetime(self, crown_window):
        """An unbounded window would contradict a board of active players."""
        set_crown_timeframe(crown_window, "lifetime")
        assert crown_activity_timeframe(GUILD) == "30d"

    def test_falls_back_on_an_unrecognised_timeframe(self, crown_window):
        set_crown_timeframe(crown_window, "banana")
        assert crown_activity_timeframe(GUILD) == "30d"

    @pytest.mark.asyncio
    async def test_excludes_a_draft_older_than_the_crown_window(
            self, test_db, crown_window):
        set_crown_timeframe(crown_window, "14d")

        data = await _fetch(test_db, player("111", "Lapsed", 1800, drafted_days_ago=20))

        assert data == []

    @pytest.mark.asyncio
    async def test_a_longer_crown_window_admits_older_activity(
            self, test_db, crown_window):
        """The same player qualifies once the guild's crown cycle is longer."""
        set_crown_timeframe(crown_window, "90d")

        data = await _fetch(test_db, player("111", "Lapsed", 1800, drafted_days_ago=20))

        assert [p["player_id"] for p in data] == ["111"]

    @pytest.mark.asyncio
    async def test_lifetime_crowns_still_bound_the_board(self, test_db, crown_window):
        set_crown_timeframe(crown_window, "lifetime")

        data = await _fetch(
            test_db,
            player("111", "LongGone", 1800, drafted_days_ago=40),
            player("222", "Recent", 1700, drafted_days_ago=20),
        )

        assert [p["player_id"] for p in data] == ["222"]


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
        description = CATEGORY_CONFIGS[CATEGORY]["description_template"]
        assert str(TOP_ELO_MIN_RATING) not in description

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


# ============================================================================
# THE RENDERED BOARD
# ============================================================================

async def _render(test_db, monkeypatch, *players, timeframe="lifetime"):
    """Build the real embed for the category against a seeded test database."""
    async with test_db() as session:
        session.add_all(players)
        await session.commit()

        @asynccontextmanager
        async def fake_db_session():
            yield session

        monkeypatch.setattr(leaderboard_service, "db_session", fake_db_session)
        return await create_leaderboard_embed(
            GUILD, category=CATEGORY, limit=20, timeframe=timeframe)


class TestRenderedBoard:
    """What a player actually reads in the channel."""

    @pytest.mark.asyncio
    async def test_entry_shows_only_name_and_rating(self, test_db, monkeypatch):
        """The rating is the whole story; game counts are tie-break data, not
        something to publish."""
        embed = await _render(test_db, monkeypatch,
                              player("111", "Champ", 1800, games=137))

        rankings = next(f for f in embed.fields if f.name == "Rankings")
        assert "Champ" in rankings.value
        assert "1800" in rankings.value
        assert "137" not in rankings.value

    @pytest.mark.asyncio
    async def test_title_reports_the_crown_window(self, test_db, monkeypatch):
        """The query ignores the selector, so a title echoing it would lie
        about which players the board can contain."""
        embed = await _render(test_db, monkeypatch,
                              player("111", "Champ", 1800), timeframe="lifetime")

        assert "Last 30 Days" in embed.title
        assert "Lifetime" not in embed.title

    @pytest.mark.asyncio
    async def test_title_tracks_a_changed_crown_window(
            self, test_db, monkeypatch, crown_window):
        set_crown_timeframe(crown_window, "90d")

        embed = await _render(test_db, monkeypatch, player("111", "Champ", 1800))

        assert "Last 90 Days" in embed.title

    @pytest.mark.asyncio
    async def test_footer_does_not_advertise_a_filter_that_does_nothing(
            self, test_db, monkeypatch):
        embed = await _render(test_db, monkeypatch, player("111", "Champ", 1800))

        assert embed.footer.text == "Updated regularly"


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
