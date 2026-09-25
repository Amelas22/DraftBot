"""Which library serves a server, on what terms, offering which cubes.

The one place "which library is this?" is answered. Every library-touching
path starts from the guild a command was typed in, looks up the binding, and
carries the LIBRARY from there -- so nothing below this module needs to know
that servers exist.

A server with no binding has no library. That is a refusal, not a free one:
absence means nobody has said this server may borrow, and reading it as an
open library is how a room nobody configured would start lending somebody
else's cards.

Set from a shell tool, never from Discord. A server admin can write
`configs/<guild>.json` through the bot's own commands, so a binding they could
reach would let them repoint their server at a cheaper library -- which is the
same as setting their own deposit.
"""
from typing import Any, Optional

from loguru import logger
from sqlalchemy import select

from database.db_session import AsyncSessionLocal
from models.library import COMMUNAL, Library
from models.library_cube import LibraryCube
from models.library_server import LibraryServer


async def library_for(guild_id: Any) -> "Optional[Library]":
    """The library this server draws on, or None if it has not been bound."""
    if guild_id is None:
        return None
    async with AsyncSessionLocal() as session:
        binding = await session.get(LibraryServer, str(guild_id))
        if binding is None:
            return None
        library = await session.get(Library, str(binding.library_id))
        if library is None:
            # A binding naming a library that no longer exists lends nothing.
            # Loud, because it is a configuration fault rather than an answer:
            # the server looks set up and behaves as though it is not.
            logger.error("library: {} is bound to '{}', which does not exist",
                         guild_id, binding.library_id)
        return library


async def library_id_for(guild_id: Any) -> "Optional[str]":
    """Just the id, for the callers that only need something to scope by."""
    library = await library_for(guild_id)
    return str(library.id) if library else None


def is_communal(library: "Optional[Library]") -> bool:
    """Does this library list and price what it is given?

    False for anything unset, because saying yes lets any member make a cube
    free -- right where the cards are the members' own, a hole where they are
    a sponsor's.
    """
    return bool(library is not None and library.kind == COMMUNAL)


def price_of(library: "Optional[Library]") -> "Optional[int]":
    """What a borrower puts up for this library, or None if there isn't one.

    None rather than 0: no library is a refusal, and a library that genuinely
    charges nothing is a choice somebody made. Folding the two together is how
    an unconfigured server would advertise free borrowing.
    """
    if library is None:
        return None
    return int(library.collateral_tix or 0)


async def offers(library_id: Any, cube_id: Any) -> bool:
    """Has this library been set up to lend for this cube?

    A standing decision, not a live one -- whether the shelf can cover a draft
    right now is card_library_inventory's question, asked per draft.
    """
    if not library_id or not cube_id:
        return False
    async with AsyncSessionLocal() as session:
        return await session.get(
            LibraryCube, (str(library_id), str(cube_id))) is not None


async def offered_among(library_id: Any, cube_ids: "list[Any]") -> "set[str]":
    """Which of these cubes the library offers.

    One query for the whole list: this runs while building a dropdown, and a
    lookup per cube would be a round trip per row.
    """
    wanted = [str(c) for c in cube_ids if c]
    if not library_id or not wanted:
        return set()
    async with AsyncSessionLocal() as session:
        rows = (await session.scalars(
            select(LibraryCube.cube_id).where(
                LibraryCube.library_id == str(library_id),
                LibraryCube.cube_id.in_(wanted)))).all()
    return {str(r) for r in rows}


async def prices_for(cube_ids: "list[Any]",
                     guild_id: Any) -> "dict[str, int]":
    """`{cube_id: deposit}` for those of these cubes this server can borrow
    from. A cube the library does not offer is simply absent, which is what
    tells a caller to leave it alone rather than mark it free.

    Every offered cube carries the SAME number, because the price belongs to
    the library. Returned per cube anyway so the callers that render a dropdown
    do not each have to know that.
    """
    library = await library_for(guild_id)
    price = price_of(library)
    if price is None or library is None:
        return {}
    offered = await offered_among(library.id, cube_ids)
    return {cube: price for cube in offered}


async def offer_cube(library_id: Any, cube_id: Any, added_by: str) -> bool:
    """List a cube for this library. True if this added it."""
    async with AsyncSessionLocal() as session:
        if await session.get(LibraryCube, (str(library_id), str(cube_id))):
            return False
        session.add(LibraryCube(library_id=str(library_id), cube_id=str(cube_id),
                                added_by=added_by))
        await session.commit()
    logger.info("library: {} now offers {}", library_id, cube_id)
    return True
