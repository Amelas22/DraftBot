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


def build_guild_debt_embed(guild: discord.Guild, rows: list, include_description: bool = True) -> discord.Embed:
    """
    Build a guild debt summary embed from debt rows.

    Args:
        guild: The Discord guild
        rows: List of rows with player_id, counterparty_id, balance attributes
        include_description: Whether to include the settle button description

    Returns:
        Discord embed with debt summary
    """
    description = "Outstanding debts in this server. Click the button below to settle your debts." if include_description else "All outstanding debts (showing debtor perspective)"

    embed = discord.Embed(
        title="Guild Debt Summary",
        description=description,
        color=discord.Color.orange()
    )

    if rows:
        debt_lines = []
        total = 0
        for row in rows[:25]:
            debtor_name = get_member_name(guild, row.player_id)
            creditor_name = get_member_name(guild, row.counterparty_id)
            amount = abs(row.balance)
            debt_lines.append(f"{debtor_name} owes {creditor_name}: {amount} tix")
            total += amount

        embed.add_field(
            name=f"Outstanding Debts (Total: {total} tix)",
            value="\n".join(debt_lines),
            inline=False
        )

        if len(rows) > 25:
            embed.set_footer(text=f"Showing 25 of {len(rows)} debt relationships")
    else:
        embed.add_field(
            name="Outstanding Debts",
            value="No outstanding debts!",
            inline=False
        )

    return embed


def build_user_balance_embed(guild: discord.Guild, balances: dict) -> discord.Embed:
    """
    Build an embed showing a user's outstanding balances.

    Args:
        guild: The Discord guild
        balances: Dict mapping counterparty_id to balance amount

    Returns:
        Discord embed with balance breakdown
    """
    embed = discord.Embed(
        title="Your Outstanding Balances",
        color=discord.Color.gold()
    )

    you_owe_lines = []
    owed_to_you_lines = []

    for counterparty_id, balance in balances.items():
        name = get_member_name(guild, counterparty_id)

        if balance < 0:
            you_owe_lines.append(f"<@{counterparty_id}>: {abs(balance)} tix")
        else:
            owed_to_you_lines.append(f"<@{counterparty_id}>: {balance} tix")

    if you_owe_lines:
        embed.add_field(
            name="You Owe",
            value="\n".join(you_owe_lines),
            inline=False
        )

    if owed_to_you_lines:
        embed.add_field(
            name="Owed to You",
            value="\n".join(owed_to_you_lines),
            inline=False
        )

    return embed
