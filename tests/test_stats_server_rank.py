"""Server rank on the stats page: a player's standing by skill rating among
the guild's established players, shown only inside the top SERVER_RANK_LIMIT."""
import os
import tempfile

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from database.db_session import AsyncSessionLocal
from database.models_base import Base
from models.player import PlayerStats
from player_stats import create_stats_embed
from stats_display import SERVER_RANK_LIMIT, _player_server_rank

GUILD = "g"


@pytest_asyncio.fixture
async def test_db():
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    temp_db.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{temp_db.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    AsyncSessionLocal.configure(bind=engine)
    yield engine
    await engine.dispose()
    os.unlink(temp_db.name)


async def seed(*players):
    """players: (player_id, mu, games). sigma is irrelevant to the display rating."""
    async with AsyncSessionLocal() as session:
        for player_id, mu, games in players:
            session.add(PlayerStats(
                player_id=player_id, guild_id=GUILD, display_name=player_id,
                true_skill_mu=mu, true_skill_sigma=1.0,
                games_won=games, games_lost=0,
            ))
        await session.commit()


ESTABLISHED = 25  # >= helpers.skill.ESTABLISHED_GAMES (20)


class TestPlayerServerRank:
    @pytest.mark.asyncio
    async def test_highest_rating_is_rank_one(self, test_db):
        await seed(("top", 30.0, ESTABLISHED), ("mid", 27.0, ESTABLISHED), ("low", 24.0, ESTABLISHED))
        assert await _player_server_rank("top", GUILD) == (1, 3)

    @pytest.mark.asyncio
    async def test_rank_counts_only_players_rated_higher(self, test_db):
        await seed(("top", 30.0, ESTABLISHED), ("mid", 27.0, ESTABLISHED), ("low", 24.0, ESTABLISHED))
        assert await _player_server_rank("mid", GUILD) == (2, 3)
        assert await _player_server_rank("low", GUILD) == (3, 3)

    @pytest.mark.asyncio
    async def test_ties_share_the_better_rank(self, test_db):
        # Competition ranking: two players tied for 1st, the next is 3rd.
        await seed(("a", 30.0, ESTABLISHED), ("b", 30.0, ESTABLISHED), ("c", 26.0, ESTABLISHED))
        assert await _player_server_rank("a", GUILD) == (1, 3)
        assert await _player_server_rank("b", GUILD) == (1, 3)
        assert await _player_server_rank("c", GUILD) == (3, 3)

    @pytest.mark.asyncio
    async def test_provisional_players_are_neither_ranked_nor_counted(self, test_db):
        # The provisional player's raw mu is the highest in the guild, but a
        # short record can't take a top-20 slot from an established player.
        await seed(("hotshot", 40.0, 5), ("steady", 30.0, ESTABLISHED))
        assert await _player_server_rank("hotshot", GUILD) == (None, None)
        assert await _player_server_rank("steady", GUILD) == (1, 1)

    @pytest.mark.asyncio
    async def test_ranks_past_the_limit_are_not_returned(self, test_db):
        # SERVER_RANK_LIMIT + 1 established players, all distinct ratings: the
        # weakest sits one past the cutoff.
        await seed(*[(f"p{i}", 30.0 - i * 0.1, ESTABLISHED) for i in range(SERVER_RANK_LIMIT + 1)])
        assert await _player_server_rank(f"p{SERVER_RANK_LIMIT - 1}", GUILD) == (
            SERVER_RANK_LIMIT, SERVER_RANK_LIMIT + 1)
        assert await _player_server_rank(f"p{SERVER_RANK_LIMIT}", GUILD) == (None, None)

    @pytest.mark.asyncio
    async def test_other_guilds_do_not_affect_the_rank(self, test_db):
        await seed(("home", 27.0, ESTABLISHED))
        async with AsyncSessionLocal() as session:
            session.add(PlayerStats(
                player_id="stranger", guild_id="other-guild", display_name="s",
                true_skill_mu=35.0, true_skill_sigma=1.0, games_won=ESTABLISHED, games_lost=0))
            await session.commit()
        assert await _player_server_rank("home", GUILD) == (1, 1)

    @pytest.mark.asyncio
    async def test_unrated_player_has_no_rank(self, test_db):
        await seed(("someone", 27.0, ESTABLISHED))
        assert await _player_server_rank("nobody", GUILD) == (None, None)


def _stats(**overrides):
    base = {
        "display_name": "P", "drafts_played": 12, "matches_won": 5, "matches_played": 9,
        "match_win_percentage": 55.0, "trophies_won": 1,
        "team_drafts_played": 4, "team_drafts_won": 2, "team_drafts_tied": 0,
        "team_draft_win_percentage": 50.0,
        "current_win_streak": 0, "longest_win_streak": 3,
        "current_perfect_streak": 0, "longest_perfect_streak": 1,
        "cube_stats": {},
    }
    base.update(overrides)
    return base


class _User:
    display_name = "P"


def _skill_field(embed):
    return next((f for f in embed.fields if f.name == "🎯 Skill Rating"), None)


class TestRankRendering:
    @pytest.mark.asyncio
    async def test_rank_is_shown_beside_the_rating(self):
        lifetime = _stats(skill_rating=1716, skill_provisional=False,
                          server_rank=3, server_rank_pool=185)
        embed = await create_stats_embed(_User(), _stats(), _stats(), lifetime)
        assert _skill_field(embed).value.startswith("1716 · **#3** of 185 ranked players")

    @pytest.mark.asyncio
    async def test_rating_renders_unchanged_without_a_rank(self):
        """Outside the top N the field must look exactly as it did before."""
        lifetime = _stats(skill_rating=1600, skill_provisional=False)
        embed = await create_stats_embed(_User(), _stats(), _stats(), lifetime)
        assert _skill_field(embed).value.startswith("1600\n")
        assert "#" not in _skill_field(embed).value.split("\n")[0]

    @pytest.mark.asyncio
    async def test_provisional_label_survives_alongside_a_rank(self):
        lifetime = _stats(skill_rating=1600, skill_provisional=True,
                          server_rank=7, server_rank_pool=40)
        embed = await create_stats_embed(_User(), _stats(), _stats(), lifetime)
        assert _skill_field(embed).value.startswith("1600 (provisional) · **#7** of 40 ranked players")
