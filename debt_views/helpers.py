"""
Helper functions for debt-related views and commands.

This module contains shared utilities to reduce code duplication
across debt_commands.py and settle_views.py.
"""
import discord

from helpers.money_gate import add_wallet_howto
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


def format_card_quantity(card_name: str, qty: int) -> str:
    """'4x Lightning Bolt' / 'Ragavan' — THE quantity+name display form."""
    return f"{qty}x {card_name}" if qty > 1 else card_name


def card_count_label(count: int) -> str:
    """'1 card' / '3 cards' — THE open-position count display form."""
    return f"{count} card{'s' if count != 1 else ''}"


def build_debt_pair_lines(rows: list, card_pairs: dict, name_of) -> tuple[list, int]:
    """One display line per debtor→creditor pair, tix and cards combined.

    rows carry the tix pairs (player_id owes counterparty_id abs(balance));
    card_pairs is {(debtor_id, creditor_id): open card position count} from
    get_guild_card_pair_counts. Tix lines gain a ' · N cards' suffix when the
    same pair also has open cards; pairs with ONLY cards get their own line.
    Returns (lines, total_tix).
    """
    card_pairs = dict(card_pairs or {})
    lines = []
    total = 0
    for row in rows:
        amount = abs(row.balance)
        total += amount
        line = f"{name_of(row.player_id)} owes {name_of(row.counterparty_id)}: {amount} tix"
        cards = card_pairs.pop((row.player_id, row.counterparty_id), 0)
        if cards:
            line += f" · {card_count_label(cards)}"
        lines.append(line)
    for (debtor_id, creditor_id), cards in card_pairs.items():
        lines.append(
            f"{name_of(debtor_id)} owes {name_of(creditor_id)}: {card_count_label(cards)}")
    return lines, total


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


async def build_debt_summary_pages(guild: discord.Guild, include_description: bool = True) -> list:
    """THE debt summary panel builder: owns all data fetching (tix pair rows,
    open card pair counts, the leaderboard) so every render path — /debts-post,
    /debts-admin, sticky refresh, pagination, background updates — produces
    identical content by construction. Render paths must not fetch panel data
    themselves."""
    from services.debt_service import (
        get_guild_debt_rows,
        get_guild_card_pair_counts,
        get_most_outstanding_creditors,
    )
    import asyncio
    guild_id = str(guild.id)
    rows, top_creditors, card_pairs = await asyncio.gather(
        get_guild_debt_rows(guild_id),
        get_most_outstanding_creditors(guild_id),
        get_guild_card_pair_counts(guild_id),
    )
    return build_guild_debt_embed_pages(
        guild, rows, include_description=include_description,
        top_creditors=top_creditors, card_pairs=card_pairs)


def build_guild_debt_embed_pages(guild: discord.Guild, rows: list, per_page: int = 10, include_description: bool = True, top_creditors: list = None, card_pairs: dict = None) -> list:
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

    all_lines, total = build_debt_pair_lines(
        rows, card_pairs, lambda pid: get_member_name(guild, pid))

    if not all_lines:
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

    total_pages = (len(all_lines) + per_page - 1) // per_page
    pages = []

    for page_num in range(total_pages):
        start = page_num * per_page
        page_lines = all_lines[start:start + per_page]

        embed = discord.Embed(
            title="Guild Debt Summary",
            description=description,
            color=discord.Color.orange()
        )

        leaderboard = _build_most_outstanding_field(guild, top_creditors)
        if leaderboard:
            embed.add_field(name=leaderboard[0], value=leaderboard[1], inline=False)

        embed.add_field(
            name=f"Outstanding Debts (Total: {total} tix)",
            value="\n".join(page_lines),
            inline=False
        )

        if total_pages > 1:
            embed.set_footer(text=f"Page {page_num + 1} of {total_pages} ({len(all_lines)} total debts)")

        pages.append(embed)

    # Last page only: the block is identical on every page, and repeating it through a
    # long paginated list pushes the debts themselves off the screen.
    # Only when tix are actually owed: build_debt_pair_lines also emits card-only
    # pairs, which contribute lines but no tix, and the wallet does not settle those.
    if total:
        add_wallet_howto(pages[-1], guild.id)

    return pages


def build_user_balance_embed(guild: discord.Guild, balances: dict,
                             positions_by_cp: dict = None) -> discord.Embed:
    """
    Build an embed showing a user's outstanding balances.

    Args:
        guild: The Discord guild
        balances: Dict mapping counterparty_id to balance amount
        positions_by_cp: Optional {counterparty_id: [card position, ...]}
            from group_positions_by_counterparty; card counterparties are
            listed alongside tix ones, tix-only callers pass nothing.

    Returns:
        Discord embed with balance breakdown
    """
    embed = discord.Embed(
        title="Your Outstanding Balances",
        color=discord.Color.gold()
    )
    positions_by_cp = positions_by_cp or {}

    def card_suffix(counterparty_id):
        count = len(positions_by_cp.get(counterparty_id, []))
        return f" · {card_count_label(count)}" if count else ""

    you_owe_lines = []
    owed_to_you_lines = []

    for counterparty_id, balance in balances.items():
        name = get_member_name(guild, counterparty_id)

        if balance < 0:
            you_owe_lines.append(f"<@{counterparty_id}>: {abs(balance)} tix{card_suffix(counterparty_id)}")
        else:
            owed_to_you_lines.append(f"<@{counterparty_id}>: {balance} tix{card_suffix(counterparty_id)}")

    cards_only_lines = [
        f"<@{cp}>: {card_count_label(len(positions))}"
        for cp, positions in positions_by_cp.items() if cp not in balances
    ]
    if cards_only_lines:
        embed.add_field(
            name="Open Card Loans",
            value="\n".join(cards_only_lines),
            inline=False
        )

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

    # Only when they actually owe: the block explains settling a debt, which is noise
    # for a player who is purely owed money.
    if you_owe_lines:
        add_wallet_howto(embed, guild.id)

    return embed
