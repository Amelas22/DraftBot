"""The "Library decks" panel in a draft's shared chat.

One message that says who has a library deck and where it stands. Everything
that can change it -- the chat opening, the draft being decided, a loan
moving, the sticky manager wanting it at the bottom, the watchdog's tick --
calls sync_panel, which reads the draft as it is now, works out the face the
panel should wear (library_panel_state.desired_face) and makes the message and
the draft's row match. One lock per draft keeps two of those from interleaving.

DraftSession.library_panel_mode is the face the panel wears and
library_panel_message_id the one pointer to its live copy. Only sync_panel
writes either. The channel's sticky record follows the pointer: sync_panel
sets it whenever it posts a return copy, and the sticky manager's repost runs
through sync_panel too, under the same lock.
"""
import asyncio
import json
import weakref
from typing import Any, Optional

import discord
from loguru import logger
from sqlalchemy import select, update

from database.db_session import AsyncSessionLocal
from helpers.team_names import labels_for
from helpers.utils import as_messageable, not_none
from library_views.panel_view import LibraryPanelView, panel_view
from models.draft_session import DraftSession
from services.library_panel_state import (Borrower, Face, all_back, borrow_embed,
                                          borrowers_of, desired_face, return_embed)
from services.library_reminders import draft_state, session_of


async def result_line(draft: Any) -> Optional[str]:
    """"Team Red won 5–3", or "Draw 4–4", counted over every played game.

    None for swiss, whose standings are in the victory embed just above the
    panel, and for an abandoned draft, which has no result.
    """
    if draft.session_type == "swiss" or draft.session_stage == "abandoned":
        return None
    from utils import calculate_team_wins  # utils imports half the bot

    a, b = await calculate_team_wins(draft.session_id)
    if a == b:
        return f"Draw {a}–{b}"
    red, blue = labels_for(draft)
    winner = red if a > b else blue
    return f"{winner.name} won {max(a, b)}–{min(a, b)}"


async def _render(draft: Any, borrowers: list[Borrower], face: Face, *,
                  copy: Face) -> tuple[discord.Embed, Optional[LibraryPanelView]]:
    """The embed and buttons for `face`. `copy` is the face of the copy being
    drawn on, which only matters for CLOSED: a borrow panel closes as one."""
    if face is Face.BORROW or (face is Face.CLOSED and copy is Face.BORROW):
        embed = borrow_embed(borrowers, closed=face is Face.CLOSED)
    else:
        embed = return_embed(borrowers, await result_line(draft),
                             closed=face is Face.CLOSED)
    return embed, panel_view(face.value)


# --- Discord ------------------------------------------------------------------


async def _chat_channel(bot: Any, channel_id: Any) -> Optional[Any]:
    """The draft chat, from the cache or else from Discord; None once it is gone
    (its rooms were cleaned up)."""
    channel = bot.get_channel(int(channel_id))
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(int(channel_id))
    except discord.NotFound:
        return None


async def _fetch_panel(channel: Any, message_id: Optional[str]) -> Optional[Any]:
    """The posted panel, or None when it is gone (deleted by hand, or never
    stored)."""
    if not message_id:
        return None
    try:
        return await as_messageable(channel).fetch_message(int(message_id))
    except discord.NotFound:
        return None


# What each live copy was last drawn with, so a redraw that changes nothing
# costs no edit. In memory: after a restart the first redraw edits once.
_shown: dict[str, str] = {}


def _digest(embed: discord.Embed, view: Optional[LibraryPanelView]) -> str:
    buttons: list[Optional[str]] = (
        [getattr(item, "custom_id", None) for item in view.children] if view else [])
    return json.dumps([embed.to_dict(), buttons], sort_keys=True, default=str)


async def _show(message: Any, embed: discord.Embed,
                view: Optional[LibraryPanelView]) -> None:
    """Draw the panel on a live copy, unless it already shows exactly that. A
    copy deleted meanwhile is not an error."""
    digest = _digest(embed, view)
    if _shown.get(str(message.id)) == digest:
        return
    try:
        await message.edit(embed=embed, view=view)
    except discord.NotFound:
        return
    _shown[str(message.id)] = digest


async def _delete(message: Any) -> None:
    """Delete a copy of the panel; one already gone is not an error."""
    _shown.pop(str(message.id), None)
    try:
        await message.delete()
    except discord.NotFound:
        pass


