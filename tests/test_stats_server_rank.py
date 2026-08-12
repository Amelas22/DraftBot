"""Server rank on the stats page: a player's standing by skill rating among
the guild's established players, rendered only inside the top N."""
import pytest
from conftest import StubUser, embed_field, stats_dict

from database.db_session import AsyncSessionLocal
from models.player import PlayerStats
from player_stats import MAX_SERVER_RANK_DISPLAY, create_stats_embed
from stats_display import _player_skill_standing

GUILD = "g"
ESTABLISHED = 25  # >= helpers.skill.ESTABLISHED_GAMES (20)


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


async def rank_of(player_id):
    """(rank, pool_size) for a player, dropping the rating half."""
    _, _, rank, pool = await _player_skill_standing(player_id, GUILD)
    return rank, pool


class TestServerRank:
    @pytest.mark.asyncio
    async def test_highest_rating_is_rank_one(self, test_db):
        await seed(("top", 30.0, ESTABLISHED), ("mid", 27.0, ESTABLISHED), ("low", 24.0, ESTABLISHED))
        assert await rank_of("top") == (1, 3)

    @pytest.mark.asyncio
    async def test_rank_counts_only_players_rated_higher(self, test_db):
        await seed(("top", 30.0, ESTABLISHED), ("mid", 27.0, ESTABLISHED), ("low", 24.0, ESTABLISHED))
        assert await rank_of("mid") == (2, 3)
        assert await rank_of("low") == (3, 3)

    @pytest.mark.asyncio
    async def test_ties_share_the_better_rank(self, test_db):
        # Competition ranking: two players tied for 1st, the next is 3rd.
        await seed(("a", 30.0, ESTABLISHED), ("b", 30.0, ESTABLISHED), ("c", 26.0, ESTABLISHED))
        assert await rank_of("a") == (1, 3)
        assert await rank_of("b") == (1, 3)
        assert await rank_of("c") == (3, 3)

    @pytest.mark.asyncio
    async def test_provisional_players_are_neither_ranked_nor_counted(self, test_db):
        # The provisional player's raw mu is the highest in the guild, but a
        # short record can't take a top slot from a proven player.
        await seed(("hotshot", 40.0, 5), ("steady", 30.0, ESTABLISHED))
        assert await rank_of("hotshot") == (None, None)
        assert await rank_of("steady") == (1, 1)

    @pytest.mark.asyncio
    async def test_deep_ranks_are_still_reported(self, test_db):
        """The helper ranks everyone established; how deep to *print* is the
        embed builder's call (MAX_SERVER_RANK_DISPLAY)."""
        size = MAX_SERVER_RANK_DISPLAY + 2
        await seed(*[(f"p{i}", 30.0 - i * 0.1, ESTABLISHED) for i in range(size)])
        assert await rank_of(f"p{size - 1}") == (size, size)

    @pytest.mark.asyncio
    async def test_other_guilds_do_not_affect_the_rank(self, test_db):
        await seed(("home", 27.0, ESTABLISHED))
        async with AsyncSessionLocal() as session:
            session.add(PlayerStats(
                player_id="stranger", guild_id="other-guild", display_name="s",
                true_skill_mu=35.0, true_skill_sigma=1.0, games_won=ESTABLISHED, games_lost=0))
            await session.commit()
        assert await rank_of("home") == (1, 1)

    @pytest.mark.asyncio
    async def test_unrated_player_has_no_rank(self, test_db):
        await seed(("someone", 27.0, ESTABLISHED))
        assert await rank_of("nobody") == (None, None)


def skill_field(embed):
    return embed_field(embed, "🎯 Skill Rating")


class TestRankRendering:
    @pytest.mark.asyncio
    async def test_rank_is_shown_beside_the_rating(self):
        lifetime = stats_dict(skill_rating=1716, skill_provisional=False,
                              server_rank=3, server_rank_pool=185)
        embed = await create_stats_embed(StubUser(), stats_dict(), stats_dict(), lifetime)
        assert skill_field(embed).value.startswith("1716 · **#3** of 185 ranked players")

    @pytest.mark.asyncio
    async def test_rating_renders_unchanged_without_a_rank(self):
        lifetime = stats_dict(skill_rating=1600, skill_provisional=False)
        embed = await create_stats_embed(StubUser(), stats_dict(), stats_dict(), lifetime)
        assert skill_field(embed).value.startswith("1600\n")

    @pytest.mark.asyncio
    async def test_rank_past_the_display_limit_is_not_printed(self):
        """A real rank the page declines to show still renders as a bare rating."""
        lifetime = stats_dict(skill_rating=1600, skill_provisional=False,
                              server_rank=MAX_SERVER_RANK_DISPLAY + 1, server_rank_pool=185)
        embed = await create_stats_embed(StubUser(), stats_dict(), stats_dict(), lifetime)
        assert skill_field(embed).value.startswith("1600\n")

    @pytest.mark.asyncio
    async def test_last_shown_rank_still_prints(self):
        lifetime = stats_dict(skill_rating=1600, skill_provisional=False,
                              server_rank=MAX_SERVER_RANK_DISPLAY, server_rank_pool=185)
        embed = await create_stats_embed(StubUser(), stats_dict(), stats_dict(), lifetime)
        assert f"**#{MAX_SERVER_RANK_DISPLAY}** of 185" in skill_field(embed).value
