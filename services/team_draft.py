"""Team-draft matchups: validate the two sides, create the row, let exactly one draft claim it.

Every function takes the caller's session; none commits. The caller commits.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from models.team_draft import TeamDraft
from services.availability import naive_utc

MAX_PICKS = 5
MAX_SIDE_PEOPLE = 12


@dataclass(frozen=True)
class Picks:
    """One side's choices: role ids and individual user ids."""

    role_ids: tuple[str, ...]
    user_ids: tuple[str, ...]

    def count(self) -> int:
        return len(self.role_ids) + len(self.user_ids)


def _people(
    picks: Picks,
    members: Callable[[str], list[str] | None],
    is_bot: Callable[[str], bool],
) -> set[str]:
    """Everyone a side names: role members plus named users, minus bots.

    A role Discord no longer has contributes nobody.
    """
    found: set[str] = set(picks.user_ids)
    for role_id in picks.role_ids:
        found.update(members(role_id) or [])
    return {user_id for user_id in found if not is_bot(user_id)}


def check_sides(
    a: Picks,
    b: Picks,
    members: Callable[[str], list[str] | None],
    guild_id: str,
    is_bot: Callable[[str], bool],
) -> str | None:
    """The reason these two sides can't be a matchup, or None when they can."""
    if a.count() == 0 or b.count() == 0:
        return "Each side needs at least one role or person."
    if guild_id in a.role_ids or guild_id in b.role_ids:
        return "@everyone can't be a side. Pick a smaller role or name people."
    if a.count() > MAX_PICKS or b.count() > MAX_PICKS:
        return f"A side can have at most {MAX_PICKS} roles and people."
    if set(a.role_ids) & set(b.role_ids) or set(a.user_ids) & set(b.user_ids):
        return "The same role or person is on both sides."
    people_a = _people(a, members, is_bot)
    people_b = _people(b, members, is_bot)
    if not people_a or not people_b:
        return "Each side needs at least one person; a deleted role or a bot doesn't count."
    both = people_a & people_b
    if both:
        noun = "person is" if len(both) == 1 else "people are"
        return f"{len(both)} {noun} on both sides. Each person can only play for one side."
    if len(people_a) > MAX_SIDE_PEOPLE or len(people_b) > MAX_SIDE_PEOPLE:
        return f"A side can have at most {MAX_SIDE_PEOPLE} people."
    return None


async def create_team_draft(
    session: AsyncSession,
    guild_id: str,
    channel_id: str,
    name: str,
    a: Picks,
    b: Picks,
    created_by: str,
    now: datetime,
) -> TeamDraft:
    """Add the matchup and flush so it has an id. The caller commits."""
    team_draft = TeamDraft(
        guild_id=guild_id,
        channel_id=channel_id,
        name=name,
        side_a_roles=list(a.role_ids),
        side_a_users=list(a.user_ids),
        side_b_roles=list(b.role_ids),
        side_b_users=list(b.user_ids),
        created_by=created_by,
        created_utc=naive_utc(now),
    )
    session.add(team_draft)
    await session.flush()
    return team_draft


async def delete_team_draft(session: AsyncSession, team_draft_id: int) -> None:
    """Remove a matchup whose thread never opened. The caller commits."""
    await session.execute(delete(TeamDraft).where(TeamDraft.id == team_draft_id))


async def set_room(
    session: AsyncSession, team_draft_id: int, thread_id: str, control_message_id: str
) -> None:
    """Record the matchup's thread and its control message. The caller commits."""
    await session.execute(
        update(TeamDraft)
        .where(TeamDraft.id == team_draft_id)
        .values(thread_id=thread_id, control_message_id=control_message_id)
    )


async def by_thread(session: AsyncSession, thread_id: str) -> TeamDraft | None:
    result = await session.execute(select(TeamDraft).where(TeamDraft.thread_id == thread_id))
    return result.scalars().first()


async def claim_draft(session: AsyncSession, team_draft_id: int, draft_session_id: str) -> bool:
    """Attach a draft to the matchup if none is attached yet; True when this call won.

    One UPDATE decides it, so concurrent claimers cannot both succeed. The
    caller commits (and rolls back or releases if the draft then fails).
    """
    result = await session.execute(
        update(TeamDraft)
        .where(TeamDraft.id == team_draft_id, TeamDraft.draft_session_id.is_(None))
        .values(draft_session_id=draft_session_id)
    )
    return isinstance(result, CursorResult) and result.rowcount == 1


