"""Which sign-up boards carry a Library button.

A premade board has no Change Entry / Settings panel, so where a rental library
backs the server it gets a button of its own for the library row. Other
servers' premade boards must not: a button that only ever says "no library
here" is clutter on every league draft.

A board is built synchronously and in many places -- when it is posted, every
time it is redrawn, and when it is re-attached after a restart -- and none of
them knows its server. So the answer is kept here per draft, filled when a
board is posted and, at startup, for every board still open, before any of
them is re-attached. A server given a library by library_tool, which runs in
its own process, shows the button on boards posted after the next restart.
"""
from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy import select

from database.db_session import AsyncSessionLocal
from models.draft_session import DraftSession
from models.library import RENTAL, Library
from models.library_server import LibraryServer

_BOARDS: "set[str]" = set()


def has_library_button(session_id: Any) -> bool:
    return str(session_id) in _BOARDS


async def note_board(session_id: Any, guild_id: Any) -> None:
    """Record a newly posted board, if its server has a rental library."""
    from services.library_service import rental_library_for

    if await rental_library_for(guild_id) is not None:
        _BOARDS.add(str(session_id))


async def load_boards() -> int:
    """Every board still open in a server with a rental library. At startup,
    before boards are re-attached."""
    async with AsyncSessionLocal() as session:
        guilds = (select(LibraryServer.guild_id)
                  .join(Library, Library.id == LibraryServer.library_id)
                  .where(Library.kind == RENTAL))
        ids = (await session.scalars(
            select(DraftSession.session_id).where(
                DraftSession.guild_id.in_(guilds),
                DraftSession.deletion_time > datetime.now()))).all()
    _BOARDS.update(str(i) for i in ids)
    logger.info("library: {} open boards carry the Library button", len(ids))
    return len(ids)
