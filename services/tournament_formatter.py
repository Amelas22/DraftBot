"""Rendering and live-updating of the tournament standings message.

`create_standings_embed` is the single renderer shared by `/tournament status`
and the auto-updating message. `update_standings_message` edits the stored
message in place on every result change, mirroring
`utils.update_leaderboards_for_guild`.
"""
import discord
from loguru import logger

from database.db_session import db_session
from models.tournament import Tournament
from services.tournament_escrow_service import describe_structure
from services.tournament_service import (
    get_standings_data,
    get_tournament_id_for_match,
)


def create_standings_embed(tournament, participants):
    """Build the standings embed for a tournament (pure)."""
    embed = discord.Embed(
        title=f"🏆 {tournament.name} — Standings",
        description=(
            f"**Status:** {tournament.status.title()}\n"
            f"**Round:** {tournament.current_round}/{tournament.total_rounds}"
        ),
        color=discord.Color.gold(),
    )
    if participants:
        rows = "\n".join(
            f"{i}. **{p.team_name}** — {p.points} pts "
            f"({p.match_wins}-{p.match_losses}-{p.match_draws})"
            for i, p in enumerate(participants, start=1)
        )
        embed.add_field(name="Standings", value=rows, inline=False)
    else:
        embed.add_field(name="Standings", value="No teams registered yet.", inline=False)
    return embed


def create_registration_embed(tournament, participants, pot=0, deficits=None,
                              closed=False):
    """Build the registration board (pure): who is in, and for a paid tournament who
    has actually paid. ``deficits`` maps participant id -> tix still needed."""
    deficits = deficits or {}
    fee = tournament.entry_fee or 0
    phase = "Registration closed" if closed else "Registration open"
    title = f"🏆 {tournament.name} — {phase}"
    desc = ""
    if fee > 0:
        desc = f"**Entry fee:** {fee} tix/team · **Prize pool:** {pot} tix\n"
        desc += f"**Payout:** {describe_structure(tournament.payout_structure or 'winner_take_all')}"
    embed = discord.Embed(title=title, description=desc,
                          color=discord.Color.gold())

    if not participants:
        embed.add_field(name="Teams (0)",
                        value="No teams yet — register with `/tournament register`.",
                        inline=False)
        return embed

    lines = []
    for i, p in enumerate(participants, start=1):
        paid = p.status == "paid"
        mark = "✅" if paid or fee == 0 else "⏳"
        lines.append(f"{i}. {mark} **{p.team_name}** — captain <@{p.captain_user_id}>")
        short = deficits.get(p.id, 0)
        if fee > 0 and not paid and not closed and short > 0:
            lines.append(f"     needs {short} more tix — `/wallet deposit {short}`")
    if fee > 0:
        paid_n = sum(1 for p in participants if p.status == "paid")
        label = f"Teams ({paid_n}/{len(participants)} paid)"
    else:
        label = f"Teams ({len(participants)})"
    embed.add_field(name=label, value="\n".join(lines), inline=False)
    return embed


async def update_standings_message(bot, tournament_id):
    """Edit the tournament's standings message in place. No-op if not posted."""
    async with db_session() as session:
        tournament = await session.get(Tournament, tournament_id)
        if tournament is None or not tournament.standings_message_id:
            return
        participants = await get_standings_data(session, tournament_id)
        embed = create_standings_embed(tournament, participants)
        channel_id = int(tournament.standings_channel_id)
        message_id = int(tournament.standings_message_id)

    channel = bot.get_channel(channel_id)
    if channel is None:
        logger.warning(f"Standings channel {channel_id} not found for tournament {tournament_id}")
        return
    try:
        message = await channel.fetch_message(message_id)
        await message.edit(embed=embed)
    except discord.NotFound:
        logger.warning(f"Standings message {message_id} gone for tournament {tournament_id}")
    except discord.HTTPException as e:
        logger.error(f"Failed to update standings message for tournament {tournament_id}: {e}")


async def update_standings_message_for_match(bot, match_id):
    """Refresh the standings message for whichever tournament owns this match."""
    async with db_session() as session:
        tournament_id = await get_tournament_id_for_match(session, match_id)
    if tournament_id is not None:
        await update_standings_message(bot, tournament_id)
