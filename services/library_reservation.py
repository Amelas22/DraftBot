"""A rental library's cube, reserved by the draft that will use it.

A library with one copy of a cube can lend it to one draft at a time. So a
draft that wants to borrow RESERVES the copy while its queue is still filling,
and a draft that fires without a reservation gets no library decks. Nothing
ranks one queue above another: once a queue holds the copy, the copy is in use,
and anybody in a second queue of the same cube is told so and pointed at the
first, which they can join to fire it.

Taken automatically: the moment an eligible player who borrows (their saved
preference, services/library_prompt) signs up to a filling queue, the queue
reserves the cube if the shelf can cover it.

Kept until nobody in the draft needs it. A player's need ends when they leave
the queue, say they don't need it (`release_player`), return their deck, have
their offer expire, or play a match without having collected a deck. The
reservation ends with the last of them, or when the draft is decided or over,
or when a queue goes quiet past its inactivity deadline. Released is for good:
another queue can take the copy, and this draft gets it back only by reserving
again while a copy is free.

What it holds is the cube's business, not this module's: the whole cube while
no decks exist, then just the decks (card_library_inventory).

Release is noticed lazily, wherever a reservation is read -- availability, a
reserve attempt, deck assignment -- and written down the first time it is seen,
so nothing has to listen for every way a player can stop needing it.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from loguru import logger
from sqlalchemy import select, update

from database.db_session import AsyncSessionLocal
from helpers.stale_drafts import rooms_reaped
from models.card_loan import CardLoan
from models.draft_session import DraftSession
from models.library_server import LibraryServer
from models.match import MatchResult

# A deck the borrower has taken, or is taking. A player holding one still needs
# the reservation however many matches they have played.
COLLECTED = ("out_pending", "borrowed", "return_pending", "dispatch_unknown")
# A player whose deck came back, or whose offer lapsed, needs nothing more.
NEED_ENDED = ("returned", "expired")
FINISHED_STAGES = ("completed", "abandoned")

# Reserving is check-then-write: can the shelf cover this cube, then take it.
# One process, so one lock is enough to stop two queues taking the last copy.
_RESERVE_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class ReserveAnswer:
    """What a reserve attempt found.

    status: "reserved" (taken now), "held" (this draft already had it),
    "in_use" (another draft's reservation is using the copy; `by` is it when
    it is a draft of the same cube), "short" (the shelf can't cover the cube for
    another reason), "fired" (teams already formed -- too late to reserve), or
    "not_applicable" (no rental library offering this cube here).
    """
    status: str
    by: "Optional[DraftSession]" = None


def joinable_link(draft: Any) -> Optional[str]:
    """A link to point players at, if `draft` is a queue they can still join."""
    if draft is None or draft.teams_start_time is not None:
        return None
    return queue_link(draft)


def queue_link(draft: Any) -> Optional[str]:
    """A link to a draft's signup message, if it has one."""
    if not (draft.guild_id and draft.draft_channel_id and draft.message_id):
        return None
    return (f"https://discord.com/channels/{draft.guild_id}/"
            f"{draft.draft_channel_id}/{draft.message_id}")


async def library_users(draft: Any, library_id: Any = None) -> "set[str]":
    """Who in this draft still needs the library's copy of its cube.

    Signed up, allowed to borrow, saved as borrowing, and not finished with it:
    not released, no deck returned or offer lapsed, and not having played a
    match without a collected deck.
    """
    from preference_service import get_players_using_library
    from services.library_access_service import may_borrow_many
    from services.library_service import library_id_for

    if library_id is None:
        library_id = await library_id_for(draft.guild_id)
    if library_id is None:
        return set()
    seated = {str(p) for p in (draft.sign_ups or {})}
    seated -= {str(p) for p in (draft.library_released or [])}
    if seated:
        seated &= await may_borrow_many(library_id, seated)
    if seated:
        seated &= await get_players_using_library(sorted(seated), str(draft.guild_id))
    if not seated:
        return set()
    async with AsyncSessionLocal() as session:
        loans = (await session.execute(
            select(CardLoan.borrower_id, CardLoan.state).where(
                CardLoan.source == f"draft:{draft.session_id}"))).all()
        results = (await session.execute(
            select(MatchResult.player1_id, MatchResult.player2_id).where(
                MatchResult.session_id == draft.session_id,
                MatchResult.result_submitted_at.isnot(None)))).all()
    collected = {str(b) for b, state in loans if state in COLLECTED}
    ended = {str(b) for b, state in loans if state in NEED_ENDED}
    played = {str(p) for row in results for p in row if p}
    return {p for p in seated
            if p not in ended and not (p in played and p not in collected)}


async def _why_released(draft: Any, now: datetime,
                        users: "Optional[set[str]]" = None) -> Optional[str]:
    from services.library_reminders import draft_state

    if draft.session_stage in FINISHED_STAGES:
        return "the draft is over"
    if draft.teams_start_time is None and rooms_reaped(draft, now):
        return "the queue went quiet"
    if draft.teams_start_time is not None and (await draft_state(draft.session_id)).settled:
        return "the draft is decided"
    if not (await library_users(draft) if users is None else users):
        return "nobody in it needs the library any more"
    return None


