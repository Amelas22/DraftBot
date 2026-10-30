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
from models.tournament import Tournament, TournamentMatch, TournamentParticipant, TournamentRound
from services.booking import BookingChange, change


def _has_draft(match_id: int):
    return exists().where(DraftSession.tournament_match_id == match_id)


async def schedulable_match(
    session: AsyncSession, guild_id: str, match_id: int
) -> tuple[TournamentMatch, TournamentParticipant, TournamentParticipant] | None:
    """The match and its two teams, only if it can be scheduled from ``guild_id``.

    None when the match is gone, a bye, still waiting on a side, or belongs to a
    tournament of another guild. Reads the database only, so it is safe to call
    before anything that talks to Discord.
    """
    row = (
        await session.execute(
            select(TournamentMatch, Tournament.guild_id)
            .join(TournamentRound, TournamentMatch.round_id == TournamentRound.id)
            .join(Tournament, TournamentRound.tournament_id == Tournament.id)
            .where(TournamentMatch.id == match_id)
        )
    ).first()
    if row is None:
        return None
    match, tournament_guild = row
    if (
        match.is_bye
        or str(tournament_guild) != guild_id
        or match.team_a_participant_id is None
        or match.team_b_participant_id is None
    ):
        return None
    ids = (match.team_a_participant_id, match.team_b_participant_id)
    found = {p.id: p for p in (await session.execute(
        select(TournamentParticipant).where(TournamentParticipant.id.in_(ids))
    )).scalars()}
    if ids[0] not in found or ids[1] not in found:
        return None
    return match, found[ids[0]], found[ids[1]]


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
