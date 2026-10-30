"""A tournament match's booked draft time, kept on its TournamentMatch row.

The rules (window, half-hour grid, version check) live in services.booking;
this module only says where a match keeps its booking and when it stops
accepting one. Every function takes the caller's session; none commits.
"""

from datetime import datetime

from sqlalchemy import exists, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from models.draft_session import DraftSession
from models.tournament import TournamentMatch
from services.booking import BookingChange, change


def _has_draft(match_id: int):
    return exists().where(DraftSession.tournament_match_id == match_id)


class MatchStore:
    """A tournament match's booking. It is closed once a result or a draft exists."""

    def __init__(self, match_id: int) -> None:
        self.match_id = match_id

    async def current(self, session: AsyncSession) -> tuple[datetime | None, int, bool] | None:
        row = (
            await session.execute(
                select(
                    TournamentMatch.booked_start_utc,
                    TournamentMatch.booking_version,
                    TournamentMatch.team_a_wins.is_not(None) | _has_draft(self.match_id),
                ).where(TournamentMatch.id == self.match_id)
            )
        ).first()
        if row is None:
            return None
        return row[0], row[1], bool(row[2])

    async def write(self, session: AsyncSession, seen_version: int, values: dict[str, datetime | str | None]) -> bool:
        # Every guard is in the one statement, so a result or a draft landing
        # between the read and the write makes it match nothing.
        result = await session.execute(
            update(TournamentMatch)
            .where(
                TournamentMatch.id == self.match_id,
                TournamentMatch.booking_version == seen_version,
                TournamentMatch.team_a_wins.is_(None),
                ~_has_draft(self.match_id),
            )
            .values(booking_version=seen_version + 1, **values)
        )
        return isinstance(result, CursorResult) and result.rowcount == 1


async def book_match(
    session: AsyncSession,
    match_id: int,
    user_id: str,
    name: str,
    start_utc: datetime,
    seen_version: int,
    now: datetime,
) -> BookingChange:
    """Book or move the match's time. The caller commits."""
    return await change(session, MatchStore(match_id), user_id, name, start_utc, seen_version, now)


async def clear_match(
    session: AsyncSession, match_id: int, user_id: str, name: str, seen_version: int, now: datetime
) -> BookingChange:
    """Clear the match's booked time. The caller commits."""
    return await change(session, MatchStore(match_id), user_id, name, None, seen_version, now)
