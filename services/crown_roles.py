"""
Crown Roles Service

Manages Discord roles based on leaderboard #1 positions.
Players who hold #1 on multiple leaderboards get higher crown roles.
"""

import discord
from loguru import logger
from config import get_config
from services.leaderboard_service import get_crown_leaders, calculate_crown_counts
from helpers.display_names import get_display_name
from leaderboard_config import DEFAULT_CROWN_ROLE_NAMES as _DEFAULT_ROLE_NAMES

# Convert string keys from config to int keys for internal use
DEFAULT_CROWN_ROLE_NAMES = {int(k): v for k, v in _DEFAULT_ROLE_NAMES.items()}


async def update_crown_roles_for_guild(bot, guild_id: str):
    """
    Update crown roles for all players in a guild based on current leaderboard standings.

    This function:
    1. Checks if crown roles are enabled for the guild
    2. Gets the #1 player for each eligible leaderboard category
    3. Calculates how many crowns each player has
    4. Updates Discord roles accordingly (removing old, adding correct one)

    Args:
        bot: The Discord bot instance
        guild_id: The guild ID to update crown roles for
    """
    config = get_config(guild_id)
    crown_config = config.get("crown_roles", {})

    if not crown_config.get("enabled", False):
        return

    guild = bot.get_guild(int(guild_id))
    if not guild:
        logger.warning(f"Crown roles: Guild {guild_id} not found")
        return

    eligible_categories = crown_config.get("eligible_categories", [])
    timeframe = crown_config.get("timeframe", "lifetime")

    # Get role names from config (keys are strings in JSON)
    config_role_names = crown_config.get("role_names", {})
    # Convert string keys to int keys
    role_names = {}
    for key, value in config_role_names.items():
        try:
            role_names[int(key)] = value
        except (ValueError, TypeError):
            continue

    # Fall back to defaults if no valid role names
    if not role_names:
        role_names = DEFAULT_CROWN_ROLE_NAMES

    logger.info(f"Updating crown roles for guild {guild_id} with categories {eligible_categories}")

    # 1. Get current #1 players for each category
    leaders = await get_crown_leaders(guild_id, eligible_categories, timeframe)
    logger.debug(f"Crown leaders: {leaders}")

    # 2. Calculate crown counts per player
    crown_counts = calculate_crown_counts(leaders)
    logger.debug(f"Crown counts: {crown_counts}")

    # 3. Get all crown roles in the guild
    crown_roles = {}
    missing_roles = []
    for count, name in role_names.items():
        role = discord.utils.get(guild.roles, name=name)
        if role:
            crown_roles[count] = role
        else:
            missing_roles.append(name)

    if missing_roles:
        logger.warning(f"Crown roles not found in guild {guild_id}: {missing_roles}")

    if not crown_roles:
        logger.warning(f"No crown roles found in guild {guild_id}, skipping role sync")
        return

    # 4. Update roles for each member
    await sync_crown_roles(guild, crown_counts, crown_roles)

    logger.info(f"Finished updating crown roles for guild {guild_id}")


async def sync_crown_roles(guild: discord.Guild, crown_counts: dict, crown_roles: dict):
    """
    Sync crown roles for all members:
    - Remove crown roles from players who no longer qualify
    - Assign correct crown role to players who do qualify
    - Ensure each player has at most one crown role (their highest)

    Args:
        guild: The Discord guild
        crown_counts: dict mapping player_id -> number of crowns
        crown_roles: dict mapping crown count -> Discord role
    """
    all_crown_roles = set(crown_roles.values())

    if not all_crown_roles:
        return

    for member in guild.members:
        # Skip bot accounts
        if member.bot:
            continue

        player_id = str(member.id)
        expected_crowns = crown_counts.get(player_id, 0)
        expected_role = crown_roles.get(expected_crowns) if expected_crowns > 0 else None

        # Get member's current crown roles
        current_crown_roles = set(member.roles) & all_crown_roles

        # Remove any crown roles that aren't the expected one
        roles_to_remove = current_crown_roles - ({expected_role} if expected_role else set())
        for role in roles_to_remove:
            try:
                await member.remove_roles(role)
                logger.info(f"Removed {role.name} from {get_display_name(member, guild)}")
            except discord.Forbidden:
                logger.warning(f"Cannot remove role {role.name} from {get_display_name(member, guild)} - missing permissions")
            except discord.HTTPException as e:
                logger.error(f"HTTP error removing role {role.name} from {get_display_name(member, guild)}: {e}")

        # Add the expected role if they don't have it
        if expected_role and expected_role not in member.roles:
            try:
                await member.add_roles(expected_role)
                logger.info(f"Added {expected_role.name} to {get_display_name(member, guild)}")
            except discord.Forbidden:
                logger.warning(f"Cannot add role {expected_role.name} to {get_display_name(member, guild)} - missing permissions")
            except discord.HTTPException as e:
                logger.error(f"HTTP error adding role {expected_role.name} to {get_display_name(member, guild)}: {e}")
