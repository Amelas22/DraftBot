"""Service layer for team-based Swiss tournaments (Slice 1: create/register/view).

All functions take an AsyncSession so callers control the transaction and tests
can point them at a temp database (mirrors the leaderboard_service convention).
"""
from sqlalchemy import func, select

from models.team import Team
from models.tournament import Tournament, TournamentParticipant

ACTIVE_STATUSES = ("registration", "active")


async def get_active_tournament(session, guild_id):
    """Return the guild's current registration/active tournament, or None."""
    stmt = select(Tournament).where(
        Tournament.guild_id == str(guild_id),
        Tournament.status.in_(ACTIVE_STATUSES),
    )
    result = await session.execute(stmt)
    return result.scalars().first()


async def create_tournament(session, guild_id, name, total_rounds):
    """Create a tournament in registration status.

    Raises ValueError if the guild already has a registration/active tournament
    (one active tournament per guild keeps all other commands argument-free).
    """
    existing = await get_active_tournament(session, guild_id)
    if existing is not None:
        raise ValueError(
            f"'{existing.name}' is already {existing.status} in this server. "
            "Finish it before creating a new tournament."
        )
    tournament = Tournament(guild_id=str(guild_id), name=name, total_rounds=total_rounds)
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
