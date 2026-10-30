"""The rules for booking a time on something that owns a booked draft slot.

``change`` holds the validation and the lost-race handling; a ``BookingStore``
says where the booking lives. Every function takes the caller's session; none
commits. The caller commits.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from services.availability import CELL_MINUTES, floor_half_hour, naive_utc

WINDOW_CELLS = 336  # the bookable window: seven days of half-hour cells
_CELL = timedelta(minutes=CELL_MINUTES)


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


class BookingStore(Protocol):
    """Where one booking is kept."""

    async def current(self, session: AsyncSession) -> tuple[datetime | None, int, bool] | None:
        """(naive booked start, version, started) as the database holds them now, or None if gone."""
        ...

    async def write(self, session: AsyncSession, seen_version: int, values: dict[str, datetime | str | None]) -> bool:
        """Apply ``values`` and set version to ``seen_version + 1``, in one conditional UPDATE.

        Matches only while the version is still ``seen_version`` and the owner
        hasn't started; True iff exactly one row matched. ``values`` keys are
        ``booked_start_utc``, ``booked_by`` and ``booked_by_name``.
        """
        ...


def _aware(moment: datetime | None) -> datetime | None:
    return None if moment is None else moment.replace(tzinfo=timezone.utc)


async def change(
    session: AsyncSession,
    store: BookingStore,
    user_id: str,
    name: str,
    start_utc: datetime | None,
    seen_version: int,
    now: datetime,
) -> BookingChange:
    """Shared body of book and clear; ``start_utc`` None means clear."""
    current = await store.current(session)
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
        if start_utc >= floor_half_hour(now) + WINDOW_CELLS * _CELL:
            return BookingChange(False, "out_of_window", old_aware, None, name, version)
    elif old is None:
        return BookingChange(False, "not_booked", None, None, name, version)

    values: dict[str, datetime | str | None] = (
        dict(booked_start_utc=None, booked_by=None, booked_by_name=None)
        if start_utc is None
        else dict(booked_start_utc=naive_utc(start_utc), booked_by=user_id, booked_by_name=name)
    )
    if not await store.write(session, seen_version, values):
        now_row = await store.current(session)
        if now_row is None:
            return BookingChange(False, "gone", None, None, name, seen_version)
        cur_start, cur_version, now_started = now_row
        code = "started" if now_started else "changed"
        return BookingChange(False, code, None, _aware(cur_start), name, cur_version)
    return BookingChange(True, None, old_aware, start_utc, name, seen_version + 1)
