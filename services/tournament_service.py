"""Service layer for team-based Swiss tournaments.

Slice 1: create/register/view. Slice 2: start, Swiss rounds, admin-set results,
standings.

All functions take an AsyncSession so callers control the transaction and tests
can point them at a temp database (mirrors the leaderboard_service convention).
"""
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from database.db_session import db_session
from draft_organization.bracket import bracket_tree, build_bracket, final_placement
from draft_organization.swiss import (
    omw_percentages,
    pair_round,
    pairing_order,
    rank_standings,
    round_robin_schedule,
)
from helpers.match_control import match_tag
from models.draft_session import DraftSession
from models.team import Team
from models.tournament import (
    BRACKET_STAGES,
    STAGE_PLAY_IN,
    STAGE_PLAYOFF,
    STAGE_SWISS,
    Tournament,
    TournamentMatch,
    TournamentParticipant,
    TournamentRound,
    TournamentTeamMember,
)

ACTIVE_STATUSES = ("registration", "active")
POINTS_WIN = 3
POINTS_DRAW = 1
# STAGE_SWISS / STAGE_PLAYOFF live in models.tournament, which owns them: the
# column default needs them too, and a constant defined here would be a circular
# import back into the model. They are imported above beside POINTS_WIN's peers.


def is_playoff(round_):
    """True when a round is a bracket round (the play-in included).

    The one place a stage value is interpreted. It used to be spelled out as
    `== "playoff"` in three places and `!= "playoff"` in a fourth, so any third
    stage would have been included by one predicate and excluded by the other.
    """
    return round_ is not None and round_.stage in BRACKET_STAGES


def _pairable(participants):
    """The teams a round pairs: entry fee held, and still in the tournament.

    The one definition of "in", because every round re-pairs from scratch and the
    answer has to be the same each time. start_tournament asked it of the first
    round and advance_round did not ask it at all, so a team that never completed
    registration sat out round one and then joined the pairings for round two.
    """
    return [p for p in participants if p.status == "paid" and p.dropped_at is None]


def _cut_eligible(standings):
    """The teams a cut can seat: those still in, with their entry fee held.

    One rule, because the end-of-swiss prompt disables its Start button on this
    count while start_playoff refuses on this list -- if the two drift, the
    prompt offers a button that then refuses, which is the failure the disabled
    state exists to prevent.
    """
    return _pairable(standings)


def _seated_by_cut(standings, cut_to):
    """The teams a top-N cut would actually seat, in rank order.

    Empty when the cut cannot be filled, which is the answer `start_playoff`
    already gives: a bracket short of entrants does not run, so nobody is
    seated rather than a smaller bracket being invented.

    One definition, because three callers were deriving it independently and
    they have to agree -- `cut_after_rank` draws the line the standings show,
    `start_playoff` seats the teams above it, and the end-of-swiss prompt
    enables its Start button on it. If they drift, the prompt offers a button
    that then refuses.

    Rank order is load-bearing and inherited: `_cut_eligible` filters without
    reordering, so the last entry here is the last team seated and the one the
    line is drawn after.
    """
    # `< 1`, not just falsy. A negative size is not a smaller cut: `cut_to=-1`
    # slices "all but the last" and reports a line two-thirds down the
    # standings, and a size past the field length raised IndexError instead.
    # Both were reachable -- create_tournament stores cut_to unvalidated, and
    # only the Discord option enforces a minimum -- so neither is worth
    # preserving. A cut that is not a positive number seats nobody.
    if not cut_to or cut_to < 1:
        return []
    eligible = _cut_eligible(standings)
    return list(eligible[:cut_to]) if len(eligible) >= cut_to else []


@dataclass(frozen=True)
class BracketRow:
    match_id: int
    stage: str                       # "Quarterfinal", "Play-in", ...
    a: tuple[int | None, str] | None  # (seed, team name), None while waiting
    b: tuple[int | None, str] | None
    a_from: int | None               # feeder match id for an empty slot
    b_from: int | None
    a_wins: int | None
    b_wins: int | None
    thread_id: str | None
    is_bye: bool


async def bracket_rows(session, tournament_id):
    """Every bracket match as a BracketRow, ordered by round, then match id."""
    from services.tournament_formatter import round_name  # formatter imports this module
    tournament = await session.get(Tournament, tournament_id)
    participants = {p.id: p for p in await list_participants(session, tournament_id)}
    feeders = {}                                   # (parent id, slot) -> child id
    matches = []
    for round_ in await _playoff_rounds(session, tournament_id):
        name = await round_name(session, round_, tournament.total_rounds)
        for m in await _round_matches(session, round_.id):
            matches.append((m, name, round_.round_number))
            if m.feeds_match_id is not None:
                feeders[(m.feeds_match_id, m.feeds_slot)] = m.id

    def side(participant_id):
        if participant_id is None:
            return None
        p = participants[participant_id]
        return (p.seed, p.team_name)

    rows = [BracketRow(m.id, name, side(m.team_a_participant_id),
                       side(m.team_b_participant_id),
                       feeders.get((m.id, "a")), feeders.get((m.id, "b")),
                       m.team_a_wins, m.team_b_wins, m.thread_id, m.is_bye)
            for m, name, _ in matches]
    order = {m.id: (number, m.id) for m, _, number in matches}
    return sorted(rows, key=lambda r: order[r.match_id])


@dataclass(frozen=True)
class PlayInProposal:
    teams: tuple[Any, Any] | None
    tied: int


def propose_play_in(standings, omw, cut_to):
    """A play-in for the last seat when the team in it and the first team out
    are level on everything but the draw: points, rounds played, exact OMW%.

    Only a two-team tie is proposed. A wider one is reported (`teams` None)
    so the organizer can pick two or skip; None means there is no tie at the
    line at all. Eligibility is _cut_eligible's, so this can never propose a
    team the cut would not seat."""
    eligible = _cut_eligible(standings)
    if not cut_to or len(eligible) <= cut_to:
        return None

    def key(p):
        return (p.points, p.match_wins + p.match_losses + p.match_draws, omw[p.id])

    last, first_out = eligible[cut_to - 1], eligible[cut_to]
    if key(last) != key(first_out):
        return None
    tied = [p for p in eligible if key(p) == key(last)]
    return PlayInProposal((last, first_out) if len(tied) == 2 else None, len(tied))


def cut_after_rank(standings: list[Any], cut_to: int | None) -> int | None:
    """The standings rank the top-N cut line is drawn after, or None for no line.

    NOT simply `cut_to`. A dropped team keeps its place in the standings --
    its record still feeds every opponent's tiebreak -- but `_cut_eligible`
    will not seat it. A line drawn at rank N would then promise the last seat
    to a team that cannot take one.

    Lives here, beside `_cut_eligible`, because both the Discord standings and
    the public league page draw this line. Two copies of "who can be seated"
    is exactly the divergence between the two surfaces worth preventing.

    None when the cut cannot be filled, because `start_playoff` refuses that
    cut outright -- drawing a line would advertise a bracket that will not run.
    """
    seated = _seated_by_cut(standings, cut_to)
    if not seated:
        return None
    # Identity, not `.id`: these rows are not necessarily flushed.
    return standings.index(seated[-1]) + 1


class SwissComplete(Exception):
    """Swiss is over and a cut is declared, so the next step is a choice:
    start the bracket, or finish and crown the swiss leader.

    Raised rather than returned because `advance_round` completing the
    tournament is irreversible and one call away — the caller must decide.
    """

    def __init__(self, cut_to, eligible, fillable):
        super().__init__(f"Swiss complete; cut to top {cut_to} is pending.")
        self.cut_to = cut_to
        self.eligible = eligible
        # Whether the bracket can actually be seated, decided HERE by
        # `_seated_by_cut` rather than left for the caller to infer from
        # `eligible`. The prompt disables its Start button on this, and
        # `start_playoff` refuses on the same rule -- when the caller
        # re-derived it, the two could drift and the prompt would offer a
        # button that then refused. Required, not defaulted: a new raise site
        # has to answer the question rather than inherit a guess.
        self.fillable = fillable


