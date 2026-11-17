"""A player's confirmation of their availability, per league week.

Imports only models and SQLAlchemy so ``availability`` can import it.
"""
from collections.abc import Iterable, Sequence
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from database.db_session import db_session
from models.scheduler import PlayerRoundConfirmation, PlayerSchedulerSetting

CONFIRMATION_FORMAT = "draftbot.scheduler.confirmation"


async def trusts_usual_of(session: AsyncSession, users: Sequence[str]) -> dict[str, bool]:
    """Whether each user's usual week counts as confirmed; no row or NULL means yes."""
    result = {user: True for user in users}
    if not users:
        return result
    rows = await session.execute(
        select(PlayerSchedulerSetting.user_id, PlayerSchedulerSetting.trusts_usual).where(
            PlayerSchedulerSetting.user_id.in_(list(users))
        )
    )
    for user_id, value in rows:
        if value is not None:
            result[str(user_id)] = bool(value)
    return result


async def confirmed_rounds(
    session: AsyncSession, users: Sequence[str], first: date, last: date
) -> dict[str, frozenset[date]]:
    """Each user's confirmed league Mondays within ``first``..``last`` inclusive."""
    found: dict[str, set[date]] = {user: set() for user in users}
    if not users:
        return {}
    rows = await session.execute(
        select(PlayerRoundConfirmation.user_id, PlayerRoundConfirmation.round_monday).where(
            PlayerRoundConfirmation.user_id.in_(list(users)),
            PlayerRoundConfirmation.round_monday >= first,
            PlayerRoundConfirmation.round_monday <= last,
        )
    )
    for user_id, monday in rows:
        found[str(user_id)].add(monday)
    return {user: frozenset(days) for user, days in found.items()}


async def add_confirmations(
    session: AsyncSession,
    user_id: str,
    mondays: Iterable[date],
    now_utc: datetime,
    source: str,
) -> None:
    """Confirm each Monday's week, keeping any confirmation already there. The caller owns the transaction."""
    wanted = sorted(set(mondays))
    if not wanted:
        return
    stored = now_utc.replace(tzinfo=None)
    await session.execute(
        insert(PlayerRoundConfirmation)
        .values(
            [
                {"user_id": user_id, "round_monday": monday, "source": source, "stored_utc": stored}
                for monday in wanted
            ]
        )
        .on_conflict_do_nothing(index_elements=["user_id", "round_monday"])
    )


async def set_trusts_usual(user_id: str, value: bool) -> None:
    """Store the switch; confirmations are never touched."""
    async with db_session() as session:
        await session.execute(
            insert(PlayerSchedulerSetting)
            .values(user_id=user_id, trusts_usual=value)
            .on_conflict_do_update(index_elements=["user_id"], set_={"trusts_usual": value})
        )


async def confirm_round(user_id: str, monday: date, now_utc: datetime) -> None:
    """The player's 'looks right, confirm' for one league week."""
    async with db_session() as session:
        await add_confirmations(session, user_id, [monday], now_utc, "button")


async def confirmation_doc(user_id: str, first: date, last: date) -> dict[str, object]:
    async with db_session() as session:
        trusts = (await trusts_usual_of(session, [user_id]))[user_id]
        days = (await confirmed_rounds(session, [user_id], first, last))[user_id]
    return {
        "format": CONFIRMATION_FORMAT,
        "version": 1,
        "trusts_usual": trusts,
        "confirmed_rounds": [day.isoformat() for day in sorted(days)],
    }