async def release_draft(session: AsyncSession, team_draft_id: int, draft_session_id: str) -> None:
    """Free the matchup, but only if it still holds this draft. The caller commits."""
    await session.execute(
        update(TeamDraft)
        .where(TeamDraft.id == team_draft_id, TeamDraft.draft_session_id == draft_session_id)
        .values(draft_session_id=None)
    )


DRAFT_CELLS = 2  # a draft takes two half-hour cells
WINDOW_CELLS = 336  # the bookable window: seven days of half-hour cells
_CELL = timedelta(minutes=30)


@dataclass(frozen=True)
class BookingChange:
    """The outcome of a book or clear: ``code`` names the refusal, None when ``ok``.

    ``old``/``new`` are tz-aware UTC (or None). On ``changed`` they carry the
    booking as it now stands (old is None), and ``version`` is the current one.
    """

    ok: bool
    code: str | None
    old: datetime | None
    new: datetime | None
    by_name: str
    version: int


def _aware(moment: datetime | None) -> datetime | None:
    return None if moment is None else moment.replace(tzinfo=timezone.utc)


def _window_start(now: datetime) -> datetime:
    """`now` floored to the half hour, in UTC."""
    moment = now.astimezone(timezone.utc)
    return moment.replace(minute=moment.minute - moment.minute % 30, second=0, microsecond=0)


async def _current(session: AsyncSession, team_draft_id: int) -> tuple[datetime | None, int, bool] | None:
    """(booked start, version, started) as the database holds them now, or None if gone."""
    row = (
        await session.execute(
            select(TeamDraft.booked_start_utc, TeamDraft.booking_version, TeamDraft.draft_session_id).where(
                TeamDraft.id == team_draft_id
            )
        )
    ).first()
    if row is None:
        return None
    return row[0], row[1], row[2] is not None


async def _change(
    session: AsyncSession,
    team_draft_id: int,
    user_id: str,
    name: str,
    start_utc: datetime | None,
    seen_version: int,
    now: datetime,
) -> BookingChange:
    """Shared body of book and clear; ``start_utc`` None means clear."""
    current = await _current(session, team_draft_id)
    if current is None:
        return BookingChange(False, "gone", None, None, name, seen_version)
    old, version, started = current
    old_aware = _aware(old)
    if started:
        return BookingChange(False, "started", old_aware, old_aware, name, version)
    if version != seen_version:
        return BookingChange(False, "changed", None, old_aware, name, version)
    if start_utc is not None:
        start_utc = start_utc.astimezone(timezone.utc)
        if start_utc.minute % 30 or start_utc.second or start_utc.microsecond:
            return BookingChange(False, "bad_input", old_aware, None, name, version)
        if start_utc < now:
            return BookingChange(False, "past", old_aware, None, name, version)
        if start_utc >= _window_start(now) + WINDOW_CELLS * _CELL:
            return BookingChange(False, "out_of_window", old_aware, None, name, version)
    elif old is None:
        return BookingChange(False, "not_booked", None, None, name, version)

    values: dict[str, datetime | str | None] = (
        dict(booked_start_utc=None, booked_by=None, booked_by_name=None)
        if start_utc is None
        else dict(booked_start_utc=naive_utc(start_utc), booked_by=user_id, booked_by_name=name)
    )
    # The version check and the write are one statement, so of two sessions
    # holding the same version exactly one UPDATE matches a row.
    result = await session.execute(
        update(TeamDraft)
        .where(
            TeamDraft.id == team_draft_id,
            TeamDraft.booking_version == seen_version,
            TeamDraft.draft_session_id.is_(None),
        )
        .values(booking_version=seen_version + 1, **values)
    )
    if not (isinstance(result, CursorResult) and result.rowcount == 1):
        now_row = await _current(session, team_draft_id)
        if now_row is None:
            return BookingChange(False, "gone", None, None, name, seen_version)
        cur_start, cur_version, now_started = now_row
        code = "started" if now_started else "changed"
        return BookingChange(False, code, None, _aware(cur_start), name, cur_version)
    return BookingChange(True, None, old_aware, start_utc, name, seen_version + 1)


async def book(
    session: AsyncSession,
    team_draft_id: int,
    user_id: str,
    name: str,
    start_utc: datetime,
    seen_version: int,
    now: datetime,
) -> BookingChange:
    """Book or move the matchup's time. The caller commits."""
    return await _change(session, team_draft_id, user_id, name, start_utc, seen_version, now)


async def clear(
    session: AsyncSession, team_draft_id: int, user_id: str, name: str, seen_version: int, now: datetime
) -> BookingChange:
    """Remove the matchup's booked time. The caller commits."""
    return await _change(session, team_draft_id, user_id, name, None, seen_version, now)