async def _unpin(message: Any) -> None:
    """Unpin a closed panel. Gone, or not ours to unpin: nothing more to do."""
    if not getattr(message, "pinned", False):
        return
    try:
        await message.unpin()
    except (discord.NotFound, discord.Forbidden):
        pass


# --- The draft's row and the channel's sticky record ---------------------------


async def _write(session_id: str, **values: Any) -> None:
    """Write `values` to the draft's row."""
    async with AsyncSessionLocal() as s:
        await s.execute(update(DraftSession)
                        .where(DraftSession.session_id == session_id).values(**values))
        await s.commit()


async def _make_sticky(bot: Any, draft: Any, message: Any,
                       view: Optional[LibraryPanelView]) -> None:
    # Here, not at the top: message_management imports views, which is cyclic.
    from database.message_management import (LIBRARY_PANEL_VIEW_TYPE,
                                             make_message_sticky)

    await make_message_sticky(
        str(draft.guild_id), str(draft.draft_chat_channel), message, view, bot,
        view_metadata={"view_type": LIBRARY_PANEL_VIEW_TYPE,
                       "draft_session_id": draft.session_id})


async def _our_sticky_names(channel_id: str, session_id: str,
                            message_id: Optional[str]) -> bool:
    """Whether the chat's sticky record is this draft's panel at `message_id`.
    A record of this panel that names any other copy is stale, and is dropped."""
    from database.message_management import (
        LIBRARY_PANEL_VIEW_TYPE, delete_sticky_message_record, fetch_sticky_message)

    async with AsyncSessionLocal() as s:
        sticky = await fetch_sticky_message(channel_id, s)
        if sticky is None:
            return False
        meta = sticky.view_metadata or {}
        if (meta.get("view_type") != LIBRARY_PANEL_VIEW_TYPE
                or meta.get("draft_session_id") != session_id):
            return False
        if sticky.message_id == message_id:
            return True
        await delete_sticky_message_record(sticky, s)
        await s.commit()
        return False


async def _drop_sticky(channel_id: str, session_id: str) -> None:
    """Forget this draft's panel as the chat's sticky, so it stops moving."""
    await _our_sticky_names(channel_id, session_id, None)


# --- Converging ----------------------------------------------------------------


async def _post(bot: Any, draft: Any, channel: Any, borrowers: list[Borrower],
                face: Face, *, replacing: Optional[Any] = None) -> None:
    """Post a copy wearing `face` at the bottom of the chat and point the draft
    at it; a return copy is made sticky. Then the copy it replaces goes.

    A failed send changes nothing, so the next sync tries again.
    """
    embed, view = await _render(draft, borrowers, face, copy=face)
    sent = await as_messageable(channel).send(embed=embed, view=not_none(view))
    await _write(draft.session_id, library_panel_mode=face.mode,
                 library_panel_message_id=str(sent.id))
    _shown[str(sent.id)] = _digest(embed, view)
    if face is Face.RETURN:
        # Before the replaced copy is deleted: deleting the message a sticky
        # record names drops the record, so it must name the new copy first.
        try:
            await _make_sticky(bot, draft, sent, view)
        except Exception:
            logger.exception("library panel: could not make the return panel for "
                             "session_id={} sticky", draft.session_id)
    if replacing is not None:
        await _delete(replacing)


