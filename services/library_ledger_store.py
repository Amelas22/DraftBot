"""The card-library ledger's store: book, release and read holds in ``library_hold``.

Locking: booking assumes the caller holds ``library_reservation._RESERVE_LOCK``,
so the read of existing holds and the booking that follows it cannot interleave
with another reserve. Reading writes too: ``spans_overlapping`` (and so
``server_verdict``) releases stale holds as it reads them. Call them under the
lock as well, or accept a concurrent heal write; it is idempotent, guarded by
``released_at IS NULL``.

Releasing needs no lock: ``release_server_hold`` only ends a hold that began by
the release time, so a release decided before a re-reserve cannot end the hold
the re-reserve booked.
"""
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.db_session import AsyncSessionLocal
from models.draft_session import DraftSession
from models.library_hold import LibraryHold
from services.library_ledger import (
    SERVER_WINDOW, Span, Verdict, copies_owned, counts_until, verdict)


STALE = "ledger:stale"


async def spans_overlapping(
    library_id: str, start: datetime, end: datetime, now: datetime,
) -> list[Span]:
    """Every hold on the library whose effective interval meets ``[start, end)``.

    The reservation is the source of truth for a server hold. A live server
    hold whose draft is gone, never reserved, or has released its reservation
    is stale: it is released here (at the draft's release, or ``now`` if the
    draft is gone) and left out, so any way a reservation ends that skipped the
    release hook heals itself on the next read.
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            result = await session.execute(
                select(LibraryHold).where(
                    LibraryHold.library_id == library_id,
                    LibraryHold.starts_at < end,
                    (LibraryHold.released_at.is_(None))
                    | (LibraryHold.released_at > start),
                ))
            rows = list(result.scalars().all())
            stale = await _release_stale(session, rows, now)
    spans = [
        Span(r.kind, r.starts_at, r.ends_at, r.released_at)
        for r in rows if r.id not in stale
    ]
    return [s for s in spans if counts_until(s, now) > start]


async def _release_stale(
    session: AsyncSession, rows: list[LibraryHold], now: datetime,
) -> set[int]:
    """Release the live server holds among ``rows`` whose reservation has ended.

    Returns their ids. Runs inside the caller's transaction.
    """
    candidates = [r for r in rows if r.kind == "server"
                  and r.released_at is None and r.draft_session_id is not None]
    if not candidates:
        return set()
    drafts = {
        str(sid): (reserved_at, released_at)
        for sid, reserved_at, released_at in (await session.execute(
            select(DraftSession.session_id, DraftSession.library_reserved_at,
                   DraftSession.library_released_at)
            .where(DraftSession.session_id.in_(
                {str(r.draft_session_id) for r in candidates})))).all()
    }
    stale: set[int] = set()
    for hold in candidates:
        found = drafts.get(str(hold.draft_session_id))
        if found is None:
            when = now
        else:
            reserved_at, released_at = found
            if reserved_at is not None and released_at is None:
                continue
            when = released_at or now
        await session.execute(
            update(LibraryHold)
            .where(LibraryHold.id == hold.id, LibraryHold.released_at.is_(None))
            .values(released_at=when, released_by=STALE))
        stale.add(int(hold.id))
    return stale


async def owned_copies(
    library_id: str, cube_id: str,
    fetch: Callable[[str], Any] | None = None,
) -> int | None:
    """Copies of the cube the library can field, or None if the cube can't be read."""
    from services.card_library_inventory import (
        cube_as_the_library_sees_it, library_holdings)

    seen = await cube_as_the_library_sees_it(cube_id, fetch=fetch)
    if seen is None:
        return None
    return copies_owned(await library_holdings(library_id), seen.cards)


async def server_verdict(
    library_id: str, cube_id: str, now: datetime,
    fetch: Callable[[str], Any] | None = None,
) -> Verdict | None:
    """Whether a server hold over ``[now, now + SERVER_WINDOW)`` fits, or None if unreadable."""
    owned = await owned_copies(library_id, cube_id, fetch=fetch)
    if owned is None:
        return None
    end = now + SERVER_WINDOW
    spans = await spans_overlapping(library_id, now, end, now)
    return verdict("server", owned, spans, now, end, now)


async def book_server_hold(
    library_id: str, cube_id: str, draft_session_id: str, now: datetime,
) -> None:
    """Replace the draft's live server hold with a fresh one over the server window."""
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                update(LibraryHold)
                .where(
                    LibraryHold.draft_session_id == draft_session_id,
                    LibraryHold.kind == "server",
                    LibraryHold.released_at.is_(None))
                .values(released_at=now))
            session.add(LibraryHold(
                library_id=library_id, cube_id=cube_id, kind="server",
                unit="copy", starts_at=now, ends_at=now + SERVER_WINDOW,
                draft_session_id=draft_session_id))


async def release_server_hold(
    draft_session_id: str, when: datetime, by: str | None = None,
) -> bool:
    """End the draft's live server hold, if it began by ``when``.

    True if there was one to release. A hold booked after ``when`` belongs to a
    later reservation, so a release decided before it leaves it alone.
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            result = await session.execute(
                update(LibraryHold)
                .where(
                    LibraryHold.draft_session_id == draft_session_id,
                    LibraryHold.kind == "server",
                    LibraryHold.released_at.is_(None),
                    LibraryHold.starts_at <= when)
                .values(released_at=when, released_by=by))
            return bool(getattr(result, "rowcount", 0))
