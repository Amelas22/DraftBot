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
