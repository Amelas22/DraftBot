"""
Helper functions for debt-related views and commands.

This module contains shared utilities to reduce code duplication
across debt_commands.py and settle_views.py.
"""
import discord
import aiohttp

# Network errors that are transient and should be logged but not re-raised
TRANSIENT_ERRORS = (
    aiohttp.ClientError,
    discord.errors.NotFound,
    discord.errors.HTTPException,
)


def get_member_name(guild: discord.Guild, user_id: str) -> str:
    """
    Resolve a user ID to their display name in a guild.

    Args:
        guild: The Discord guild to look up the member in
        user_id: The user's ID as a string

    Returns:
        The member's display name, or "User {id}" as fallback
    """
    try:
        member = guild.get_member(int(user_id))
        return member.display_name if member else f"User {user_id}"
    except (ValueError, AttributeError):
        return f"User {user_id}"


def format_entry_source(entry) -> str:
    """
    Format a debt ledger entry's source for display.

    Args:
        entry: A DebtLedger model instance

    Returns:
        Formatted string like "Draft #123" or "Settlement"
    """
    if entry.source_type == 'draft':
        return f"Draft #{entry.source_id}"
    elif entry.source_type == 'settlement':
        return "Settlement"
    else:
        return entry.source_type.title()
