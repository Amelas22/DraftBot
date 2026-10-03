"""Rendering and live-updating of the tournament standings message.

`create_standings_embed` is the single renderer shared by `/tournament status`
and the auto-updating message. `update_standings_message` edits the stored
message in place on every result change, mirroring
`utils.update_leaderboards_for_guild`.
"""
import discord
from loguru import logger
from sqlalchemy import func, select

from database.db_session import db_session
from models.tournament import (
    BRACKET_STAGES,
    STAGE_PLAY_IN,
    STAGE_SWISS,
    Tournament,
    TournamentMatch,
)
from services.tournament_escrow_service import describe_structure
from services.tournament_service import (
    bracket_rows,
    current_round_stage,
    cut_after_rank,
    get_standings_with_omw,
    get_tournament_id_for_match,
)


def bracket_stage_name(stage, matches_in_round):
    """A bracket round's name: Play-in, or by how many matches it holds."""
    if stage == STAGE_PLAY_IN:
        return "Play-in"
    return {1: "Final", 2: "Semifinal", 4: "Quarterfinal"}.get(
        matches_in_round, f"Round of {matches_in_round * 2}")


def round_label(total_rounds, round_number, stage, swiss_noun="Round",
                matches_in_round=None):
    """How one round is named, wherever a round is named.

    Bracket rounds are numbered past total_rounds -- the first one of a 3-round
    swiss is round 4 -- so naming them as swiss rounds calls the semifinal
    "Week 4". Three display sites did that arithmetic differently (or not at
    all); this is the one that answers now. A bracket round is named by
    bracket_stage_name when the caller knows its match count.

    ``swiss_noun`` is the word a site already uses for a swiss round: "Week" in
    the pairings channel, "Round" on a match's control message. Only the swiss
    wording differs between sites -- the playoff form is identical everywhere,
    which is the half that was wrong.
    """
    if stage in BRACKET_STAGES:
        if matches_in_round:
            return bracket_stage_name(stage, matches_in_round)
        return f"Playoff round {round_number - total_rounds}"
    return f"{swiss_noun} {round_number}"


async def round_name(session, round_, total_rounds, swiss_noun="Round"):
    """round_label for a stored round, counting its matches only when a bracket
    round needs them for its name."""
    count = None
    if round_.stage in BRACKET_STAGES:
        count = (await session.execute(
            select(func.count(TournamentMatch.id))
            .where(TournamentMatch.round_id == round_.id))).scalar_one()
    return round_label(total_rounds, round_.round_number, round_.stage,
                       swiss_noun=swiss_noun, matches_in_round=count)


def _round_line(tournament, stage):
    """The "**Round:** …" line. A bracket is one pinned message, not a round
    count: the swiss "N of M" form reads "Round: 4/3" once the bracket starts."""
    if stage in BRACKET_STAGES:
        link = ""
        if tournament.bracket_channel_id and tournament.bracket_message_id:
            link = (f" (https://discord.com/channels/{tournament.guild_id}/"
                    f"{tournament.bracket_channel_id}/{tournament.bracket_message_id})")
        return f"**Round:** Playoff — see bracket{link}"
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


def _bracket_side(side, feeder, won):
    if side is None:
        return f"winner of #{feeder}" if feeder else "TBD"
    seed, name = side
    text = f"({seed}) {name}" if seed is not None else name
    return f"**{text}**" if won else text


def create_bracket_embed(tournament_name, rows):
    """The live bracket: one aligned line per match, earliest first.

    The id and stage sit in a code span padded to one width, the same
    technique as the standings board, so every team name starts at the same
    x however wide its emoji renders."""
    embed = discord.Embed(title=f"🏆 {tournament_name} — Bracket", color=discord.Color.gold())
    shown = [r for r in rows if not r.is_bye]
    width = max((len(f"#{r.match_id} {r.stage}") for r in shown), default=0)
    lines = []
    for r in shown:
        decided = r.a_wins is not None and r.b_wins is not None and r.a_wins != r.b_wins
        a = _bracket_side(r.a, r.a_from, decided and r.a_wins > r.b_wins)
        b = _bracket_side(r.b, r.b_from, decided and r.b_wins > r.a_wins)
        middle = f" {r.a_wins}–{r.b_wins} " if r.a_wins is not None else " vs "
        room = f" — <#{r.thread_id}>" if r.thread_id and r.a_wins is None else ""
        lines.append(f"`{f'#{r.match_id} {r.stage}'.ljust(width)}` {a}{middle}{b}{room}")
    _add_chunked_field(embed, "Matches", lines)
    return embed


