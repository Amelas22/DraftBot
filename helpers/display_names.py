"""
Centralized display name handling for Discord members.

This module provides consistent methods for retrieving and formatting
display names throughout the bot. It serves as a single point of control
for display name logic, including crown icons for leaderboard leaders.
"""

import discord
from leaderboard_config import CROWN_ICONS


def get_crown_icon(member: discord.Member, guild: discord.Guild) -> str:
    """
    Get crown icon for a member based on their crown roles.

    Checks the member's roles against the configured crown role names
    and returns the appropriate icon for their highest crown level.

    Args:
        member: The Discord member object
        guild: The guild to get config from

    Returns:
        The crown icon string, or empty string if no crown role
    """
    if not member or not guild:
        return ""

    # Import here to avoid circular imports
    from config import get_config

    config = get_config(guild.id)
    crown_config = config.get("crown_roles", {})

    if not crown_config.get("enabled", False):
        return ""

    role_names = crown_config.get("role_names", {})

    # Check roles from highest to lowest crown count
    for count in sorted(role_names.keys(), key=lambda x: int(x), reverse=True):
        role_name = role_names[count]
        if discord.utils.get(member.roles, name=role_name):
            return CROWN_ICONS.get(int(count), "")

    return ""


def get_display_name(member: discord.Member, guild: discord.Guild = None) -> str:
    """
    Get display name for a Discord member, with crown icon if applicable.

    Args:
        member: The Discord member object
        guild: The guild (used for crown icon lookup)

    Returns:
        The member's display name (with crown icon prefix if they have one),
        or "Unknown User" if member is None
    """
    if not member:
        return "Unknown User"

    icon = get_crown_icon(member, guild) if guild else ""
    if icon:
        return f"{icon} {member.display_name}"
    return member.display_name


def get_display_name_by_id(user_id: str, guild: discord.Guild, fallback: str = "Unknown User") -> str:
    """
    Get display name by user ID, looking up the member in the guild.

    Args:
        user_id: The Discord user ID as a string
        guild: The guild to look up the member in
        fallback: Value to return if member not found

    Returns:
        The member's display name, or fallback if not found
    """
    if not guild or not user_id:
        return fallback

    try:
        member = guild.get_member(int(user_id))
        if not member:
            return fallback
        return get_display_name(member, guild)
    except (ValueError, TypeError):
        return fallback


def format_display_name(display_name: str, user_id: str = None, guild: discord.Guild = None) -> str:
    """
    Format an existing display name string.

    Currently returns as-is, but provides a hook for future enhancements
    (like adding crown icons based on user roles).

    Args:
        display_name: The display name to format
        user_id: Optional user ID for looking up role-based enhancements
        guild: Optional guild for looking up member roles

    Returns:
        The formatted display name
    """
    return display_name
