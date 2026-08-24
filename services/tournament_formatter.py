"""Rendering and live-updating of the tournament standings message.

`create_standings_embed` is the single renderer shared by `/tournament status`
and the auto-updating message. `update_standings_message` edits the stored
message in place on every result change, mirroring
`utils.update_leaderboards_for_guild`.
"""
import discord
from loguru import logger

from database.db_session import db_session
from models.tournament import STAGE_PLAYOFF, STAGE_SWISS, Tournament
from services.tournament_escrow_service import describe_structure
from services.tournament_service import (
    current_round_stage,
    get_standings_data,
    get_tournament_id_for_match,
)


def round_label(total_rounds, round_number, stage, swiss_noun="Round"):
    """How one round is named, wherever a round is named.

    Bracket rounds are numbered past total_rounds -- the first one of a 3-round
    swiss is round 4 -- so naming them as swiss rounds calls the semifinal
    "Week 4". Three display sites did that arithmetic differently (or not at
    all); this is the one that answers now.

    ``swiss_noun`` is the word a site already uses for a swiss round: "Week" in
    the pairings channel, "Round" on a match's control message. Only the swiss
    wording differs between sites -- the playoff form is identical everywhere,
    which is the half that was wrong.
    """
    if stage == STAGE_PLAYOFF:
        return f"Playoff round {round_number - total_rounds}"
    return f"{swiss_noun} {round_number}"


def _round_line(tournament, stage):
    """The "**Round:** …" line. A bracket round is named, not counted: the
    swiss "N of M" form reads "Round: 4/3" once the bracket starts."""
    if stage == STAGE_PLAYOFF:
        return (f"**Round:** "
                f"{round_label(tournament.total_rounds, tournament.current_round, stage)}")
    return f"**Round:** {tournament.current_round}/{tournament.total_rounds}"


# Discord caps a single embed field's value at 1024 characters, and rejects the WHOLE
# embed if any field is over — not just that field. Both the roster and the standings
# outgrow it well before a league this size, so both go through the same splitter.
_FIELD_LIMIT = 1024


def _add_chunked_field(embed, label, lines, cont_label=None):
    """Add ``lines`` as one field, or as many as the 1024-char cap requires.

    Splits BETWEEN lines, so it cannot rescue a single line that is itself over the
    cap — callers keep individual lines short. Continuation fields are named
    ``cont_label`` (default "<label> (cont.)") so the first field keeps the real
    heading and the rest read as overflow.
    """
    from utils import split_content_for_embed  # module-level would cycle via utils

    cont = cont_label or f"{label} (cont.)"
    for i, chunk in enumerate(split_content_for_embed(lines, max_length=_FIELD_LIMIT)):
        embed.add_field(name=label if i == 0 else cont, value=chunk, inline=False)


def create_standings_embed(tournament, participants, stage=STAGE_SWISS):
    """Build the standings embed for a tournament (pure).

    ``stage`` is the stage of the round it is on (see
    tournament_service.current_round_stage). It defaults to swiss for the
    read-only callers of a tournament that has none."""
    embed = discord.Embed(
        title=f"🏆 {tournament.name} — Standings",
        description=(
            f"**Status:** {tournament.status.title()}\n"
            f"{_round_line(tournament, stage)}"
        ),
        color=discord.Color.gold(),
    )
    if participants:
        rows = [
            f"{i}. **{p.team_name}** — {p.points} pts "
            f"({p.match_wins}-{p.match_losses}-{p.match_draws})"
            for i, p in enumerate(participants, start=1)
        ]
        _add_chunked_field(embed, "Standings", rows)
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
            f"3. `/wallet deposit {fee}` — your spot completes when the tix land\n"
            f"4. `/tournament add_teammate @player` — once per teammate"
        )
    else:
        value = (
            "1. `/tournament register <team name>`\n"
            "2. `/tournament add_teammate @player` — once per teammate"
        )
    embed.add_field(name="How to join", value=value, inline=False)


CAPTAIN_MARK = "👑"

# A mention renders as ~24 characters with its separator, and every member of a team
# shares one line. The field chunker below splits BETWEEN lines, so it cannot rescue a
# single line that is itself over Discord's 1024-char cap -- past that, Discord rejects
# the whole edit and the board freezes on a stale roster. Cap the names shown so one
# line stays well inside the limit even beside a 128-char team name.
MAX_MEMBERS_SHOWN = 20


def _team_line(index, participant, fee, members):
    """One roster row: rank, paid mark, team, then the full team inline.

    Members share the team's line rather than getting one each: at 25 teams a line
    per player would quadruple the row count and push the board through Discord's
    embed limits, and the roster reads as a team either way.
    """
    paid = participant.status == "paid"
    mark = "✅" if paid or fee == 0 else "⏳"
    roster = [f"{CAPTAIN_MARK} <@{participant.captain_user_id}>"]
    roster += [f"<@{m.user_id}>" for m in members[:MAX_MEMBERS_SHOWN]]
    overflow = len(members) - MAX_MEMBERS_SHOWN
    if overflow > 0:
        roster.append(f"+{overflow} more")
    return f"{index}. {mark} **{participant.team_name}** — {' · '.join(roster)}"