async def still_reserved(draft: Any, now: Optional[datetime] = None, *,
                         library_id: Any = None) -> bool:
    """Whether this draft holds the library's copy right now.

    The first time it finds the reservation has no reason left to stand, it
    writes the release down, which is what makes released for good.
    """
    if draft.library_reserved_at is None or draft.library_released_at is not None:
        return False
    when = now or datetime.now()
    why = await _why_released(draft, when, await library_users(draft, library_id))
    if why is None:
        return True
    # Only the reservation that was judged: a re-reserve, or a borrower joining
    # while this was being worked out, must not be undone by a stale verdict.
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            update(DraftSession)
            .where(DraftSession.id == draft.id,
                   DraftSession.library_reserved_at == draft.library_reserved_at,
                   DraftSession.library_released_at.is_(None))
            .values(library_released_at=when))
        await session.commit()
    if not result.rowcount:
        return False
    draft.library_released_at = when
    logger.info("library: draft {} released its reservation of {}: {}",
                draft.session_id, draft.cube, why)
    return False


async def reservations(library_id: Any) -> "list[DraftSession]":
    """The drafts holding a reservation against this library's shelf."""
    async with AsyncSessionLocal() as session:
        guilds = select(LibraryServer.guild_id).where(
            LibraryServer.library_id == str(library_id))
        candidates = list((await session.scalars(
            select(DraftSession).where(
                DraftSession.library_reserved_at.isnot(None),
                DraftSession.library_released_at.is_(None),
                DraftSession.guild_id.in_(guilds)))).all())
    return [d for d in candidates if await still_reserved(d, library_id=library_id)]


async def users_if_reserved(draft: Any) -> "set[str]":
    """Who still needs the copy, if this draft holds it; nobody otherwise."""
    if draft.library_reserved_at is None or draft.library_released_at is not None:
        return set()
    users = await library_users(draft)
    if await _why_released(draft, datetime.now(), users) is not None:
        await still_reserved(draft)        # records the release
        return set()
    return users


async def reserve(session_id: Any) -> ReserveAnswer:
    """Reserve the library's copy of this queue's cube, if a copy is free."""
    from services.card_library_inventory import (
        cube_as_the_library_sees_it, cube_support, fetch_cube, library_available,
        read_once)
    from services.library_service import offers, rental_library_for

    async with _RESERVE_LOCK:
        async with AsyncSessionLocal() as session:
            draft = (await session.execute(select(DraftSession).where(
                DraftSession.session_id == str(session_id)))).scalar_one_or_none()
        if draft is None or not draft.cube:
            return ReserveAnswer("not_applicable")
        library = await rental_library_for(draft.guild_id)
        if library is None or not await offers(library.id, draft.cube):
            return ReserveAnswer("not_applicable")
        if await still_reserved(draft):
            return ReserveAnswer("held")
        if draft.teams_start_time is not None:
            return ReserveAnswer("fired")
        if not await library_users(draft):
            # Asked for by somebody who has since left, released it, or switched
            # off: reserving would only be released at the next read.
            return ReserveAnswer("not_applicable")

        read = read_once(fetch_cube)
        seen = await cube_as_the_library_sees_it(draft.cube, fetch=read)
        if seen and seen.cards and cube_support(
                seen.cards, await library_available(library.id, fetch=read)).ok:
            async with AsyncSessionLocal() as session:
                row = await session.get(DraftSession, draft.id)
                if row is not None:
                    row.library_reserved_at = datetime.now()
                    row.library_released_at = None
                    await session.commit()
            logger.info("library: draft {} reserved {} from {}",
                        draft.session_id, draft.cube, library.id)
            return ReserveAnswer("reserved")

        using = [d for d in await reservations(library.id)
                 if d.cube == draft.cube and d.session_id != draft.session_id]
        if using:
            return ReserveAnswer("in_use", by=_first_reserved(using))
        return ReserveAnswer("short")


async def release_player(session_id: Any, player_id: Any) -> bool:
    """Record that this player doesn't need the library for this draft.

    False if there is no such draft. The reservation itself goes the next time
    it is read, if this was the last player who needed it.
    """
    async with AsyncSessionLocal() as session:
        row = (await session.execute(select(DraftSession).where(
            DraftSession.session_id == str(session_id)))).scalar_one_or_none()
        if row is None:
            return False
        released = [str(p) for p in (row.library_released or [])]
        if str(player_id) not in released:
            # A new list, not an append: a JSON column mutated in place is not
            # seen as changed and would never be written.
            row.library_released = released + [str(player_id)]
            await session.commit()
        draft = row
    await still_reserved(draft)
    return True


async def queue_holding(guild_id: Any, cube_id: Any) -> "Optional[DraftSession]":
    """The draft holding the library's copy of this cube here, if any.

    For the signup board, which is written when a queue opens and so can say
    where the copy is rather than just that it is gone.
    """
    from services.library_service import library_id_for

    library_id = await library_id_for(guild_id)
    if library_id is None:
        return None
    using = [d for d in await reservations(library_id) if d.cube == cube_id]
    return _first_reserved(using) if using else None


def _first_reserved(drafts: "list[DraftSession]") -> DraftSession:
    """The longest-standing of these reservations: the queue to point at."""
    def reserved_at(draft: DraftSession) -> datetime:
        return draft.library_reserved_at or datetime.max
    return min(drafts, key=reserved_at)

