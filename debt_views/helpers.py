"""
Helper functions for debt-related views and commands.

This module contains shared utilities to reduce code duplication
across debt_commands.py and settle_views.py.
"""
import discord
import aiohttp
from sqlalchemy import select

from config import get_config
from database.db_session import db_session
from helpers.display_names import get_member_name, get_member_name_plain
from models.draft_session import DraftSession

# Network errors that are transient and should be logged but not re-raised
TRANSIENT_ERRORS = (
    aiohttp.ClientError,
    discord.errors.NotFound,
    discord.errors.HTTPException,
)


def format_entry_source(entry, draft_labels: dict | None = None) -> str:
    """
    Format a debt ledger entry's source for display.

    Args:
        entry: A DebtLedger model instance
        draft_labels: Optional {session_id: label} from describe_draft_sources;
            draft entries fall back to the raw "Draft #<session_id>" form when
            no label is available.

    Returns:
        Formatted string like "[LSVCube · Jul 29](jump-url)" or "Settlement"
    """
    if entry.source_type == 'draft':
        if draft_labels and entry.source_id in draft_labels:
            return draft_labels[entry.source_id]
        return f"Draft #{entry.source_id}"
    elif entry.source_type == 'settlement':
        return "Settlement"
    elif entry.source_type == 'transfer':
        return "Transfer"
    else:
        return entry.source_type.title()


def _draft_label(cube, when, guild_id=None, channel_id=None, message_id=None) -> str:
    """"[LSVCube · Jul 29](jump-url)" — or the same text unlinked when the
    draft has no surviving victory message to jump to."""
    text = cube or "Draft"
    if when:
        text += f" · {when.strftime('%b %d')}"
    if guild_id and channel_id and message_id:
        return f"[{text}](https://discord.com/channels/{guild_id}/{channel_id}/{message_id})"
    return text


async def describe_draft_sources(guild: discord.Guild, entries) -> dict[str, str]:
    """
    Readable labels for the draft entries among `entries`:
    {session_id: "[<cube> · <date>](link)"}.

    The link targets the draft's victory post in the guild's results channel —
    the one draft message that survives channel cleanup (team/draft-chat
    messages are deleted after a draft). Drafts without a victory message get
    unlinked text; session ids with no surviving DraftSession row are omitted
    so format_entry_source falls back to the legacy "Draft #id" form.
    """
    session_ids = {e.source_id for e in entries if e.source_type == "draft"}
    if not session_ids:
        return {}

    async with db_session() as session:
        rows = (await session.execute(
            select(DraftSession).where(DraftSession.session_id.in_(session_ids))
        )).scalars().all()

    results_channel_name = get_config(guild.id).get("channels", {}).get("draft_results")
    results_channel = (
        discord.utils.get(guild.text_channels, name=results_channel_name)
        if results_channel_name else None
    )

    return {
        ds.session_id: _draft_label(
            ds.cube,
            ds.teams_start_time or ds.draft_start_time,
            guild_id=guild.id,
            channel_id=results_channel.id if results_channel else None,
            message_id=ds.victory_message_id_results_channel,
        )
        for ds in rows
    }


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


_MEDALS = ("🥇", "🥈", "🥉")


def _build_most_outstanding_field(guild, top_creditors):
    """Return (name, value) for the 🏆 Most Outstanding leaderboard field, or None
    when nobody is involved in outstanding debts. Ranks by the number of debt
    relationships each player is involved in (owed + owing)."""
    if not top_creditors:
        return None
    lines = []
    for rank, (player_id, count) in enumerate(top_creditors):
        medal = _MEDALS[rank] if rank < len(_MEDALS) else f"{rank + 1}."
        name = get_member_name(guild, player_id)
        unit = "debt" if count == 1 else "debts"
        lines.append(f"{medal} {name} — {count} {unit}")
    return "🏆 Most Outstanding", "\n".join(lines)


def build_guild_debt_embed_pages(guild: discord.Guild, rows: list, per_page: int = 10, include_description: bool = True, top_creditors: list = None) -> list:
    """
    Build a list of paginated guild debt summary embeds.

    Args:
        guild: The Discord guild
        rows: List of rows with player_id, counterparty_id, balance attributes
        per_page: Number of debt lines per page
        include_description: Whether to include the settle button description

    Returns:
        List of Discord embeds, one per page
    """
    description = "Outstanding debts in this server. Click the button below to settle your debts." if include_description else "All outstanding debts (showing debtor perspective)"

    if not rows:
        embed = discord.Embed(
            title="Guild Debt Summary",
            description=description,
            color=discord.Color.orange()
        )
        embed.add_field(
            name="Outstanding Debts",
            value="No outstanding debts!",
            inline=False
        )
        return [embed]

    # Calculate total across all rows
    total = sum(abs(row.balance) for row in rows)
    total_pages = (len(rows) + per_page - 1) // per_page
    pages = []

    for page_num in range(total_pages):
        start = page_num * per_page
        page_rows = rows[start:start + per_page]

        embed = discord.Embed(
            title="Guild Debt Summary",
            description=description,
            color=discord.Color.orange()
        )

        leaderboard = _build_most_outstanding_field(guild, top_creditors)
        if leaderboard:
            embed.add_field(name=leaderboard[0], value=leaderboard[1], inline=False)

        debt_lines = []
        for row in page_rows:
            debtor_name = get_member_name(guild, row.player_id)
            creditor_name = get_member_name(guild, row.counterparty_id)
            amount = abs(row.balance)
            debt_lines.append(f"{debtor_name} owes {creditor_name}: {amount} tix")

        embed.add_field(
            name=f"Outstanding Debts (Total: {total} tix)",
            value="\n".join(debt_lines),
            inline=False
        )

        if total_pages > 1:
            embed.set_footer(text=f"Page {page_num + 1} of {total_pages} ({len(rows)} total debts)")

        pages.append(embed)

    return pages


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
