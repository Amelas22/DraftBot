"""Which sign-up boards carry a Library button, and what each board's library
line says.

Only a staked board has a Change Entry / Settings panel, so where a rental
library backs the server every other board -- premade, random, swiss, winston
-- gets a button of its own for the library row. Other servers' boards must
not: a button that only ever says "no library here" is clutter on every league
draft.

A board is built synchronously and in many places -- when it is posted, every
time it is redrawn, and when it is re-attached after a restart -- and none of
them knows its server. So the answer is kept here per draft, filled when a
board is posted and, at startup, for every board still open, before any of
them is re-attached. A server given a library by library_tool, which runs in
its own process, shows the button on boards posted after the next restart.

It also keeps each open board's library line (the Cards field) true. Every
board in a server with a library -- rental or communal -- listens to that
library (services/library_events); when it publishes, the line is recomputed
and, only if it changed, the board is redrawn by update_draft_message, which
writes the line kept here. A board listens until its teams form, then keeps its
last line. A check that fails keeps the line it had.
"""
from datetime import datetime
from typing import Any, Optional

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

    async with AsyncSessionLocal() as session:
        any_library = select(LibraryServer.guild_id)
        rows = (await session.execute(
            select(DraftSession.session_id, DraftSession.guild_id).where(
                DraftSession.guild_id.in_(any_library),
                DraftSession.teams_start_time.is_(None),
                DraftSession.deletion_time > datetime.now()))).all()
    from services import library_events
    for sid, guild in rows:
        if str(sid) in _LINES:
            continue    # on_ready again after a reconnect: this line is already true
        await watch(sid, guild, None)
        _LINES.pop(str(sid), None)      # unknown until computed: the first redraw writes it
    for guild in {g for _, g in rows}:
        library_events.publish_for_guild(guild)
    logger.info("library: {} open boards watch their library line", len(rows))
    return len(ids)


UNTRACKED = object()        # not a library board: leave its Cards field alone
_LINES: "dict[str, Optional[str]]" = {}


def line_for(session_id: Any) -> object:
    """The board's current library line (a str, or None for "no line"), or
    UNTRACKED for a board in a server without a library."""
    return _LINES.get(str(session_id), UNTRACKED)


async def watch(session_id: Any, guild_id: Any, line: "Optional[str]") -> None:
    """Keep this board's line true from now on, starting from ``line``."""
    from services import library_events, library_service

    lid = await library_service.library_id_for(guild_id)
    if lid is None:
        return
    sid = str(session_id)
    _LINES[sid] = line

    async def on_change(_library_id: str) -> None:
        await _recompute(sid)

    library_events.subscribe(sid, on_change, lid)


async def _recompute(sid: str) -> None:
    from cube_views import pack_options
    from services import library_events

    async with AsyncSessionLocal() as session:
        draft = (await session.execute(
            select(DraftSession).where(DraftSession.session_id == sid))).scalar_one_or_none()
    if not _taking_signups(draft):
        # The line answers "can I play without owning the cards?", which only
        # matters while people can still join. The last line stays on the board.
        library_events.unsubscribe(sid)
        return
    assert draft is not None
    try:
        line = await pack_options.library_signup_check(draft.cube, draft.guild_id)
    except pack_options.LibraryLineUnknown:
        return          # never claim what wasn't checked: keep the board as it is
    # A watched board not in _LINES is pending (loaded at startup, never
    # computed): even a None line is stored and redrawn, which removes a
    # Cards field left over from before the restart.
    if sid in _LINES and line == _LINES[sid]:
        return
    _LINES[sid] = line
    if not draft.message_id:
        return          # not posted yet: the board's first redraw writes the line
    from bot_registry import get_bot
    await _redraw(get_bot(), sid)


async def _redraw(bot: Any, sid: str) -> None:
    from views import update_draft_message
    await update_draft_message(bot, sid)


def _taking_signups(draft: Any) -> bool:
    from services.library_reservation import FINISHED_STAGES
    return (draft is not None and draft.teams_start_time is None
            and draft.session_stage not in FINISHED_STAGES
            and (draft.deletion_time is None or draft.deletion_time > datetime.now()))
