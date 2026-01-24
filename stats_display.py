"""
High-level statistics display functions.

This module combines player_stats and legacy_stats to create formatted
displays for Discord. It sits at the top of the dependency chain.
"""
import discord
from player_stats import create_stats_embed
from legacy_stats import get_player_statistics_with_legacy


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

    # Create and return the embed
    embed = await create_stats_embed(user, stats_weekly, stats_monthly, stats_lifetime)
    return embed
