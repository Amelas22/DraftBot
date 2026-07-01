"""
High-level statistics display functions.

This module combines player_stats and legacy_stats to create formatted
displays for Discord. It sits at the top of the dependency chain.
"""
import discord
from player_stats import create_stats_embed
from legacy_stats import get_player_statistics_with_legacy
from sqlalchemy import select
from database.db_session import AsyncSessionLocal
from models.player import PlayerStats
from helpers.skill import is_established, skill_rating


async def _player_skill_rating(player_id, guild_id, drafts_played):
    """Return (scaled_rating, provisional) for a player, or (None, None) if the
    player has no stored rating. Provisional is True until they are established."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PlayerStats.true_skill_mu, PlayerStats.true_skill_sigma).where(
                PlayerStats.player_id == str(player_id),
                PlayerStats.guild_id == str(guild_id),
            )
        )
        row = result.first()
    if row is None or row[0] is None or row[1] is None:
        return None, None
    mu, sigma = row
    return skill_rating(mu, sigma), not is_established(drafts_played)


async def get_stats_embed_for_player(
    bot,
    player_id: str,
    guild_id: str,
    display_name: str = None
) -> discord.Embed:
    """
    Get stats embed for any player by their ID.

    This is a DRY helper function that can be used by both the regular /stats command
    and the admin /admin-stats command.

    Args:
        bot: Discord bot instance (for fetching user info)
        player_id: Discord user ID as string
        guild_id: Guild ID for scoping stats
        display_name: Optional display name (will fetch from Discord if None)

    Returns:
        Discord embed with player statistics across weekly, monthly, and lifetime timeframes
    """
    # Get user object for embed (needed for avatar, etc.)
    try:
        user = await bot.fetch_user(int(player_id))
    except Exception:
        # Create a mock user object if fetch fails (e.g., user left Discord)
        class MockUser:
            def __init__(self, user_id, name):
                self.id = int(user_id)
                self.display_name = name or "Unknown Player"
                self.avatar = None
        user = MockUser(player_id, display_name)

    # Get stats for all 3 timeframes
    stats_weekly = await get_player_statistics_with_legacy(player_id, 'week', display_name, guild_id)
    stats_monthly = await get_player_statistics_with_legacy(player_id, 'month', display_name, guild_id)
    stats_lifetime = await get_player_statistics_with_legacy(player_id, None, display_name, guild_id)

    # Skill rating from stored TrueSkill μ/σ, gated on lifetime drafts played.
    rating, provisional = await _player_skill_rating(
        player_id, guild_id, stats_lifetime['drafts_played']
    )
    stats_lifetime['skill_rating'] = rating
    stats_lifetime['skill_provisional'] = provisional

    # Create and return the embed
    embed = await create_stats_embed(user, stats_weekly, stats_monthly, stats_lifetime)
    return embed
