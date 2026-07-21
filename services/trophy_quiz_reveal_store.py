from sqlalchemy.exc import IntegrityError
from database.db_session import db_session
from models import TrophyQuizReveal


async def has_revealed(quiz_id: str, player_id: str) -> bool:
    """True if this player has paid to reveal the pilots on this quiz."""
    async with db_session() as session:
        row = await session.get(TrophyQuizReveal, (quiz_id, player_id))
    return row is not None


async def record_reveal(quiz_id: str, player_id: str) -> None:
    """Idempotently mark that this player revealed the pilots on this quiz.

    Tolerates a concurrent duplicate (rapid double-click): the check-then-insert
    can race two interactions past the existence check, and the losing commit hits
    the composite PK. Swallow that — the row exists either way, so it's a no-op.
    """
    async with db_session() as session:
        existing = await session.get(TrophyQuizReveal, (quiz_id, player_id))
        if existing is None:
            session.add(TrophyQuizReveal(quiz_id=quiz_id, player_id=player_id))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()  # a concurrent click already recorded it
