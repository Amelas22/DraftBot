"""Service layer for team-based Swiss tournaments.

Slice 1: create/register/view. Slice 2: start, Swiss rounds, admin-set results,
standings.

All functions take an AsyncSession so callers control the transaction and tests
can point them at a temp database (mirrors the leaderboard_service convention).
"""
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError

from database.db_session import db_session
from draft_organization.bracket import advance_pairs, build_bracket, final_placement
from draft_organization.swiss import pair_round, rank_standings, round_robin_schedule
from models.team import Team
from models.tournament import (
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
    """True when a round is a bracket round.

    The one place a stage value is interpreted. It used to be spelled out as
    `== "playoff"` in three places and `!= "playoff"` in a fourth, so any third
    stage would have been included by one predicate and excluded by the other.
    """
    return round_ is not None and round_.stage == STAGE_PLAYOFF


def _cut_eligible(standings):
    """The teams a cut can seat: those that completed registration (escrow paid).

    One rule, because the end-of-swiss prompt disables its Start button on this
    count while start_playoff refuses on this list -- if the two drift, the
    prompt offers a button that then refuses, which is the failure the disabled
    state exists to prevent.
    """
    return [p for p in standings if p.status == "paid"]


class SwissComplete(Exception):
    """Swiss is over and a cut is declared, so the next step is a choice:
    start the bracket, or finish and crown the swiss leader.

    Raised rather than returned because `advance_round` completing the
    tournament is irreversible and one call away — the caller must decide.
    """

    def __init__(self, cut_to, eligible):
        super().__init__(f"Swiss complete; cut to top {cut_to} is pending.")
        self.cut_to = cut_to
        self.eligible = eligible


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


async def _create_round_with_pairings(session, tournament, participants, history, rng):
    """Create the next round row and its matches; auto-scores the bye."""
    round_number = tournament.current_round + 1
    new_round = TournamentRound(tournament_id=tournament.id, round_number=round_number)
    session.add(new_round)
    await session.flush()

    teams = [{"id": p.id, "points": p.points, "byes": p.byes} for p in participants]
    pairs, bye_id = pair_round(teams, history, rng)
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
        .where(TournamentRound.stage == STAGE_PLAYOFF)
        .order_by(TournamentRound.round_number)
    )
    return (await session.execute(stmt)).scalars().all()


async def start_playoff(session, tournament_id, size=None):
    """Cut to the top `size` and create the first playoff round.

    Seeds are stamped from final swiss standings and never recomputed: they
    are the numbers players were told, and they are what orders teams that
    went out at the same depth.
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
    eligible = _cut_eligible(standings)
    if len(eligible) < size:
        raise ValueError(
            f"Only {len(eligible)} eligible team(s) — can't cut to top {size}. "
            f"Re-run with a smaller `top:`."
        )

    cut = eligible[:size]
    for position, participant in enumerate(cut, start=1):
        participant.seed = position
    by_seed = {position: p.id for position, p in enumerate(cut, start=1)}

    pairs = [
        (by_seed[seed_a], None if seed_b is None else by_seed[seed_b])
        for seed_a, seed_b in build_bracket(size)
    ]
    return await _create_playoff_round(
        session, tournament, tournament.total_rounds + 1, pairs)


async def _create_playoff_round(session, tournament, round_number, pairs):
    """Create a bracket round and its matches, and move the tournament onto it.

    ``pairs`` is (participant_id, partner_id_or_None) in bracket order; a None
    partner is a bye. Deliberately NOT merged with _create_round_with_pairings:
    a swiss round pairs itself from standings and SCORES its bye, and a bracket
    bye is the absence of a match -- nothing is awarded, because swiss records
    are frozen at the cut.
    """
    new_round = TournamentRound(
        tournament_id=tournament.id, round_number=round_number, stage=STAGE_PLAYOFF
    )
    session.add(new_round)
    await session.flush()
    for id_a, id_b in pairs:
        session.add(TournamentMatch(
            round_id=new_round.id,
            team_a_participant_id=id_a,
            team_b_participant_id=id_b,
            is_bye=id_b is None,
        ))
    tournament.current_round = round_number
    await session.flush()
    return new_round


def _winner_loser(match):
    """(winner_id, loser_id) for a decided playoff match; loser is None for a bye.

    Raises ValueError on a draw -- single elimination has no drawn match, and
    both the advancement and the payout order depend on this answer being the
    same one. The two copies of this decision had already drifted: the
    placement copy fell through to "team B won" on a draw, so /tournament
    finish could complete a tournament -- and pay it out -- on a team nobody
    beat.
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


