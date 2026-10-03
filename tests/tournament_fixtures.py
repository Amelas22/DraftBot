"""Helpers shared by the tournament bracket test files."""
from sqlalchemy import select

from models.tournament import TournamentMatch, TournamentParticipant, TournamentRound
from services.tournament_service import create_tournament, register_team


async def _swiss_done(session, cut_to=4, teams=6):
    """A tournament sitting at the end of swiss, with distinct points so the
    seeding order is unambiguous.

    Order matters: register_team refuses a tournament that is not open for
    registration, so teams go in FIRST and the status flips afterwards. It
    also returns (participant, created), not a participant.
    """
    t = await create_tournament(session, "g1", "Cut Test", 3)
    for i in range(teams):
        participant, _ = await register_team(session, t.id, f"Team{i}", f"cap{i}")
        participant.status = "paid"
        participant.points = (teams - i) * 3      # Team0 highest
    # A tournament at the end of swiss HAS its round rows; without them
    # _current_round returns None and the tests traverse a branch production
    # never reaches.
    for number in range(1, 4):
        session.add(TournamentRound(
            tournament_id=t.id, round_number=number, stage="swiss"))
    t.cut_to = cut_to
    t.status = "active"
    t.current_round = 3
    await session.flush()
    return t


async def _matches(session, round_id):
    """A round's matches in creation order -- which, in the bracket, IS bracket
    order. Written out in nearly every test below before it landed here."""
    return (await session.execute(
        select(TournamentMatch).where(TournamentMatch.round_id == round_id)
        .order_by(TournamentMatch.id)
    )).scalars().all()


async def _participants(session, tournament_id):
    """Ordered by points, best first -- the seeding order. Unordered, a test
    that drops "the top two" drops an unspecified two."""
    return (await session.execute(
        select(TournamentParticipant).where(
            TournamentParticipant.tournament_id == tournament_id)
        .order_by(TournamentParticipant.points.desc())
    )).scalars().all()
