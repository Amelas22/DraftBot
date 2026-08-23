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
    Tournament,
    TournamentMatch,
    TournamentParticipant,
    TournamentRound,
    TournamentTeamMember,
)

ACTIVE_STATUSES = ("registration", "active")
POINTS_WIN = 3
POINTS_DRAW = 1


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
                            payout_structure="winner_take_all"):
    """Create a tournament in registration status.

    ``format`` is 'swiss', 'round_robin', or 'manual'. For the all-open formats
    total_rounds is set at start (derived from the schedule), so callers may
    pass 0. ``entry_fee`` is the per-team escrow in tix (0 = free); ``payout_structure``
    is how the pool splits at payout. Raises ValueError if the guild already has a
    registration/active tournament (one active per guild keeps other commands argument-free).
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
    # The roster has no ORM relationship to cascade through (deliberately — lazy
    # loading a relationship on an async session is a footgun), so its rows are
    # cleared explicitly. Without this they would outlive the team that owned them.
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
        .where(TournamentRound.stage == "playoff")
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

    standings = await get_standings_data(session, tournament_id)
    eligible = [p for p in standings if p.status == "paid"]
    if len(eligible) < size:
        raise ValueError(
            f"Only {len(eligible)} eligible team(s) — can't cut to top {size}. "
            f"Re-run with a smaller `top:`."
        )

    cut = eligible[:size]
    for position, participant in enumerate(cut, start=1):
        participant.seed = position
    by_seed = {position: p.id for position, p in enumerate(cut, start=1)}

    round_number = tournament.total_rounds + 1
    new_round = TournamentRound(
        tournament_id=tournament.id, round_number=round_number, stage="playoff"
    )
    session.add(new_round)
    await session.flush()

    for seed_a, seed_b in build_bracket(size):
        session.add(TournamentMatch(
            round_id=new_round.id,
            team_a_participant_id=by_seed[seed_a],
            team_b_participant_id=None if seed_b is None else by_seed[seed_b],
            # A bracket bye is the ABSENCE of a match, not a result: no call
            # to _award_bye, because swiss records are frozen at the cut.
            is_bye=seed_b is None,
        ))

    tournament.current_round = round_number
    await session.flush()
    return new_round


async def _advance_playoff(session, tournament, last_round):
    """Pair the winners of `last_round` into the next bracket round, or
    complete the tournament when the final has been decided."""
    stmt = (
        select(TournamentMatch)
        .where(TournamentMatch.round_id == last_round.id)
        .order_by(TournamentMatch.id)          # creation order IS bracket order
    )
    matches = (await session.execute(stmt)).scalars().all()
    unreported = [m for m in matches if not m.is_bye and m.team_a_wins is None]
    if unreported:
        raise ValueError(
            f"{len(unreported)} match(es) in round {last_round.round_number} "
            "still need results."
        )

    winners = []
    for m in matches:
        if m.is_bye:
            winners.append(m.team_a_participant_id)
        elif m.team_a_wins > m.team_b_wins:
            winners.append(m.team_a_participant_id)
        else:
            winners.append(m.team_b_participant_id)

    if len(winners) == 1:
        tournament.status = "completed"
        await session.flush()
        return None

    round_number = last_round.round_number + 1
    new_round = TournamentRound(
        tournament_id=tournament.id, round_number=round_number, stage="playoff"
    )
    session.add(new_round)
    await session.flush()
    for id_a, id_b in advance_pairs(winners):
        session.add(TournamentMatch(
            round_id=new_round.id,
            team_a_participant_id=id_a,
            team_b_participant_id=id_b,
        ))
    tournament.current_round = round_number
    await session.flush()
    return new_round


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
    """End an active tournament now, ranking current standings. Returns the
    champion participant (top of standings), or None if there are none."""
    tournament = await session.get(Tournament, tournament_id)
    if tournament is None:
        raise ValueError("Tournament not found.")
    if tournament.status != "active":
        raise ValueError(f"'{tournament.name}' is not active.")
    tournament.status = "completed"
    await session.flush()
    standings = await get_standings_data(session, tournament_id)
    return standings[0] if standings else None


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
    if match.is_bye:
        raise ValueError("Byes are scored automatically and cannot be reported.")

    part_a = await session.get(TournamentParticipant, match.team_a_participant_id)
    part_b = await session.get(TournamentParticipant, match.team_b_participant_id)

    # Swiss records freeze at the cut: a playoff result is recorded on the
    # match and drives the bracket, but never moves points/W-L/OMW%.
    round_ = await session.get(TournamentRound, match.round_id)
    if round_ is not None and round_.stage != "playoff":
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


async def _round_matches(session, round_id):
    stmt = select(TournamentMatch).where(TournamentMatch.round_id == round_id)
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

    Returns the new TournamentRound, or None when the tournament completes.
    Raises ValueError while the current round still has unreported matches.
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
    matches = await _round_matches(session, round_.id) if round_ else []
    unreported = [m for m in matches if not m.is_bye and m.team_a_wins is None]
    if unreported:
        raise ValueError(
            f"{len(unreported)} match(es) in round {tournament.current_round} "
            "still need results."
        )

    playoff_rounds = await _playoff_rounds(session, tournament_id)
    if playoff_rounds:
        return await _advance_playoff(session, tournament, playoff_rounds[-1])

    if tournament.current_round >= tournament.total_rounds:
        if tournament.cut_to:
            standings = await get_standings_data(session, tournament_id)
            raise SwissComplete(
                tournament.cut_to,
                len([p for p in standings if p.status == "paid"]),
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

    participants = await list_participants(session, tournament_id)
    new_round, _ = await _create_round_with_pairings(
        session, tournament, participants, history, rng
    )
    return new_round


async def get_standings_data(session, tournament_id):
    """Participants ranked by points, then OMW%, then game diff, then name.

    OMW% (opponents' match-win %, byes excluded) needs the full match graph, so
    we load participants and matches and rank in memory (tournaments are small).
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
    )).scalars().all()
    return rank_standings(participants, matches)


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
