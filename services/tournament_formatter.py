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


def _add_how_to_join(embed, fee, closed):
    """Spell out how to sign up, for as long as sign-ups are open.

    This used to appear only on an empty board, so it vanished the moment the first
    team registered — exactly when newcomers start reading the board. A paid entry
    also isn't one step: the fee is paid from the captain's wallet, so someone who has
    never deposited needs the MTGO link and the deposit named too, not just the
    register command."""
    if closed:
        return
    if fee > 0:
        value = (
            f"1. `/link_mtgo <your MTGO username>` — once, so the bot can trade with you\n"
            f"2. `/tournament register <team name>` — holds your spot\n"
            f"3. `/wallet deposit {fee}` — your spot completes when the tix land"
        )
    else:
        value = "`/tournament register <team name>`"
    embed.add_field(name="How to join", value=value, inline=False)


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
        embed.add_field(name="Teams (0)", value="No teams yet.", inline=False)
        _add_how_to_join(embed, fee, closed)
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

    # Discord caps a single embed field's value at 1024 characters. A paid roster's
    # deficit lines push this well past that at ~10+ pending teams, and Discord then
    # rejects the whole edit (the board freezes on a stale roster). Pack lines into
    # as many fields as needed, each under the cap.
    _FIELD_LIMIT = 1024
    chunks = []
    current = []
    current_len = 0
    for line in lines:
        added_len = len(line) + (1 if current else 0)  # + newline joiner
        if current and current_len + added_len > _FIELD_LIMIT:
            chunks.append(current)
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += added_len
    if current:
        chunks.append(current)

    for i, chunk in enumerate(chunks):
        name = label if i == 0 else "Teams (cont.)"
        embed.add_field(name=name, value="\n".join(chunk), inline=False)
    _add_how_to_join(embed, fee, closed)
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


async def _board_state(session, tournament):
    """(participants, pot, deficits) for a tournament's board."""
    from services import wallet_service
    from services.tournament_service import list_participants

    participants = await list_participants(session, tournament.id)
    fee = tournament.entry_fee or 0
    if fee <= 0:
        return participants, 0, {}
    guild_id = str(tournament.guild_id)
    pot = await wallet_service.balance_in(
        session, guild_id, wallet_service.prize_wallet_id(tournament.id))
    pending = [p for p in participants if p.status != "paid"]
    balances = await wallet_service.balances_for(
        guild_id, [p.captain_user_id for p in pending])
    deficits = {p.id: max(fee - balances.get(p.captain_user_id, 0), 0) for p in pending}
    return participants, pot, deficits


async def refresh_boards(bot, tournament_ids, closed=False):
    """Refresh several boards, guarded. The board is a view: a Discord failure on one
    logs and the rest still update. Every caller that reacts to a change goes through
    here rather than hand-rolling the try/except."""
    for t_id in set(tournament_ids):
        try:
            await update_registration_board(bot, t_id, closed=closed)
        except Exception as e:
            logger.warning(f"board refresh failed for {t_id}: {e}")


async def update_registration_board(bot, tournament_id, closed=False):
    """Edit the registration board in place. No-op if it was never posted; a board that
    has been deleted clears its ids so it stops being retried."""
    async with db_session() as session:
        tournament = await session.get(Tournament, tournament_id)
        if tournament is None or not tournament.board_message_id:
            return
        participants, pot, deficits = await _board_state(session, tournament)
        embed = create_registration_embed(tournament, participants, pot, deficits, closed)
        channel_id = int(tournament.board_channel_id)
        message_id = int(tournament.board_message_id)

    channel = bot.get_channel(channel_id)
    if channel is None:
        logger.warning(f"Board channel {channel_id} not found for tournament {tournament_id}")
        return
    try:
        message = await channel.fetch_message(message_id)
        await message.edit(embed=embed)
    except discord.NotFound:
        logger.warning(f"Board message gone for tournament {tournament_id}; clearing ids")
        async with db_session() as session:
            t = await session.get(Tournament, tournament_id)
            if t is not None:
                t.board_channel_id = None
                t.board_message_id = None
    except discord.HTTPException as e:
        logger.warning(f"Board edit failed for tournament {tournament_id}: {e}")


async def post_registration_board(channel, tournament_id):
    """Post the board for a freshly created tournament and remember it. Returns the
    message, or None if posting failed (the tournament is unaffected either way)."""
    async with db_session() as session:
        tournament = await session.get(Tournament, tournament_id)
        if tournament is None:
            return None
        participants, pot, deficits = await _board_state(session, tournament)
        embed = create_registration_embed(tournament, participants, pot, deficits)
    try:
        message = await channel.send(embed=embed)
    except discord.HTTPException as e:
        logger.warning(f"Could not post registration board for {tournament_id}: {e}")
        return None
    async with db_session() as session:
        t = await session.get(Tournament, tournament_id)
        if t is not None:
            t.board_channel_id = str(message.channel.id)
            t.board_message_id = str(message.id)
    return message


async def update_standings_message_for_match(bot, match_id):
    """Refresh the standings message for whichever tournament owns this match."""
    async with db_session() as session:
        tournament_id = await get_tournament_id_for_match(session, match_id)
    if tournament_id is not None:
        await update_standings_message(bot, tournament_id)
