"""Service layer for team-based Swiss tournaments.

Slice 1: create/register/view. Slice 2: start, Swiss rounds, admin-set results,
standings.

All functions take an AsyncSession so callers control the transaction and tests
can point them at a temp database (mirrors the leaderboard_service convention).
"""
from sqlalchemy import func, select

from database.db_session import db_session
from draft_organization.swiss import pair_round, rank_standings, round_robin_schedule
from models.team import Team
from models.tournament import (
    Tournament,
    TournamentMatch,
    TournamentParticipant,
    TournamentRound,
)

ACTIVE_STATUSES = ("registration", "active")
POINTS_WIN = 3
POINTS_DRAW = 1


async def get_active_tournament(session, guild_id):
    """Return the guild's current registration/active tournament, or None."""
    stmt = select(Tournament).where(
        Tournament.guild_id == str(guild_id),
        Tournament.status.in_(ACTIVE_STATUSES),
    )
    result = await session.execute(stmt)
    return result.scalars().first()


ALL_OPEN_FORMATS = ("round_robin", "manual")


async def create_tournament(session, guild_id, name, total_rounds, format="swiss"):
    """Create a tournament in registration status.

    ``format`` is 'swiss', 'round_robin', or 'manual'. For the all-open formats
    total_rounds is set at start (derived from the schedule), so callers may
    pass 0. Raises ValueError if the guild already has a registration/active
    tournament (one active per guild keeps other commands argument-free).
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
        guild_id=str(guild_id), name=name, total_rounds=total_rounds, format=format
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
    await session.delete(participant)
    await session.flush()
    return participant


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
    if len(participants) < 2:
        raise ValueError("At least 2 teams must be registered to start.")

    tournament.status = "active"
    if tournament.format == "round_robin":
        return await _build_round_robin(session, tournament, participants, rng)

    _, matches = await _create_round_with_pairings(
        session, tournament, participants, set(), rng
    )
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
    if tournament.format != "swiss":
        raise ValueError(
            "next_round is for Swiss tournaments — this one's schedule is fixed; "
            "use /tournament finish to end it."
        )

    round_ = await _current_round(session, tournament)
    matches = await _round_matches(session, round_.id)
    unreported = [m for m in matches if not m.is_bye and m.team_a_wins is None]
    if unreported:
        raise ValueError(
            f"{len(unreported)} match(es) in round {tournament.current_round} "
            "still need results."
        )

    if tournament.current_round >= tournament.total_rounds:
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