async def get_active_tournament(session, guild_id):
    """Return the guild's current registration/active tournament, or None."""
    stmt = select(Tournament).where(
        Tournament.guild_id == str(guild_id),
        Tournament.status.in_(ACTIVE_STATUSES),
    )
    result = await session.execute(stmt)
    return result.scalars().first()


ALL_OPEN_FORMATS = ("round_robin", "manual")
MANUAL_ROUND_SIZE = 10  # matches per pairings message, well under Discord's 25-button cap


async def find_participant_by_name(session, tournament_id, team_name):
    """Find a tournament participant by team name (case-insensitive), or None."""
    stmt = select(TournamentParticipant).where(
        TournamentParticipant.tournament_id == tournament_id,
        func.lower(TournamentParticipant.team_name) == team_name.strip().lower(),
    )
    return (await session.execute(stmt)).scalars().first()


async def get_latest_completed_tournament(session, guild_id):
    """The guild's most recently completed tournament, or None. Used by payout, since
    get_active_tournament only returns registration/active ones."""
    stmt = (
        select(Tournament)
        .where(Tournament.guild_id == str(guild_id), Tournament.status == "completed")
        .order_by(Tournament.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def create_tournament(session, guild_id, name, total_rounds, format="swiss", entry_fee=0,
                            payout_structure="winner_take_all", cut_to=None):
    """Create a tournament in registration status.

    ``format`` is 'swiss', 'round_robin', or 'manual'. For the all-open formats
    total_rounds is set at start (derived from the schedule), so callers may
    pass 0. ``entry_fee`` is the per-team escrow in tix (0 = free); ``payout_structure``
    is how the pool splits at payout. ``cut_to`` declares a top-N single-elimination
    playoff to follow Swiss (None = no cut, i.e. today's behavior). Raises ValueError
    if the guild already has a registration/active tournament (one active per guild
    keeps other commands argument-free).
    """
    if format not in ("swiss",) + ALL_OPEN_FORMATS:
        raise ValueError(f"Unknown tournament format: {format}")
    existing = await get_active_tournament(session, guild_id)
    if existing is not None:
        raise ValueError(
            f"'{existing.name}' is already {existing.status} in this server. "
            "Finish it before creating a new tournament."
        )
    tournament = Tournament(
        guild_id=str(guild_id), name=name, total_rounds=total_rounds, format=format,
        entry_fee=max(0, int(entry_fee or 0)), payout_structure=payout_structure,
        cut_to=cut_to or None,
    )
    session.add(tournament)
    await session.flush()
    return tournament


async def register_team(session, tournament_id, team_name, captain_user_id):
    """Register a team into a tournament, creating its Team identity if new.

    Returns (participant, created). Idempotent: re-registering an already
    registered team returns the existing participant with created=False.
    Raises ValueError if the tournament doesn't exist or isn't open for
    registration.
    """
    tournament = await session.get(Tournament, tournament_id)
    if tournament is None:
        raise ValueError("Tournament not found.")
    if tournament.status != "registration":
        raise ValueError(
            f"'{tournament.name}' is {tournament.status} — registration is closed."
        )

    # Find or create the persistent Team identity (case-insensitive, like
    # register_team_to_db in session.py, but on the caller's session).
    normalized = team_name.strip()
    stmt = select(Team).where(func.lower(Team.TeamName) == normalized.lower())
    team = (await session.execute(stmt)).scalars().first()
    if team is None:
        team = Team(TeamName=normalized)
        session.add(team)
        await session.flush()

    stmt = select(TournamentParticipant).where(
        TournamentParticipant.tournament_id == tournament_id,
        TournamentParticipant.team_id == team.TeamID,
    )
    participant = (await session.execute(stmt)).scalars().first()
    if participant is not None:
        return participant, False

    participant = TournamentParticipant(
        tournament_id=tournament_id,
        team_id=team.TeamID,
        team_name=team.TeamName,
        captain_user_id=str(captain_user_id),
        # A paid tournament starts a new team as 'pending' until escrow is secured;
        # a free tournament (entry_fee 0) leaves it 'paid' (the column default).
        status="pending" if (tournament.entry_fee or 0) > 0 else "paid",
    )
    session.add(participant)
    await session.flush()
    return participant, True


async def list_participants(session, tournament_id):
    """Return the tournament's participants in registration order."""
    stmt = (
        select(TournamentParticipant)
        .where(TournamentParticipant.tournament_id == tournament_id)
        .order_by(TournamentParticipant.id)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def remove_team(session, tournament_id, team_name):
    """Remove a registered team (admin action; only while registration is open)."""
    tournament = await session.get(Tournament, tournament_id)
    if tournament is None:
        raise ValueError("Tournament not found.")
    if tournament.status != "registration":
        raise ValueError(
            f"Teams cannot be removed once '{tournament.name}' has started."
        )
    stmt = select(TournamentParticipant).where(
        TournamentParticipant.tournament_id == tournament_id,
        func.lower(TournamentParticipant.team_name) == team_name.strip().lower(),
    )
    participant = (await session.execute(stmt)).scalars().first()
    if participant is None:
        raise ValueError(f"'{team_name}' is not registered for this tournament.")
    # TournamentParticipant.team_members is an ORM relationship now (services/
    # tournament_roles.py needs it), but it carries no delete cascade, so this
    # explicit delete is still required -- not redundant double-bookkeeping.
    # Without it, session.delete(participant) would make SQLAlchemy try to
    # nullify participant_id on the loaded children instead, and
    # participant_id is nullable=False, so the flush would fail with an
    # IntegrityError. This delete is what keeps the roster rows from either
    # outliving or corrupting the team.
    await session.execute(
        delete(TournamentTeamMember).where(
            TournamentTeamMember.participant_id == participant.id
        )
    )
    await session.delete(participant)
    await session.flush()
    return participant


async def drop_team(session, tournament_id, team_name):
    """Take a team out of the pairings for the rounds still to come.

    The running counterpart to remove_team, which only serves registration: that
    one deletes the row and refunds the fee, because nothing has been played yet.
    Once a tournament is live the row has to stay -- other teams' tiebreaks are
    computed from the matches this team played -- so the drop is a mark, not a
    deletion, and the entry fee stays in the pot the way it does at a real event.

    A match already paired is left alone. The team is simply not in the pool the
    next time a round is paired, which is what a drop means; an open match from
    the round in progress is still the organizer's to record.

    That is also why this is a swiss-only, pre-bracket operation. Swiss is the
    only stage that re-pairs from the pool, so it is the only one a drop changes:
    round_robin and manual build every round at the start, and the bracket
    advances on results, never on who is pairable. Marking a team in either would
    say it had left while it went on being paired -- and leave the abandoned
    matches blocking the tournament exactly as before.
    """
    tournament = await session.get(Tournament, tournament_id)
    if tournament is None:
        raise ValueError("Tournament not found.")
    if tournament.status != "active":
        raise ValueError(
            f"'{tournament.name}' is not running — teams leave a tournament that "
            f"has not started with remove_team, which also refunds the entry fee."
        )
    if tournament.format != "swiss":
        raise ValueError(
            f"'{tournament.name}' is a {tournament.format} tournament — its whole "
            f"schedule was built when it started, so a drop would change no "
            f"pairing. Record the abandoned matches with /tournament set_result, "
            f"or end it with /tournament finish."
        )
    if await _playoff_rounds(session, tournament_id):
        raise ValueError(
            "The bracket has already been built, and it advances on results "
            "rather than on who is pairable — a drop would not take "
            f"'{team_name}' out of it. Record the result with /tournament "
            "set_result, or end the tournament with /tournament finish."
        )

    participant = await find_participant_by_name(session, tournament_id, team_name)
    if participant is None:
        raise ValueError(f"'{team_name}' is not in this tournament.")
    if participant.dropped_at is not None:
        raise ValueError(f"'{participant.team_name}' has already dropped.")

    remaining = [p for p in _pairable(await list_participants(session, tournament_id))
                 if p.id != participant.id]
    if len(remaining) < 2:
        raise ValueError(
            f"Dropping '{participant.team_name}' would leave "
            f"{len(remaining)} team(s) to pair — finish the tournament instead."
        )

    participant.dropped_at = datetime.now()
    await session.flush()
    logger.info(f"tournament {tournament_id}: '{participant.team_name}' dropped "
                f"in round {tournament.current_round}")
    return participant


# ---- team rosters ---------------------------------------------------------------

async def find_participants_for_captain(session, tournament_id, captain_user_id):
    """Every team this user captains in the tournament, in registration order.

    A list rather than one row because nothing stops a user registering several
    teams -- register_team keys uniqueness on the team, never the captain. Taking
    .first() here sent roster edits to whichever row the database happened to
    return and left the captain's other teams unreachable, with no error to say so.
    """
    stmt = (
        select(TournamentParticipant)
        .where(
            TournamentParticipant.tournament_id == tournament_id,
            TournamentParticipant.captain_user_id == str(captain_user_id),
        )
        .order_by(TournamentParticipant.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def other_teams_for_user(session, tournament_id, user_id, exclude_participant_id):
    """The tournament's other teams this player already belongs to.

    Players may be shared between teams, so this no longer blocks anything -- it
    feeds the note on the reply, which is how the overlap stays visible. Counts
    captaincy as well as roster rows: someone who captains Bravo is on Bravo even
    though no roster row says so.
    """
    user_id = str(user_id)
    stmt = (
        select(TournamentParticipant)
        .outerjoin(TournamentTeamMember,
                   TournamentTeamMember.participant_id == TournamentParticipant.id)
        .where(
            TournamentParticipant.tournament_id == tournament_id,
            TournamentParticipant.id != exclude_participant_id,
            or_(
                TournamentParticipant.captain_user_id == user_id,
                TournamentTeamMember.user_id == user_id,
            ),
        )
        .order_by(TournamentParticipant.id)
        .distinct()
    )
    return list((await session.execute(stmt)).scalars().all())


async def _assert_roster_editable(session, participant):
    """Rosters stay editable while a tournament runs, but a finished one is a record."""
    tournament = await session.get(Tournament, participant.tournament_id)
    if tournament is None:
        raise ValueError("Tournament not found.")
    if tournament.status == "completed":
        raise ValueError(f"'{tournament.name}' is completed — its rosters are final.")
    return tournament


async def add_teammate(session, participant, user_id, display_name):
    """Put a player on a team's roster.

    Returns (member, created). Idempotent, like register_team: adding someone who
    is already on the roster returns the existing row with created=False and leaves
    their stored display name alone. Raises ValueError if the tournament is
    completed or if the player is this team's own captain.

    Belonging to another team in the same tournament is allowed -- players get
    shared, and callers surface that with other_teams_for_user rather than blocking.
    """
    await _assert_roster_editable(session, participant)
    user_id = str(user_id)

    if user_id == participant.captain_user_id:
        raise ValueError(
            f"<@{user_id}> is the captain of {participant.team_name} and is already on the team."
        )

    stmt = select(TournamentTeamMember).where(
        TournamentTeamMember.participant_id == participant.id,
        TournamentTeamMember.user_id == user_id,
    )
    existing = (await session.execute(stmt)).scalars().first()
    if existing is not None:
        return existing, False

    member = TournamentTeamMember(
        participant_id=participant.id,
        user_id=user_id,
        display_name=display_name,
    )
    try:
        # SAVEPOINT so losing the race below doesn't poison the caller's transaction.
        async with session.begin_nested():
            session.add(member)
            await session.flush()
    except IntegrityError:
        # Another add committed this same player between our lookup above and this
        # insert. uq_participant_member held, so the roster is right -- report it the
        # way the uncontended duplicate is reported rather than raising at the user.
        return (await session.execute(stmt)).scalars().first(), False
    return member, True


async def remove_teammate(session, participant, user_id):
    """Take a player off a team's roster. Returns True if they were on it."""
    await _assert_roster_editable(session, participant)
    stmt = select(TournamentTeamMember).where(
        TournamentTeamMember.participant_id == participant.id,
        TournamentTeamMember.user_id == str(user_id),
    )
    member = (await session.execute(stmt)).scalars().first()
    if member is None:
        return False
    await session.delete(member)
    await session.flush()
    return True


async def get_rosters(session, tournament_id):
    """{participant_id: [members in add order]} for one tournament.

    One query for the whole event: the registration board renders every team at
    once, so a per-participant lookup would be an N+1 on every board refresh.
    Teams with an empty roster are absent from the mapping.
    """
    stmt = (
        select(TournamentTeamMember)
        .join(TournamentParticipant,
              TournamentTeamMember.participant_id == TournamentParticipant.id)
        .where(TournamentParticipant.tournament_id == tournament_id)
        .order_by(TournamentTeamMember.id)
    )
    rosters = {}
    for member in (await session.execute(stmt)).scalars().all():
        rosters.setdefault(member.participant_id, []).append(member)
    return rosters


# ---- slice 2: rounds, results, standings -------------------------------------

def _award_bye(participant):
    participant.match_wins += 1
    participant.points += POINTS_WIN
    participant.byes += 1


def _apply_result(part_a, part_b, a_wins, b_wins, sign=1):
    """Apply (sign=1) or revert (sign=-1) a result onto both participants."""
    part_a.game_wins += sign * a_wins
    part_a.game_losses += sign * b_wins
    part_b.game_wins += sign * b_wins
    part_b.game_losses += sign * a_wins
    if a_wins > b_wins:
        part_a.match_wins += sign
        part_a.points += sign * POINTS_WIN
        part_b.match_losses += sign
    elif b_wins > a_wins:
        part_b.match_wins += sign
        part_b.points += sign * POINTS_WIN
        part_a.match_losses += sign
    else:
        part_a.match_draws += sign
        part_b.match_draws += sign
        part_a.points += sign * POINTS_DRAW
        part_b.points += sign * POINTS_DRAW


async def _create_round_with_pairings(session, tournament, participants, history,
                                      rng, played=(), everyone=None):
    """Create the next round row and its matches; auto-scores the bye.

    `played` is every match so far, for the pairing tiebreaks. Empty for round
    one, which has no record to rank on.

    OMW is computed over the WHOLE field, not the teams being paired. A dropped
    team keeps its place in the standings and its record still feeds every
    opponent's tiebreak -- and `omw_percentages` silently skips an opponent
    missing from the list it is handed, so ranking the pairable teams alone
    would quietly drop every dropped opponent from the tiebreak and rank the
    field on numbers the board does not show.

    The engine is handed the field already ranked, and told whether this is the
    final round. Rank decides two different amounts in those two cases -- the
    boundary seats always, every seat in the last round -- and the reasoning
    for that split lives on `_arrange_bracket`.
    """
    round_number = tournament.current_round + 1
    new_round = TournamentRound(tournament_id=tournament.id, round_number=round_number)
    session.add(new_round)
    await session.flush()

    # Defaults to a fresh load rather than to `participants`: getting this
    # wrong narrows the tiebreak silently, which is the bug this argument
    # exists to prevent, so the safe answer is the one you get by saying
    # nothing. `advance_round` passes the list it already holds.
    if everyone is None:
        everyone = await list_participants(session, tournament.id)
    played = list(played)
    omw = omw_percentages(everyone, played)
    ranked = pairing_order(participants, played, rng, omw=omw)
    teams = [{"id": p.id, "points": p.points, "byes": p.byes} for p in ranked]

    # `cut_to` only matters in the final round, where it lets the engine break
    # a tie towards not pairing a team still playing for a seat against one
    # that is out on arithmetic alone.
    pairs, bye_id = pair_round(teams, history, rng,
                               power_pair=round_number == tournament.total_rounds,
                               cut_to=tournament.cut_to,
                               points_for_win=POINTS_WIN)
    by_id = {p.id: p for p in participants}

    matches = []
    for id_a, id_b in pairs:
        match = TournamentMatch(
            round_id=new_round.id,
            team_a_participant_id=id_a,
            team_b_participant_id=id_b,
        )
        session.add(match)
        matches.append(match)
    if bye_id is not None:
        bye_match = TournamentMatch(
            round_id=new_round.id,
            team_a_participant_id=bye_id,
            team_b_participant_id=None,
            is_bye=True,
        )
        session.add(bye_match)
        matches.append(bye_match)
        _award_bye(by_id[bye_id])

    tournament.current_round = round_number
    await session.flush()
    return new_round, matches


async def _playoff_rounds(session, tournament_id):
    """Playoff rounds for a tournament, earliest first."""
    stmt = (
        select(TournamentRound)
        .where(TournamentRound.tournament_id == tournament_id)
        .where(TournamentRound.stage.in_(BRACKET_STAGES))
        .order_by(TournamentRound.round_number)
    )
    return (await session.execute(stmt)).scalars().all()


async def _swiss_frozen(session, tournament):
    """True once a tournament's swiss results can no longer change: it has ended
    or its bracket exists, and seeds, placement and prizes were drawn from them."""
    return tournament.status != "active" or bool(
        await _playoff_rounds(session, tournament.id))


async def start_playoff(session, tournament_id, size=None, play_in=None):
    """Cut to the top `size` and create the first playoff round.

    Seeds are stamped from final swiss standings and never recomputed: they
    are the numbers players were told, and they are what orders teams that
    went out at the same depth.

    `play_in` is two participant ids tied for the last seat: they take seeds
    size and size+1 and play one match whose winner fills seed size's slot.
    """
    tournament = await session.get(Tournament, tournament_id)
    if tournament is None:
        raise ValueError("Tournament not found.")
    if tournament.status != "active":
        raise ValueError(f"'{tournament.name}' is not active.")
    # A cut is defined off swiss standings. The all-open formats stamp
    # current_round = total_rounds at START, so without this a cut could be
    # made over a field that has not played a single match.
    if tournament.format != "swiss":
        raise ValueError(
            f"A cut is made off Swiss standings — '{tournament.name}' is a "
            f"{tournament.format} tournament. Use /tournament finish to end it."
        )

    size = size or tournament.cut_to
    if not size:
        raise ValueError(
            "No cut size — declare one at creation or pass `top:` to this command."
        )
    if size < 2:
        raise ValueError("A cut needs at least 2 teams.")
    if tournament.current_round < tournament.total_rounds:
        raise ValueError(
            f"Swiss isn't finished — round {tournament.current_round} of "
            f"{tournament.total_rounds}."
        )
    if await _playoff_rounds(session, tournament_id):
        raise ValueError("The bracket has already been built.")
    # advance_round refuses to move on with results outstanding; the explicit
    # command must too, or seeds get stamped from partial standings and the
    # money follows them. Checked across every round, not just the last: the
    # seeds come from the whole swiss record.
    unreported = await count_unreported_matches(session, tournament_id)
    if unreported:
        raise ValueError(
            f"{unreported} match(es) still need results — seeds must come from "
            "final standings."
        )

    standings = await get_standings_data(session, tournament_id)
    if play_in is not None:
        eligible = _cut_eligible(standings)
        pair = [p for p in eligible if p.id in play_in]
        if len(set(play_in)) != 2 or len(pair) != 2:
            raise ValueError("A play-in needs two different teams that can make the cut.")
        rest = [p for p in eligible if p.id not in play_in][: size - 1]
        if len(rest) < size - 1:
            raise ValueError(f"Not enough eligible teams for a top {size} with a play-in.")
        for position, p in enumerate(rest, start=1):
            p.seed = position
        pair[0].seed, pair[1].seed = size, size + 1       # eligible order: higher first
        by_seed = {position: p.id for position, p in enumerate(rest, start=1)}
        rounds = await _build_bracket(session, tournament, by_seed, size,
                                      tournament.total_rounds + 2)
        return await _attach_play_in(session, tournament, rounds, pair)
    cut = _seated_by_cut(standings, size)
    if not cut:
        raise ValueError(
            f"Only {len(_cut_eligible(standings))} eligible team(s) — can't cut "
            f"to top {size}. Re-run with a smaller `top:`."
        )
    for position, participant in enumerate(cut, start=1):
        participant.seed = position
    by_seed = {position: p.id for position, p in enumerate(cut, start=1)}

    rounds = await _build_bracket(session, tournament, by_seed, size,
                                  tournament.total_rounds + 1)
    return rounds[0]


async def _attach_play_in(session, tournament, rounds, pair):
    """Create the play-in round ahead of the bracket and wire its winner into
    the slot left empty for the last seed. Its match is created last, so its id
    is higher than the final's; rounds, not ids, order the display."""
    play_in_round = TournamentRound(tournament_id=tournament.id,
                                    round_number=tournament.total_rounds + 1,
                                    stage=STAGE_PLAY_IN)
    session.add(play_in_round)
    await session.flush()
    first = await _round_matches(session, rounds[0].id)
    target = next(m for m in first if m.team_b_participant_id is None and not m.is_bye)
    session.add(TournamentMatch(round_id=play_in_round.id,
                                team_a_participant_id=pair[0].id,
                                team_b_participant_id=pair[1].id,
                                feeds_match_id=target.id, feeds_slot="b"))
    tournament.current_round = play_in_round.round_number
    await session.flush()
    return play_in_round


async def _build_bracket(session, tournament, seat_ids, size, first_round_number):
    """Create every round and match of the bracket, linked by feeds_match_id/
    feeds_slot. Earlier rounds get lower ids; a bye is created decided and its
    team written straight into the parent slot, awarding nothing."""
    nodes = bracket_tree(size)
    round_rows = []
    for r in range(max(n.round for n in nodes) + 1):
        row = TournamentRound(tournament_id=tournament.id,
                              round_number=first_round_number + r, stage=STAGE_PLAYOFF)
        session.add(row)
        round_rows.append(row)
    await session.flush()

    by_node = {}
    for n in nodes:
        is_bye = n.round == 0 and n.b_seed is None
        m = TournamentMatch(
            round_id=round_rows[n.round].id,
            team_a_participant_id=seat_ids.get(n.a_seed) if n.a_seed else None,
            team_b_participant_id=seat_ids.get(n.b_seed) if n.b_seed else None,
            is_bye=is_bye,
        )
        session.add(m)
        by_node[(n.round, n.index)] = m
    await session.flush()

    for n in nodes:
        if n.feeds is None:
            continue
        child = by_node[(n.round, n.index)]
        parent = by_node[(n.feeds[0], n.feeds[1])]
        child.feeds_match_id, child.feeds_slot = parent.id, n.feeds[2]
        if child.is_bye:
            setattr(parent, _slot_column(n.feeds[2]), child.team_a_participant_id)
    # A bye can put both teams of a later match in place at the cut.
    playable = [n.round for n in nodes
                if not by_node[(n.round, n.index)].is_bye
                and by_node[(n.round, n.index)].team_a_participant_id is not None
                and by_node[(n.round, n.index)].team_b_participant_id is not None]
    tournament.current_round = first_round_number + max(playable, default=0)
    await session.flush()
    return round_rows


def _slot_column(slot):
    return "team_a_participant_id" if slot == "a" else "team_b_participant_id"


def _decided_winner(match, a_wins, b_wins):
    """The participant id that wins `match` on this score, or None for a draw."""
    if a_wins == b_wins:
        return None
    return match.team_a_participant_id if a_wins > b_wins else match.team_b_participant_id


def _refuse_flip_in_closed_event(tournament, old_winner, new_winner):
    """A completed tournament has been announced and, on a money event, paid
    out from its bracket: only the score may follow a draft that finishes after
    the close, never the winner."""
    if (tournament.status != "completed" or old_winner is None
            or new_winner != old_winner):
        raise ValueError(
            f"'{tournament.name}' is {tournament.status} — a finished "
            "tournament's playoff results are final."
        )


def _stored_winner(match):
    """The winner of a match's recorded result, or None if undecided or drawn."""
    if match.team_a_wins is None:
        return None
    return _decided_winner(match, match.team_a_wins, match.team_b_wins)


async def _advance_into_parent(session, match, new_winner):
    """Move `match`'s winner into its parent's feeds_slot. The same team already
    in either slot is a no-op; changing a filled slot is refused once the parent
    has a room, a linked draft or a result."""
    if match.feeds_match_id is None:
        return
    parent = await session.get(TournamentMatch, match.feeds_match_id)
    slots = (parent.team_a_participant_id, parent.team_b_participant_id)
    if new_winner is not None and new_winner in slots:
        return
    column = _slot_column(match.feeds_slot)
    current = getattr(parent, column)
    if current == new_winner:
        return
    if current is not None:
        if parent.pairings_message_id is not None or parent.thread_id is not None:
            raise ValueError(
                f"#{parent.id} is already open with the previous winner — this "
                f"result can change its score but not its winner.")
        if parent.team_a_wins is not None:
            raise ValueError(
                f"#{parent.id} already has a result — this result can change "
                f"its score but not its winner.")
        linked = (await session.execute(
            select(DraftSession.id).where(DraftSession.tournament_match_id == parent.id)
            .limit(1))).first()
        if linked is not None:
            raise ValueError(
                f"#{parent.id} has a linked draft — this result can change "
                f"its score but not its winner.")
    setattr(parent, column, new_winner)
    if parent.team_a_participant_id is not None and parent.team_b_participant_id is not None:
        parent_round = await session.get(TournamentRound, parent.round_id)
        tournament = await session.get(Tournament, parent_round.tournament_id)
        tournament.current_round = max(tournament.current_round, parent_round.round_number)


def _winner_loser(match):
    """(winner_id, loser_id) for a decided playoff match; loser is None for a bye.

    Advancement uses _decided_winner; get_final_placement skips drawn matches
    before calling this, so the raise on a draw is a defensive assertion that
    keeps a level score from ever falling through to "team B won".
    """
    if match.is_bye:
        return match.team_a_participant_id, None
    if match.team_a_wins == match.team_b_wins:
        raise ValueError(
            f"Match {match.id} is a draw ({match.team_a_wins}-{match.team_b_wins}); "
            "a single-elimination match needs a decisive result before the bracket "
            "can advance."
        )
    if match.team_a_wins > match.team_b_wins:
        return match.team_a_participant_id, match.team_b_participant_id
    return match.team_b_participant_id, match.team_a_participant_id


async def start_tournament(session, tournament_id, rng):
    """Activate a tournament and create its first round(s).

    Returns a flat list of the matches created. Swiss pairs round 1 only (later
    rounds via advance_round); round_robin builds the entire schedule up front,
    all rounds open at once.
    """
    tournament = await session.get(Tournament, tournament_id)
    if tournament is None:
        raise ValueError("Tournament not found.")
    if tournament.status != "registration":
        raise ValueError(f"'{tournament.name}' is already {tournament.status}.")
    # Only teams that completed registration (escrow paid) are seeded. Free
    # tournaments mark everyone 'paid', so this is a no-op there. Asked through
    # _pairable, because one definition of who a round pairs is the whole point
    # of having it -- nothing can have dropped before a tournament is active, so
    # this is the same list either way, and it stays the same list if that ever
    # changes.
    everyone = await list_participants(session, tournament_id)
    paid = _pairable(everyone)
    if len(paid) < 2:
        raise ValueError(
            "At least 2 teams must have completed registration (entry fee paid) to start."
        )

    tournament.status = "active"
    if tournament.format == "round_robin":
        matches = await _build_round_robin(session, tournament, paid, rng)
    elif tournament.format == "manual":
        matches = await _open_manual_schedule(session, tournament)
    else:
        _, matches = await _create_round_with_pairings(
            session, tournament, paid, set(), rng
        )
    # Drawn after the schedule so it consumes nothing the pairing reads: a
    # seeded rng pairs round one exactly as it did before draw numbers existed.
    # Over every row the standings rank, so none is left undrawn.
    for participant, number in zip(everyone, rng.sample(range(1, len(everyone) + 1), len(everyone))):
        participant.draw_number = number
    return matches


async def add_match(session, tournament_id, team_a_name, team_b_name):
    """Author one match for a manual tournament (before it starts).

    Resolves team names to registered participants and packs the match into a
    round capped at MANUAL_ROUND_SIZE (so each pairings message stays under the
    Discord button limit). Returns the created match.
    """
    tournament = await session.get(Tournament, tournament_id)
    if tournament is None:
        raise ValueError("Tournament not found.")
    if tournament.format != "manual":
        raise ValueError("Matches are only authored by hand for manual tournaments.")
    if tournament.status != "registration":
        raise ValueError("Add matches before starting the tournament.")

    a = await find_participant_by_name(session, tournament_id, team_a_name)
    b = await find_participant_by_name(session, tournament_id, team_b_name)
    if a is None or b is None:
        missing = team_a_name if a is None else team_b_name
        raise ValueError(f"'{missing}' is not registered for this tournament.")
    if a.id == b.id:
        raise ValueError("A team can't be scheduled against itself.")
    unpaid = [p.team_name for p in (a, b) if p.status != "paid"]
    if unpaid:
        raise ValueError(
            f"{' and '.join(unpaid)} hasn't completed registration (entry fee unpaid)."
        )

    rounds = (await session.execute(
        select(TournamentRound)
        .where(TournamentRound.tournament_id == tournament_id)
        .order_by(TournamentRound.round_number)
    )).scalars().all()
    target = rounds[-1] if rounds else None
    if target is not None:
        count = (await session.execute(
            select(func.count()).select_from(TournamentMatch).where(
                TournamentMatch.round_id == target.id
            )
        )).scalar_one()
        if count >= MANUAL_ROUND_SIZE:
            target = None
    if target is None:
        target = TournamentRound(tournament_id=tournament_id, round_number=len(rounds) + 1)
        session.add(target)
        await session.flush()

    match = TournamentMatch(
        round_id=target.id,
        team_a_participant_id=a.id,
        team_b_participant_id=b.id,
    )
    session.add(match)
    await session.flush()
    return match


async def _open_manual_schedule(session, tournament):
    """Activate a manual tournament by opening its pre-authored matches."""
    rounds = (await session.execute(
        select(TournamentRound).where(TournamentRound.tournament_id == tournament.id)
    )).scalars().all()
    if not rounds:
        raise ValueError("Add matches with /tournament add_match before starting.")
    matches = (await session.execute(
        select(TournamentMatch)
        .join(TournamentRound, TournamentMatch.round_id == TournamentRound.id)
        .where(TournamentRound.tournament_id == tournament.id)
    )).scalars().all()
    tournament.total_rounds = len(rounds)
    tournament.current_round = len(rounds)
    await session.flush()
    return matches


async def _build_round_robin(session, tournament, participants, rng):
    """Create every round of a single round-robin at once (no byes). All open."""
    schedule = round_robin_schedule([p.id for p in participants], rng)
    all_matches = []
    for round_number, pairs in enumerate(schedule, start=1):
        new_round = TournamentRound(tournament_id=tournament.id, round_number=round_number)
        session.add(new_round)
        await session.flush()
        for id_a, id_b in pairs:
            match = TournamentMatch(
                round_id=new_round.id,
                team_a_participant_id=id_a,
                team_b_participant_id=id_b,
            )
            session.add(match)
            all_matches.append(match)
    tournament.total_rounds = len(schedule)
    tournament.current_round = len(schedule)  # all rounds revealed at once
    await session.flush()
    return all_matches


async def finish_tournament(session, tournament_id):
    """End an active tournament now. Returns the champion participant (top of
    final placement — bracket order if a cut was played, standings otherwise),
    or None if there are none.

    A team that dropped is skipped: it keeps its place in the placement, because
    its record still counts for everyone it played, but a team that walked away
    is not what the tournament announces as its winner. Payout draws the same
    line, and the two must not disagree about who won."""
    tournament = await session.get(Tournament, tournament_id)
    if tournament is None:
        raise ValueError("Tournament not found.")
    if tournament.status != "active":
        raise ValueError(f"'{tournament.name}' is not active.")
    # A drawn bracket match advances nobody and placement skips it, so finishing
    # now would crown (and pay) whoever seeds higher -- and a completed
    # tournament can no longer be corrected.
    drawn = (await session.execute(
        _playable_bracket_stmt(tournament_id, TournamentMatch.id)
        .where(TournamentMatch.team_a_wins.isnot(None),
               TournamentMatch.team_a_wins == TournamentMatch.team_b_wins)
        .order_by(TournamentMatch.id))).scalars().all()
    if drawn:
        tags = ", ".join(f"#{mid}" for mid in drawn)
        raise ValueError(
            f"{tags} {'is' if len(drawn) == 1 else 'are'} drawn — settle "
            f"{'it' if len(drawn) == 1 else 'them'} with /tournament set_result "
            f"before finishing.")
    tournament.status = "completed"
    await session.flush()
    placement = [p for p in await get_final_placement(session, tournament_id)
                 if p.dropped_at is None]
    return placement[0] if placement else None


async def set_result(session, match_id, team_a_wins, team_b_wins):
    """Record or correct a match result (admin override path); returns the match.

    Correction-safe: if the match already has a result, the old stats are
    reverted before the new ones are applied.
    """
    match, _completed_now = await record_result(session, match_id, team_a_wins, team_b_wins)
    return match


async def record_result(session, match_id, team_a_wins, team_b_wins):
    """set_result, also reporting whether THIS call completed the tournament
    (the decided final of an active bracket) -- the one call that should
    announce the champion."""
    if team_a_wins < 0 or team_b_wins < 0:
        raise ValueError("Game wins cannot be negative.")
    match = await session.get(TournamentMatch, match_id)
    if match is None:
        raise ValueError("Match not found.")
    round_ = await session.get(TournamentRound, match.round_id)
    playoff_round = is_playoff(round_)
    completed_now = False
    if match.is_bye:
        # A swiss bye is a RESULT (points awarded); a bracket bye is the
        # absence of a match. Neither can be reported, but saying "scored
        # automatically" about a bracket bye tells the organizer the opposite
        # of what the code does — nothing is scored there.
        raise ValueError(
            "That team has a bye this round — there is no match to report."
            if playoff_round
            else "Byes are scored automatically and cannot be reported."
        )
    if playoff_round:
        # A completed tournament has been announced and, on a money event,
        # paid out from this very bracket. record_linked_result writes to any
        # match id whenever a linked draft finishes, so a draft that lands
        # after /tournament finish would otherwise rewrite the champion of a
        # closed event. The later-round guard below cannot catch it: the final
        # has no later round.
        tournament = await session.get(Tournament, round_.tournament_id)
        # Lock the tournament first: the copies read so far may be stale.
        # rowcount 0 means it is no longer active.
        locked = await session.execute(
            update(Tournament)
            .where(Tournament.id == round_.tournament_id, Tournament.status == "active")
            .values(status=Tournament.status)
            .execution_options(synchronize_session=False))
        still_active = locked.rowcount == 1
        if tournament is not None:
            await session.refresh(tournament)
        await session.refresh(match)
        if match.feeds_match_id is not None:
            await session.refresh(await session.get(TournamentMatch, match.feeds_match_id))
        if match.team_a_participant_id is None or match.team_b_participant_id is None:
            raise ValueError(f"#{match.id} is still waiting for its teams — "
                             "report the matches that feed it first.")
        old_winner = _stored_winner(match)
        new_winner = _decided_winner(match, team_a_wins, team_b_wins)
        if tournament is not None and not still_active:
            _refuse_flip_in_closed_event(tournament, old_winner, new_winner)
        else:
            if (tournament is not None and match.feeds_match_id is None
                    and new_winner is not None):
                # The final is decided. Conditional on still being active, so
                # of any two completions racing, exactly one sees rowcount 1.
                done = await session.execute(
                    update(Tournament)
                    .where(Tournament.id == tournament.id, Tournament.status == "active")
                    .values(status="completed")
                    .execution_options(synchronize_session=False))
                completed_now = done.rowcount == 1
                await session.refresh(tournament)
            await _advance_into_parent(session, match, new_winner)

    if round_ is not None and not playoff_round:
        # Match ids span the server, so a swiss result can be aimed at a finished
        # event or a week before the cut; both would rewrite settled standings.
        tournament = await session.get(Tournament, round_.tournament_id)
        if tournament is not None and await _swiss_frozen(session, tournament):
            if tournament.status != "active":
                raise ValueError(
                    f"'{tournament.name}' is {tournament.status} — its Swiss "
                    "results are final."
                )
            raise ValueError(
                "Swiss records froze at the cut — a Swiss result cannot be "
                "changed once the playoff has started."
            )

    part_a = await session.get(TournamentParticipant, match.team_a_participant_id)
    part_b = await session.get(TournamentParticipant, match.team_b_participant_id)

    # Swiss records freeze at the cut: a playoff result is recorded on the
    # match and drives the bracket, but never moves points/W-L/OMW%.
    if round_ is not None and not playoff_round:
        if match.team_a_wins is not None:
            _apply_result(part_a, part_b, match.team_a_wins, match.team_b_wins, sign=-1)
        _apply_result(part_a, part_b, team_a_wins, team_b_wins, sign=1)
    match.team_a_wins = team_a_wins
    match.team_b_wins = team_b_wins
    await session.flush()
    return match, completed_now


async def record_linked_result(tournament_match_id, team_a_wins, team_b_wins):
    """Record a result coming from a linked premade draft's completion.

    Opens its own session because the caller (the draft victory chokepoint in
    utils.py) holds an unrelated transaction. Side A of the draft is side A of
    the match — the launcher pre-names the draft teams from the pairing.
    Correction-safe via set_result, so a draft finishing after an admin ruling
    (or a re-finalization) replaces rather than double-counts.
    """
    async with db_session() as session:
        return await set_result(session, tournament_match_id, team_a_wins, team_b_wins)


async def sync_linked_result(tournament_match_id, team_a_wins, team_b_wins):
    """Keep a linked match's score equal to its draft's current score.

    A draft clinches once, but its remaining games are still played and
    reported afterwards, so the score at the clinch is usually not the final
    one. The victory chokepoint runs on every report, so this runs there too
    and writes only when the stored score actually moved -- a match that grew
    past its clinch ends up holding the true score, without re-posting the
    standings for the reports that changed nothing.

    Returns (match, completed_now) when it wrote, or None when the score already
    matched; completed_now is True only for the write that completed the tournament.
    """
    async with db_session() as session:
        match = await session.get(TournamentMatch, tournament_match_id)
        if match is None:
            raise ValueError("Match not found.")
        if (match.team_a_wins, match.team_b_wins) == (team_a_wins, team_b_wins):
            return None
        # set_result guards a finished tournament for playoff rounds only. That
        # was enough while this ran once, at the clinch: a swiss match could not
        # be reached after the event closed. It can now, on any late-landing
        # report, so refuse here too rather than restate a closed event. An
        # organiser correcting a result by hand still goes through set_result
        # and is still allowed to.
        tournament_id = await get_tournament_id_for_match(session, tournament_match_id)
        tournament = (await session.get(Tournament, tournament_id)
                      if tournament_id is not None else None)
        if tournament is not None and tournament.status != "active":
            # A bracket match's score still follows its draft after the close
            # as long as its winner holds; anything else is a closed event.
            round_ = await session.get(TournamentRound, match.round_id)
            stored = _stored_winner(match)
            same_winner = (stored is not None
                           and stored == _decided_winner(match, team_a_wins, team_b_wins))
            if not (is_playoff(round_) and tournament.status == "completed" and same_winner):
                return None
        return await record_result(session, tournament_match_id, team_a_wins, team_b_wins)


def _playable_bracket_stmt(tournament_id, *columns):
    """Select over a tournament's non-bye bracket matches with both teams set."""
    return (select(*columns)
            .join(TournamentRound, TournamentMatch.round_id == TournamentRound.id)
            .where(TournamentRound.tournament_id == tournament_id,
                   TournamentRound.stage.in_(BRACKET_STAGES),
                   TournamentMatch.is_bye.is_(False),
                   TournamentMatch.team_a_participant_id.is_not(None),
                   TournamentMatch.team_b_participant_id.is_not(None)))


async def ready_bracket_matches(session, tournament_id):
    """Bracket matches that can be played and have not been posted yet, as
    (match_id, team_a_name, team_b_name, stage_name), in match-id order."""
    # The formatter imports this module, so it cannot be imported at the top.
    from services.tournament_formatter import round_name
    rows = (await session.execute(
        _playable_bracket_stmt(tournament_id, TournamentMatch, TournamentRound)
        .where(TournamentMatch.team_a_wins.is_(None),
               TournamentMatch.pairings_message_id.is_(None))
        .order_by(TournamentMatch.id))).all()
    tournament = await session.get(Tournament, tournament_id)
    if tournament is None or tournament.status != "active":
        return []         # no rooms in a closed event
    out = []
    for match, round_ in rows:
        stage = await round_name(session, round_, tournament.total_rounds)
        part_a = await session.get(TournamentParticipant, match.team_a_participant_id)
        part_b = await session.get(TournamentParticipant, match.team_b_participant_id)
        out.append((match.id, part_a.team_name, part_b.team_name, stage))
    return out


async def roomless_posted_bracket_matches(session, tournament_id):
    """Ids of playable bracket matches whose pairing line is posted but whose
    room never opened, in match-id order."""
    tournament = await session.get(Tournament, tournament_id)
    if tournament is None or tournament.status != "active":
        return []
    return list((await session.execute(
        _playable_bracket_stmt(tournament_id, TournamentMatch.id)
        .where(TournamentMatch.team_a_wins.is_(None),
               TournamentMatch.pairings_message_id.is_not(None),
               TournamentMatch.thread_id.is_(None))
        .order_by(TournamentMatch.id))).scalars().all())


async def get_tournament_id_for_match(session, match_id):
    """Resolve a match's tournament id (match -> round -> tournament), or None."""
    match = await session.get(TournamentMatch, match_id)
    if match is None:
        return None
    round_ = await session.get(TournamentRound, match.round_id)
    return round_.tournament_id if round_ else None


async def _current_round(session, tournament):
    stmt = select(TournamentRound).where(
        TournamentRound.tournament_id == tournament.id,
        TournamentRound.round_number == tournament.current_round,
    )
    return (await session.execute(stmt)).scalars().first()


async def current_round_stage(session, tournament):
    """The stage of the round a tournament is currently on.

    STAGE_SWISS when it has no rounds yet, which is what a registration-status
    board wants. Read off the round rather than inferred from
    `current_round > total_rounds`: that arithmetic happens to agree today and
    stops agreeing the moment any other stage exists.
    """
    round_ = await _current_round(session, tournament)
    return round_.stage if round_ is not None else STAGE_SWISS


async def _round_matches(session, round_id):
    """A round's matches, in creation order.

    The order is load-bearing in the bracket -- creation order IS bracket order,
    which is the invariant bracket_tree rests on -- and free for the swiss
    callers, which only ask which matches are unreported.
    """
    stmt = (
        select(TournamentMatch)
        .where(TournamentMatch.round_id == round_id)
        .order_by(TournamentMatch.id)
    )
    return (await session.execute(stmt)).scalars().all()


async def find_current_match(session, tournament_id, team_name):
    """Find the current-round match involving the named team, or None."""
    tournament = await session.get(Tournament, tournament_id)
    if tournament is None or tournament.current_round == 0:
        return None
    stmt = select(TournamentParticipant).where(
        TournamentParticipant.tournament_id == tournament_id,
        func.lower(TournamentParticipant.team_name) == team_name.strip().lower(),
    )
    participant = (await session.execute(stmt)).scalars().first()
    if participant is None:
        return None
    round_ = await _current_round(session, tournament)
    for match in await _round_matches(session, round_.id):
        if participant.id in (match.team_a_participant_id, match.team_b_participant_id):
            return match
    return None


async def match_in_guild(session, match_id, guild_id):
    """The tournament match with this id, or None unless it belongs to a
    tournament on this server. Admins enter results by the id shown on the
    pairing line, so the id is checked against the server it was typed in."""
    return (await session.execute(
        select(TournamentMatch)
        .join(TournamentRound, TournamentMatch.round_id == TournamentRound.id)
        .join(Tournament, TournamentRound.tournament_id == Tournament.id)
        .where(TournamentMatch.id == match_id, Tournament.guild_id == str(guild_id))
    )).scalars().first()


AUTOCOMPLETE_LIMIT = 25  # Discord's cap on suggestions per autocomplete


async def reportable_match_choices(session, guild_id, typed=""):
    """(match_id, label) rows for /tournament set_result's `match` autocomplete.

    Offers the active tournament's playable matches by name (no byes; swiss only
    until the cut), unreported first then newest; ``typed`` filters the label.
    """
    # Imported here: the formatter imports this module.
    from services.tournament_formatter import round_label

    tournament = await get_active_tournament(session, guild_id)
    if tournament is None:
        return []
    part_a = aliased(TournamentParticipant)
    part_b = aliased(TournamentParticipant)
    stmt = (
        select(TournamentMatch, TournamentRound, part_a.team_name, part_b.team_name)
        .join(TournamentRound, TournamentMatch.round_id == TournamentRound.id)
        .outerjoin(part_a, TournamentMatch.team_a_participant_id == part_a.id)
        .outerjoin(part_b, TournamentMatch.team_b_participant_id == part_b.id)
        .where(TournamentRound.tournament_id == tournament.id)
    )
    if await _swiss_frozen(session, tournament):
        stmt = stmt.where(TournamentRound.stage.in_(BRACKET_STAGES))
    found = (await session.execute(stmt)).all()
    # A bracket round is named by its match count, byes included.
    in_round = Counter(match.round_id for match, *_ in found)
    needle = typed.strip().lower()
    rows = []
    for match, round_, a_name, b_name in found:
        if match.is_bye or a_name is None or b_name is None:
            continue
        name = round_label(tournament.total_rounds, round_.round_number, round_.stage,
                           swiss_noun="Week", matches_in_round=in_round[round_.id])
        label = f"{match_tag(match.id, name)} · {a_name} vs {b_name}"
        if needle in label.lower():
            rows.append((match.team_a_wins is not None, -match.id, match.id, label))
    rows.sort()
    return [(match_id, label) for _, _, match_id, label in rows[:AUTOCOMPLETE_LIMIT]]


async def advance_round(session, tournament_id, rng):
    """Advance to the next round, or complete the tournament after round N.

    Returns the new TournamentRound. Returns None when the tournament
    completes (end of swiss with no cut declared, or the playoff final has
    just been decided). Raises ValueError while the current round still has
    unreported matches (or, in the bracket, on a drawn match — single
    elimination has no such result). Raises SwissComplete(cut_to, eligible, fillable)
    at the end of swiss instead of completing when a cut IS declared: the
    caller must ask the organizer whether to start the bracket or finish and
    crown the swiss leader, since completing is otherwise irreversible.
    """
    tournament = await session.get(Tournament, tournament_id)
    if tournament is None:
        raise ValueError("Tournament not found.")
    if tournament.status != "active":
        raise ValueError(f"'{tournament.name}' is not active.")
    if tournament.format != "swiss" and not await _playoff_rounds(session, tournament_id):
        raise ValueError(
            "next_round is for Swiss tournaments — this one's schedule is fixed; "
            "use /tournament finish to end it."
        )

    round_ = await _current_round(session, tournament)
    if round_ is None:
        # Completing is irreversible, so the branch that completes must never
        # be the quiet one: with `matches = [] if round_ is None`, a tournament
        # whose current_round pointed at a missing round row sailed through the
        # unreported check and completed itself. The cog's `except ValueError`
        # surfaces this to the organizer instead.
        raise ValueError(
            f"Round {tournament.current_round} has no round row — "
            f"'{tournament.name}' cannot be advanced."
        )
    if is_playoff(round_):
        raise ValueError(
            "The bracket advances on its own as results come in — "
            "/tournament next_round reopens any room that is missing."
        )
    matches = await _round_matches(session, round_.id)
    unreported = [m for m in matches if not m.is_bye and m.team_a_wins is None]
    if unreported:
        raise ValueError(
            f"{len(unreported)} match(es) in round {tournament.current_round} "
            "still need results."
        )

    if tournament.current_round >= tournament.total_rounds:
        if tournament.cut_to:
            standings = await get_standings_data(session, tournament_id)
            raise SwissComplete(
                tournament.cut_to,
                len(_cut_eligible(standings)),
                bool(_seated_by_cut(standings, tournament.cut_to)),
            )
        tournament.status = "completed"
        await session.flush()
        return None

    # Rematch history across all rounds so far (byes excluded)
    stmt = (
        select(TournamentMatch)
        .join(TournamentRound, TournamentMatch.round_id == TournamentRound.id)
        .where(TournamentRound.tournament_id == tournament_id)
    )
    played = (await session.execute(stmt)).scalars().all()
    history = {
        frozenset((m.team_a_participant_id, m.team_b_participant_id))
        for m in played
        if not m.is_bye
    }

    everyone = await list_participants(session, tournament_id)
    new_round, _ = await _create_round_with_pairings(
        session, tournament, _pairable(everyone), history, rng, played, everyone
    )
    return new_round


async def get_standings_data(session, tournament_id):
    """Participants ranked by ``swiss.rank_standings``; the key, and why game
    differential is not part of it, live there.

    OMW% (opponents' match-win %, byes excluded) needs the full match graph, so
    we load participants and matches and rank in memory (tournaments are small).

    Swiss rounds only. Records freeze at the cut, but OMW% is the FIRST
    tiebreak and is computed from the opponent graph, so letting bracket
    pairings into it would reorder two tied teams the instant the bracket is
    paired — the standings would contradict the seeds just announced.
    """
    ranked, _ = await get_standings_with_omw(session, tournament_id)
    return ranked


async def get_standings_with_omw(session, tournament_id):
    """``(ranked participants, {participant id: OMW%})`` from one load.

    The board has to show the tiebreak it sorted by, and the renderer cannot
    derive it -- OMW% needs the whole match graph, which only this layer has.
    Returning both from the same read is what keeps the number displayed and
    the number sorted on identical to each other.
    """
    participants = (await session.execute(
        select(TournamentParticipant).where(
            TournamentParticipant.tournament_id == tournament_id
        )
    )).scalars().all()
    matches = (await session.execute(
        select(TournamentMatch)
        .join(TournamentRound, TournamentMatch.round_id == TournamentRound.id)
        .where(TournamentRound.tournament_id == tournament_id)
        .where(TournamentRound.stage.notin_(BRACKET_STAGES))
    )).scalars().all()
    # One derivation, passed to both: the map the board prints is the map the
    # sort ranked on, by construction rather than by the two calls happening
    # to carry identical arguments.
    omw = omw_percentages(participants, matches)
    return rank_standings(participants, matches, omw), omw


async def get_final_placement(session, tournament_id):
    """Participants in finishing order, best first.

    Bracket placement when a cut was played, plain standings otherwise — so a
    tournament with no cut behaves exactly as it always has, and callers
    (payout, the champion announcement) never learn what a bracket is.

    Every decided match counts, even in a round still waiting on others:
    `/tournament finish` can end a tournament with the bracket mid-stream, and
    `final_placement` ranks any team that never lost above every eliminated
    team, so a live team is never mistaken for one that missed the cut.
    Draws and undecided matches advance nobody and count for nothing.
    """
    standings = await get_standings_data(session, tournament_id)
    rounds = await _playoff_rounds(session, tournament_id)
    if not rounds:
        return standings

    by_id = {p.id: p for p in standings}
    seeds = {p.id: p.seed for p in standings if p.seed is not None}
    # Rounds finish unevenly, so every decided match counts, whatever else its
    # round is still waiting on. A later match only has teams once its feeders
    # are decided, so a later result can always be trusted.
    results = []
    for round_ in rounds:
        pairs = []
        for m in await _round_matches(session, round_.id):
            if m.is_bye:
                pairs.append((m.team_a_participant_id, None))
            elif (m.team_a_wins is not None and m.team_a_participant_id is not None
                  and m.team_b_participant_id is not None
                  and m.team_a_wins != m.team_b_wins):
                pairs.append(_winner_loser(m))
        if pairs:
            results.append(pairs)
    # Seeded teams with no decided match yet are still alive: they rank with
    # the never-beaten, above everyone eliminated (final_placement orders
    # never-lost teams by seed).
    placed = {pid for rnd in results for pair in rnd for pid in pair if pid is not None}
    waiting = [(pid, None) for pid in seeds if pid not in placed]
    if waiting:
        results.insert(0, waiting)

    ordered = [by_id[pid] for pid in final_placement(results, seeds) if pid in by_id]
    ranked_ids = {p.id for p in ordered}
    # Teams that missed the cut rank below every bracket team, in swiss order.
    return ordered + [p for p in standings if p.id not in ranked_ids]


async def count_unreported_matches(session, tournament_id):
    """Non-bye matches in this tournament with no result yet (team_a_wins IS NULL).

    A tournament can be finished (status='completed') with matches still unreported — those
    count as 0-0, so standings aren't truly final. Payout surfaces this before disbursing.
    """
    stmt = (
        select(func.count())
        .select_from(TournamentMatch)
        .join(TournamentRound, TournamentMatch.round_id == TournamentRound.id)
        .where(
            TournamentRound.tournament_id == tournament_id,
            TournamentMatch.is_bye.is_(False),
            TournamentMatch.team_a_wins.is_(None),
        )
    )
    return int((await session.execute(stmt)).scalar_one())


async def tournament_match_is_unfinished(session, match_id):
    """True iff the tournament match exists, isn't a bye, and has no result yet."""
    if not match_id:
        return False
    match = await session.get(TournamentMatch, int(match_id))
    if match is None or match.is_bye:
        return False
    return match.team_a_wins is None


async def extend_deletion_if_unfinished(session, draft_session, now):
    """Cleanup guard: if the draft's tournament match is still unfinished, push its
    deletion_time out (7 days) so cleanup won't reap it mid-match. Returns True when
    the session should be skipped by cleanup, False otherwise."""
    from datetime import timedelta
    if await tournament_match_is_unfinished(session, draft_session.tournament_match_id):
        draft_session.deletion_time = now + timedelta(days=7)
        return True
    return False


async def store_role_ids(role_ids: dict[int, str]) -> None:
    """Persist each team's role id after a successful start."""
    if not role_ids:
        return
    async with db_session() as session:
        for participant_id, role_id in role_ids.items():
            participant = await session.get(TournamentParticipant, participant_id)
            if participant is None:
                # The participant dropped (with a refund) in the gap between
                # _create_roles_for_start's read and the money-locked start --
                # its role was already created and assigned, and now nothing
                # will ever record its id. Log it so it can be found by hand;
                # every other skip in this feature does the same.
                logger.warning(
                    f"[team-roles] participant {participant_id} is gone; "
                    f"role {role_id} was created but cannot be recorded"
                )
                continue
            participant.role_id = role_id