def _standings_rows(participants, omw):
    """One line per team: an inline code span, then the name in normal markdown.

    Discord aligns nothing in proportional text, and a fenced block aligns but
    strips markdown -- so a dropped team could not be struck through. An inline
    span splits the difference: it renders monospace, so spans built to the same
    character count render the same width and every name starts at the same x,
    while the name itself stays outside the span where bold and strikethrough
    still apply.

    The numbers have to lead. They are the fixed-width anchor, and the parts
    whose rendered width cannot be known here -- emoji in a team name, a name
    long enough to wrap -- have to come last so their drift never reaches the
    columns. Column widths are measured from the teams actually being shown
    rather than fixed, so a drawn record (W-L-D, two wider than W-L) or a
    three-digit rank widens the column instead of knocking every row below it
    out of true.
    """
    rank_w = len(str(len(participants)))
    points_w = max(len(str(p.points)) for p in participants)
    record_w = max(len(p.record) for p in participants)

    rows = []
    for i, p in enumerate(participants, start=1):
        cells = [f"{i:>{rank_w}}", f"{p.points:>{points_w}}", f"{p.record:>{record_w}}"]
        value = (omw or {}).get(p.id)
        if value is not None:
            # 5 wide, not 4: an OMW% of 100.0 is one character longer than 99.9
            # and would otherwise push its own row out of line.
            cells.append(f"{value * 100:>5.1f}%")
        # A dropped team keeps its place and its record, because both still count
        # towards the tiebreaks of everyone it played. Struck through and labelled
        # is what stops the pairings quietly shrinking and reading as a bug.
        name = (f"~~{p.team_name}~~ *(dropped)*" if p.dropped_at
                else f"**{p.team_name}**")
        rows.append(f"`{'  '.join(cells)}` {name}")
    return rows


def _cut_rule(cut_to):
    """The rule drawn between the last team in the bracket and the first out.

    Labelled rather than a bare line: the field splitter breaks between rows at
    Discord's 1024-character cap, so the rule can land at the top of a
    continuation field, away from the rank it follows. Naming the cut keeps it
    readable wherever it lands.
    """
    return f"────────── **top {cut_to} cut** ──────────"


def create_standings_embed(tournament, participants, stage=STAGE_SWISS, omw=None,
                           cut_after=None):
    """Build the standings embed for a tournament (pure).

    ``stage`` is the stage of the round it is on (see
    tournament_service.current_round_stage). It defaults to swiss for the
    read-only callers of a tournament that has none.

    ``omw`` is {participant id: OMW%} from ``omw_percentages`` -- the same
    mapping the sort used. It is passed in rather than derived here because
    deriving it needs the match graph, and a second derivation is how the
    number on the board drifts from the number that ordered the board. Omit it
    and the rows render exactly as before.

    ``cut_after`` is the rank the top-N rule is drawn after, from
    ``tournament_service.cut_after_rank`` -- the rank, not the cut size, because
    a dropped team holds its standings place but cannot be seated. None draws no
    rule, which is also what that function returns for a cut nothing can fill."""
    embed = discord.Embed(
        title=f"🏆 {tournament.name} — Standings",
        description=(
            f"**Status:** {tournament.status.title()}\n"
            f"{_round_line(tournament, stage)}"
        ),
        color=discord.Color.gold(),
    )
    if participants:
        rows = _standings_rows(participants, omw)
        # tournament.cut_to guards the label, not the position: the rule is
        # named after the cut it marks, so a cut_after with no declared cut
        # would render "top None cut" rather than no rule at all.
        if cut_after and tournament.cut_to and 0 < cut_after < len(rows):
            rows.insert(cut_after, _cut_rule(tournament.cut_to))
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
        participants, omw = await get_standings_with_omw(session, tournament_id)
        embed = create_standings_embed(
            tournament, participants, await current_round_stage(session, tournament),
            omw=omw, cut_after=cut_after_rank(participants, tournament.cut_to))
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


async def update_bracket_message(bot, tournament_id):
    """Edit the live bracket in place. No-op until it has been posted; Discord
    errors are logged, not raised."""
    async with db_session() as session:
        tournament = await session.get(Tournament, tournament_id)
        if tournament is None or not tournament.bracket_message_id:
            return
        embed = create_bracket_embed(
            tournament.name, await bracket_rows(session, tournament_id))
        channel_id = int(tournament.bracket_channel_id)
        message_id = int(tournament.bracket_message_id)
    try:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        await channel.get_partial_message(message_id).edit(embed=embed)
    except discord.HTTPException as e:
        logger.warning(f"Could not update the bracket for tournament {tournament_id}: {e}")


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
