"""
Centralized display name handling for Discord members.

This module provides consistent methods for retrieving and formatting
display names throughout the bot. It serves as a single point of control
for display name logic, including crown icons for leaderboard leaders.
"""

import discord
from typing import Optional
from leaderboard_config import CROWN_ICONS


def get_crown_icon(member: discord.Member, guild: Optional[discord.Guild]) -> str:
    """
    Get crown icon for a member based on their crown roles.

    Checks the member's roles against the configured crown role names
    and returns the appropriate icon for their highest crown level.

    Args:
        member: The Discord member object
        guild: The guild to get config from (optional)

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


def get_display_name(member: discord.Member, guild: Optional[discord.Guild] = None) -> str:
    """
    Get display name for a Discord member, with ring bearer and/or crown icon if applicable.

    Args:
        member: The Discord member object
        guild: The guild (used for icon lookup)

    Returns:
        The member's display name (with icons if they have any),
        or "Unknown User" if member is None
    """
    if not member:
        return "Unknown User"

    icons = []

    # Check for ring bearer icon first
    if guild:
        from config import get_config
        config = get_config(guild.id)
        rb_config = config.get("ring_bearer", {})

        if rb_config.get("enabled", False):
            role_name = rb_config.get("role_name", "ring bearer")
            rb_role = discord.utils.get(member.roles, name=role_name)
            if rb_role:
                rb_icon = rb_config.get("icon", "💎")
                icons.append(rb_icon)

    # Then check for crown icon
    crown_icon = get_crown_icon(member, guild) if guild else ""
    if crown_icon:
        icons.append(crown_icon)

    # Escape markdown characters in display name to prevent formatting issues
    escaped_name = discord.utils.escape_markdown(member.display_name)

    if icons:
        return f"{' '.join(icons)} {escaped_name}"
    return escaped_name


def get_display_name_by_id(user_id: str, guild: discord.Guild, fallback: str = "Unknown User") -> str:
    """
    Get display name by user ID, looking up the member in the guild.

    Args:
        user_id: The Discord user ID as a string
        guild: The guild to look up the member in
        fallback: Value to return if member not found

    Returns:
        The member's display name (escaped), or escaped fallback if not found
    """
    if not guild or not user_id:
        return discord.utils.escape_markdown(fallback)

    try:
        member = guild.get_member(int(user_id))
        if not member:
            # Escape the fallback name since it won't go through get_display_name()
            return discord.utils.escape_markdown(fallback)
        return get_display_name(member, guild)
    except (ValueError, TypeError):
        return discord.utils.escape_markdown(fallback)


def format_display_name(display_name: str, user_id: Optional[str] = None, guild: Optional[discord.Guild] = None) -> str:
    """
    Format an existing display name string.

    Escapes markdown characters to prevent formatting issues in Discord embeds.
    Provides a hook for future enhancements (like adding crown icons based on user roles).

    Args:
        display_name: The display name to format
        user_id: Optional user ID for looking up role-based enhancements
        guild: Optional guild for looking up member roles

    Returns:
        The formatted display name with escaped markdown characters
    """
    return discord.utils.escape_markdown(display_name)