async def _converge(bot: Any, session_id: str, *, move_down: bool) -> None:
    draft = await DraftSession.get_by_session_id(session_id)
    if draft is None:
        return
    current = Face.of(draft.library_panel_mode)
    chat = str(draft.draft_chat_channel) if draft.draft_chat_channel else None
    if current is Face.CLOSED:
        # Closed is final. A sticky record that outlived the close goes when
        # the sticky manager next asks about it.
        if move_down and chat:
            await _drop_sticky(chat, session_id)
        return
    borrowers = await borrowers_of(draft)
    channel = await _chat_channel(bot, chat) if chat else None
    settled = (await draft_state(session_id)).settled
    face = desired_face(current, borrowers, settled=settled,
                        chat_open=channel is not None)
    if channel is None or chat is None:
        if face is Face.CLOSED:
            await _write(session_id, library_panel_mode=Face.CLOSED.mode)
            logger.info("library panel: draft chat for session_id={} is gone; "
                        "panel closed", session_id)
        return
    live = await _fetch_panel(channel, draft.library_panel_message_id)

    if face is Face.NONE:
        return
    if face is current:
        if live is None:
            # A return panel lost while decks are out is posted again. A lost
            # borrow panel is left be: a second one is worse than none.
            if face is Face.RETURN and not all_back(borrowers):
                await _post(bot, draft, channel, borrowers, face)
                logger.info("Reposted the missing library return panel for "
                            "session_id={}", session_id)
            return
        if (face is Face.RETURN and move_down
                and await _our_sticky_names(chat, session_id, str(live.id))):
            await _post(bot, draft, channel, borrowers, face, replacing=live)
            return
        await _show(live, *await _render(draft, borrowers, face, copy=face))
        return
    if face is Face.BORROW:  # from none
        await _post(bot, draft, channel, borrowers, face)
        logger.info("Posted library panel for session_id={}", session_id)
        return
    if face is Face.RETURN:  # from borrow: below the victory post
        await _post(bot, draft, channel, borrowers, face, replacing=live)
        logger.info("Library panel for session_id={} now asks for returns", session_id)
        return
    # Closed, from borrow or return: in place, with no button, and still.
    if live is not None:
        await _show(live, *await _render(draft, borrowers, face, copy=current))
        await _unpin(live)
    await _write(session_id, library_panel_mode=Face.CLOSED.mode)
    await _drop_sticky(chat, session_id)
    logger.info("Closed library panel for session_id={}", session_id)


# One lock per draft, held while its panel converges. Weak values: a lock
# lives only while someone holds or waits on it. Kept per event loop, since an
# asyncio lock belongs to the loop it is used on: a task stranded on a closed
# loop (a restart in tests) must not hold a later loop's draft forever.
_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, weakref.WeakValueDictionary[str, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary())


def _lock_for(session_id: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    locks = _locks.get(loop)
    if locks is None:
        locks = weakref.WeakValueDictionary()
        _locks[loop] = locks
    lock = locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        locks[session_id] = lock
    return lock


async def sync_panel(bot: Any, session_id: str, *, move_down: bool = False) -> None:
    """Bring the draft's panel in step with the draft as it is now.

    Posts it, redraws it in place, moves it below the victory post when the
    draft is decided with decks out, posts it again if it went missing while
    decks are out, and closes it in place once every deck is back. Already
    right: nothing. Safe to call any number of times from anywhere.

    `move_down` is the sticky manager asking for the return panel at the
    bottom of the chat: it is posted again there and the old copy deleted.
    """
    async with _lock_for(session_id):
        await _converge(bot, session_id, move_down=move_down)


async def nudge(bot: Any, session_id: str) -> None:
    """sync_panel for a caller whose own work must not fail with it."""
    try:
        await sync_panel(bot, session_id)
    except Exception:
        logger.exception("library panel: sync failed for session_id={}", session_id)


def loan_changed(loan: Any) -> None:
    """A loan moved: sync its draft's panel in the background. Called from the
    lending code after its commits, so it never raises and never waits."""
    try:
        session_id = session_of(loan)
        if session_id is None:
            return
        import bot_registry

        bot = bot_registry.get_bot()
        if bot is None:
            return
        from helpers.money_gate import spawn_followup

        spawn_followup("library panel sync", sync_panel(bot, session_id))
    except Exception:
        logger.exception("library panel: could not schedule a sync for loan {}",
                         getattr(loan, "id", None))


async def reconcile_library_panels(bot: Any) -> None:
    """Sync every open panel, from the lending watchdog's tick.

    Catches what a missed trigger left behind: a decided draft still showing
    its borrow panel (a restart, or a failed victory post, between the decision
    and the switch), a return panel whose message is gone, a panel whose chat
    no longer exists. Closed and never-posted panels are not read at all.
    """
    async with AsyncSessionLocal() as s:
        session_ids = (await s.execute(select(DraftSession.session_id).where(
            DraftSession.library_panel_mode.in_(
                (Face.BORROW.mode, Face.RETURN.mode))))).scalars().all()
    for session_id in session_ids:
        try:
            await sync_panel(bot, session_id)
        except Exception:
            logger.exception("library panel: reconcile failed for session_id={}",
                             session_id)
