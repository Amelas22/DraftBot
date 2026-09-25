"""Who may borrow from a library that is not open to everyone.

Only BORROWING is gated. Depositing, /mydeposits and /withdraw stay open to
anyone: somebody contributing cards is not the risk this guards against, and a
sponsor locked out of their own deposits would be absurd.

Scoped to the LIBRARY, not the server. Somebody trusted with a sponsor's cards
is trusted with them in every room that library lends into, and a server-scoped
list meant re-inviting the same people per room -- and, worse, left the library
open in any room nobody had listed yet.
"""
from typing import Any

from loguru import logger
from sqlalchemy import delete, select

from database.db_session import AsyncSessionLocal
from models.library_member import LibraryMember


async def members(library_id: Any) -> "list[str]":
    """Everyone trusted to borrow from this library. Empty means it is open."""
    if not library_id:
        return []
    async with AsyncSessionLocal() as session:
        return [str(p) for p in (await session.scalars(
            select(LibraryMember.player_id).where(
                LibraryMember.library_id == str(library_id)))).all()]


async def is_invite_only(library_id: Any) -> bool:
    """Does this library lend only to named people?

    Asked by the signup board, which is shared and cannot know who is reading
    it -- so it says the library is restricted rather than promising a deck to
    a room where most people would be turned away at /borrow.
    """
    return bool(await members(library_id))


async def may_borrow(library_id: Any, player_id: Any) -> bool:
    """Is this person allowed to take a deck out of this library?

    A library with nobody listed lends to everyone in the servers it serves --
    the communal case, which needs no setup and must keep working untouched.
    Naming anybody restricts borrowing to the named.
    """
    if not library_id:
        return False
    listed = await members(library_id)
    return not listed or str(player_id) in listed


async def invite(library_id: Any, player_id: Any, added_by: str) -> bool:
    """Trust somebody to borrow. True if this was the first, which CLOSES the
    library to everyone else -- the caller is expected to say so."""
    async with AsyncSessionLocal() as session:
        already = await members(library_id)
        if str(player_id) in already:
            return False
        session.add(LibraryMember(library_id=str(library_id),
                                  player_id=str(player_id), added_by=added_by))
        await session.commit()
        return not already


async def uninvite(library_id: Any, player_id: Any) -> str:
    """Stop somebody borrowing. "removed", "not_listed", or "would_open".

    Refuses to remove the LAST member. Empty means open, so that removal turned
    a curated library into a public one and handed the person being removed the
    access that was being taken away -- the most expensive possible outcome of
    an operation whose whole purpose is to withdraw trust. Nothing about
    "remove Bob" says "and let everyone in", so it does not do that;
    open_library says it outright.

    "not_listed" because this used to report success for anybody at all: a
    mistyped id read as a revocation that had not happened, while the real
    member carried on borrowing.
    """
    listed = await members(library_id)
    if str(player_id) not in listed:
        return "not_listed"
    if len(listed) == 1:
        return "would_open"
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(LibraryMember).where(
                LibraryMember.library_id == str(library_id),
                LibraryMember.player_id == str(player_id)))
        await session.commit()
    return "removed"


async def open_library(library_id: Any) -> int:
    """Let everyone borrow from this library again. Returns how many were listed.

    The deliberate way to reach the state uninvite refuses to fall into.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            delete(LibraryMember).where(
                LibraryMember.library_id == str(library_id)))
        await session.commit()
    removed = int(result.rowcount or 0)
    logger.info("library: {} is open to everyone again ({} member(s) cleared)",
                library_id, removed)
    return removed