def create_registration_embed(tournament, participants, pot=0, deficits=None,
                              closed=False, rosters=None):
    """Build the registration board (pure): who is in, and for a paid tournament who
    has actually paid. ``deficits`` maps participant id -> tix still needed;
    ``rosters`` maps participant id -> that team's TournamentTeamMember rows."""
    deficits = deficits or {}
    rosters = rosters or {}
    fee = tournament.entry_fee or 0
    phase = "Registration closed" if closed else "Registration open"
    title = f"🏆 {tournament.name} — {phase}"
    desc = ""
    if fee > 0:
        desc = f"**Entry fee:** {fee} tix/team · **Prize pool:** {pot} tix\n"
        desc += f"**Payout:** {describe_structure(tournament.payout_structure or 'winner_take_all')}"
    if tournament.cut_to:
        desc += (" · " if desc else "") + f"**Cut:** top {tournament.cut_to}"
    embed = discord.Embed(title=title, description=desc,
                          color=discord.Color.gold())

    if not participants:
        embed.add_field(name="Teams (0)", value="No teams yet.", inline=False)
        _add_how_to_join(embed, fee, closed)
        return embed

    lines = []
    for i, p in enumerate(participants, start=1):
        lines.append(_team_line(i, p, fee, rosters.get(p.id, [])))
        short = deficits.get(p.id, 0)
        if fee > 0 and p.status != "paid" and not closed and short > 0:
            lines.append(f"     needs {short} more tix — `/wallet deposit {short}`")
    if fee > 0:
        paid_n = sum(1 for p in participants if p.status == "paid")
        label = f"Teams ({paid_n}/{len(participants)} paid)"
    else:
        label = f"Teams ({len(participants)})"

    # A paid roster's deficit lines push past the 1024-char field cap at ~10+ pending
    # teams, and Discord then rejects the whole edit (the board freezes on a stale
    # roster). The continuation label is fixed rather than derived, because `label`
    # carries the paid count and repeating it on every field would read as a new total.
    _add_chunked_field(embed, label, lines, cont_label="Teams (cont.)")
    embed.set_footer(text=f"{CAPTAIN_MARK} team captain")
    _add_how_to_join(embed, fee, closed)
    return embed


async def update_standings_message(bot, tournament_id):
    """Edit the tournament's standings message in place. No-op if not posted."""
    async with db_session() as session:
        tournament = await session.get(Tournament, tournament_id)
        if tournament is None or not tournament.standings_message_id:
            return
        participants = await get_standings_data(session, tournament_id)
        embed = create_standings_embed(
            tournament, participants, await current_round_stage(session, tournament))
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
    """(participants, pot, deficits, rosters) for a tournament's board."""
    from services import wallet_service
    from services.tournament_service import get_rosters, list_participants

    participants = await list_participants(session, tournament.id)
    rosters = await get_rosters(session, tournament.id)
    fee = tournament.entry_fee or 0
    if fee <= 0:
        return participants, 0, {}, rosters
    guild_id = str(tournament.guild_id)
    pot = await wallet_service.balance_in(
        session, guild_id, wallet_service.prize_wallet_id(tournament.id))
    pending = [p for p in participants if p.status != "paid"]
    balances = await wallet_service.balances_for(
        guild_id, [p.captain_user_id for p in pending])
    deficits = {p.id: max(fee - balances.get(p.captain_user_id, 0), 0) for p in pending}
    return participants, pot, deficits, rosters


async def refresh_boards(bot, tournament_ids):
    """Refresh several boards, guarded. The board is a view: a Discord failure on one
    logs and the rest still update. Every caller that reacts to a change goes through
    here rather than hand-rolling the try/except."""
    for t_id in set(tournament_ids):
        try:
            await update_registration_board(bot, t_id)
        except Exception as e:
            logger.warning(f"board refresh failed for {t_id}: {e}")


async def update_registration_board(bot, tournament_id):
    """Edit the registration board in place. No-op if it was never posted; a board that
    has been deleted clears its ids so it stops being retried.

    Whether the board reads as open or closed is derived from the tournament, not
    passed in by the caller. Rosters stay editable after a tournament starts, and
    every roster edit refreshes the board -- with a caller-supplied flag, the first
    such edit flipped a started tournament back to "Registration open" and
    re-advertised the join steps."""
    async with db_session() as session:
        tournament = await session.get(Tournament, tournament_id)
        if tournament is None or not tournament.board_message_id:
            return
        closed = tournament.status != "registration"
        participants, pot, deficits, rosters = await _board_state(session, tournament)
        embed = create_registration_embed(tournament, participants, pot, deficits, closed,
                                          rosters)
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
        participants, pot, deficits, rosters = await _board_state(session, tournament)
        embed = create_registration_embed(tournament, participants, pot, deficits,
                                          rosters=rosters)
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
