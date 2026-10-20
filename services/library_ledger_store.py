"""The card-library ledger's store: book, release and read holds in ``library_hold``.

Locking: these functions assume the caller holds
``library_reservation._RESERVE_LOCK`` when booking, so the read of existing holds
and the booking that follows it cannot interleave with another reserve.
"""
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import select, update

from database.db_session import AsyncSessionLocal
from models.library_hold import LibraryHold
from services.library_ledger import (
    SERVER_WINDOW, Span, Verdict, copies_owned, counts_until, verdict)


async def spans_overlapping(
    library_id: str, start: datetime, end: datetime, now: datetime,
) -> list[Span]:
    """Every hold on the library whose effective interval meets ``[start, end)``."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(LibraryHold).where(
                LibraryHold.library_id == library_id,
                LibraryHold.starts_at < end,
                (LibraryHold.released_at.is_(None))
                | (LibraryHold.released_at > start),
            ))
        rows = result.scalars().all()
    spans = [
        Span(r.kind, r.starts_at, r.ends_at, r.released_at) for r in rows
    ]
    return [s for s in spans if counts_until(s, now) > start]


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
    """End the draft's live server hold. True if there was one to release."""
    async with AsyncSessionLocal() as session:
        async with session.begin():
            result = await session.execute(
                update(LibraryHold)
                .where(
                    LibraryHold.draft_session_id == draft_session_id,
                    LibraryHold.kind == "server",
                    LibraryHold.released_at.is_(None))
                .values(released_at=when, released_by=by))
            return bool(getattr(result, "rowcount", 0))