async def _advance_playoff(session, tournament, last_round):
    """Pair the winners of `last_round` into the next bracket round, or
    complete the tournament when the final has been decided.

    `last_round` is the round advance_round already fetched and checked for
    unreported matches -- the same object, so this does not re-check it and
    cannot disagree with it.
    """
    winners = [_winner_loser(m)[0] for m in await _round_matches(session, last_round.id)]
    if len(winners) == 1:
        tournament.status = "completed"
        await session.flush()
        return None
    return await _create_playoff_round(
        session, tournament, last_round.round_number + 1, advance_pairs(winners))


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
    participants = await list_participants(session, tournament_id)
    # Only teams that completed registration (escrow paid) are seeded. Free
    # tournaments mark everyone 'paid', so this is a no-op there.
    paid = [p for p in participants if p.status == "paid"]
    if len(paid) < 2:
        raise ValueError(
            "At least 2 teams must have completed registration (entry fee paid) to start."
        )

    tournament.status = "active"
    if tournament.format == "round_robin":
        return await _build_round_robin(session, tournament, paid, rng)
    if tournament.format == "manual":
        return await _open_manual_schedule(session, tournament)

    _, matches = await _create_round_with_pairings(
        session, tournament, paid, set(), rng
    )
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
    or None if there are none."""
    tournament = await session.get(Tournament, tournament_id)
    if tournament is None:
        raise ValueError("Tournament not found.")
    if tournament.status != "active":
        raise ValueError(f"'{tournament.name}' is not active.")
    tournament.status = "completed"
    await session.flush()
    placement = await get_final_placement(session, tournament_id)
    return placement[0] if placement else None


async def set_result(session, match_id, team_a_wins, team_b_wins):
    """Record or correct a match result (admin override path).

    Correction-safe: if the match already has a result, the old stats are
    reverted before the new ones are applied.
    """
    if team_a_wins < 0 or team_b_wins < 0:
        raise ValueError("Game wins cannot be negative.")
    match = await session.get(TournamentMatch, match_id)
    if match is None:
        raise ValueError("Match not found.")
    round_ = await session.get(TournamentRound, match.round_id)
    playoff_round = is_playoff(round_)
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
        if tournament is not None and tournament.status != "active":
            raise ValueError(
                f"'{tournament.name}' is {tournament.status} — a finished "
                "tournament's playoff results are final."
            )
        # A bracket round closes the moment the next one is paired: its winners
        # are already playing on. Rewriting it would recompute placement from a
        # contradictory bracket — the round-1 loser could end up "champion", and
        # a team that lost twice could be paid two prize slots.
        latest = (await _playoff_rounds(session, round_.tournament_id))[-1]
        if latest.id != round_.id:
            raise ValueError(
                f"Playoff round {round_.round_number} is already decided; its "
                "result cannot be changed once a later bracket round exists."
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
    return match


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
    which is the invariant advance_pairs rests on -- and free for the swiss
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


async def advance_round(session, tournament_id, rng):
    """Advance to the next round, or complete the tournament after round N.

    Returns the new TournamentRound. Returns None when the tournament
    completes (end of swiss with no cut declared, or the playoff final has
    just been decided). Raises ValueError while the current round still has
    unreported matches (or, in the bracket, on a drawn match — single
    elimination has no such result). Raises SwissComplete(cut_to, eligible)
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
    matches = await _round_matches(session, round_.id)
    unreported = [m for m in matches if not m.is_bye and m.team_a_wins is None]
    if unreported:
        raise ValueError(
            f"{len(unreported)} match(es) in round {tournament.current_round} "
            "still need results."
        )

    if is_playoff(round_):
        return await _advance_playoff(session, tournament, round_)

    if tournament.current_round >= tournament.total_rounds:
        if tournament.cut_to:
            standings = await get_standings_data(session, tournament_id)
            raise SwissComplete(tournament.cut_to, len(_cut_eligible(standings)))
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

    participants = await list_participants(session, tournament_id)
    new_round, _ = await _create_round_with_pairings(
        session, tournament, participants, history, rng
    )
    return new_round


async def get_standings_data(session, tournament_id):
    """Participants ranked by points, then OMW%, then game diff, then name.

    OMW% (opponents' match-win %, byes excluded) needs the full match graph, so
    we load participants and matches and rank in memory (tournaments are small).

    Swiss rounds only. Records freeze at the cut, but OMW% is the FIRST
    tiebreak and is computed from the opponent graph, so letting bracket
    pairings into it would reorder two tied teams the instant the bracket is
    paired — the standings would contradict the seeds just announced.
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
        .where(TournamentRound.stage != STAGE_PLAYOFF)
    )).scalars().all()
    return rank_standings(participants, matches)


async def get_final_placement(session, tournament_id):
    """Participants in finishing order, best first.

    Bracket placement when a cut was played, plain standings otherwise — so a
    tournament with no cut behaves exactly as it always has, and callers
    (payout, the champion announcement) never learn what a bracket is.

    A round counts as played only when every one of its non-bye matches has
    a result. `/tournament finish` can end a tournament with the bracket
    mid-stream, so we stop at the first incomplete round rather than skip
    past individual unreported matches within it — a later round's results
    cannot be trusted once an earlier one is incomplete, and `final_placement`
    ranks any team that never lost in the rounds given above every eliminated
    team, so a live team is never mistaken for one that missed the cut.
    """
    standings = await get_standings_data(session, tournament_id)
    rounds = await _playoff_rounds(session, tournament_id)
    if not rounds:
        return standings

    by_id = {p.id: p for p in standings}
    seeds = {p.id: p.seed for p in standings if p.seed is not None}
    results = []
    for round_ in rounds:
        matches = await _round_matches(session, round_.id)
        playable = [m for m in matches if not m.is_bye]
        if any(m.team_a_wins is None for m in playable):
            break
        pairs = [_winner_loser(m) for m in matches]
        if pairs:
            results.append(pairs)

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


async def store_role_ids(role_ids: dict) -> None:
    """Persist each team's role id after a successful start."""
    if not role_ids:
        return
    async with db_session() as session:
        for participant_id, role_id in role_ids.items():
            participant = await session.get(TournamentParticipant, participant_id)
            if participant is not None:
                participant.role_id = role_id
