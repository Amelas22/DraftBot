"""The "Library decks" panel in a draft's shared chat.

One message that says who has a library deck and where it stands. It is posted
once, when the chat opens, for a draft that reserved library decks or already
has loans. library_panel_mode on the draft is both the "already posted" guard
and the face the panel wears: NULL (none), 'borrow', 'return' or 'closed'.
"""
from dataclasses import dataclass
from types import EllipsisType
from typing import Any, Optional

import discord
from loguru import logger
from sqlalchemy import select, update

from database.db_session import AsyncSessionLocal
from helpers.team_names import labels_for
from helpers.utils import as_messageable, not_none
from models.card_loan import CardLoan
from models.draft_session import DraftSession
from services.library_reminders import deck_summary, draft_state, session_of
from services.library_reservation import library_users

# A loan in one of these has its cards (or a trade for them) out of the library.
OUT_STATES: frozenset[str] = frozenset(
    {"out_pending", "borrowed", "return_pending", "dispatch_unknown"})

TITLE = "🏛️ Library decks"

_STATE_TEXT = {
    None: "⏳ being prepared",
    "assigned": "📦 ready, not collected",
    "out_pending": "🚚 trade on its way",
    "borrowed": "✅ collected",
    "return_pending": "↩️ return on its way",
    "dispatch_unknown": "🚚 trade on its way",
    "returned": "☑️ returned",
    "expired": "⌛ offer lapsed",
}


@dataclass(frozen=True)
class Borrower:
    user_id: str
    name: str
    # The loan's state, or None when the player is reserved with no loan yet.
    state: Optional[str]
    cards: Optional[list[Any]]


def _sort_key(b: Borrower) -> tuple[str, str]:
    return (b.name.lower(), b.user_id)


async def panel_borrowers(session_id: str) -> list[Borrower]:
    """Everyone the panel lists: the reserved plus anyone with a loan from this
    draft, each with their newest loan, sorted by name."""
    draft = await DraftSession.get_by_session_id(session_id)
    if draft is None:
        return []
    reserved = await library_users(draft)
    async with AsyncSessionLocal() as s:
        loans = (await s.execute(
            select(CardLoan).where(CardLoan.source == f"draft:{session_id}")
            .order_by(CardLoan.id))).scalars().all()
    newest = {str(loan.borrower_id): loan for loan in loans}  # later ids win
    names = {str(k): v for k, v in (draft.sign_ups or {}).items()}
    out: list[Borrower] = []
    for uid in set(reserved) | set(newest):
        loan = newest.get(uid)
        out.append(Borrower(uid, names.get(uid, f"<@{uid}>"),
                            loan.state if loan else None,
                            loan.cards if loan else None))
    return sorted(out, key=_sort_key)


def borrow_embed(borrowers: list[Borrower], *, closed: bool = False) -> discord.Embed:
    lines = []
    for b in borrowers:
        if closed and b.state in (None, "assigned", "expired"):
            text = "not collected, back on the shelf"
        else:
            text = _STATE_TEXT.get(b.state, str(b.state))
        lines.append(f"**{b.name}**: {text}")
    if closed:
        footer = "This draft's library decks are closed."
    else:
        footer = ("Press **Borrow my deck** and the library will trade you "
                  "your pool on MTGO.")
    return discord.Embed(title=TITLE, description="\n".join(lines) + f"\n\n{footer}")


async def _set_mode(session_id: str, *, expect: "Optional[str] | EllipsisType" = ...,
                    **values: Any) -> bool:
    """Write `values` to the draft's row; with `expect`, only while the mode is
    still that (None for no panel). True when the row changed."""
    cond = DraftSession.session_id == session_id
    if expect is None:
        cond = cond & DraftSession.library_panel_mode.is_(None)
    elif not isinstance(expect, EllipsisType):
        cond = cond & (DraftSession.library_panel_mode == expect)
    async with AsyncSessionLocal() as s:
        res = await s.execute(update(DraftSession).where(cond).values(**values))
        await s.commit()
        return bool(res.rowcount)  # type: ignore[attr-defined]


async def post_library_panel(bot: Any, session_id: str) -> None:
    """Post the borrow panel in the draft chat, once. No chat, nobody involved,
    or already posted: nothing happens."""
    draft = await DraftSession.get_by_session_id(session_id)
    if draft is None or not draft.draft_chat_channel or draft.library_panel_mode:
        return
    borrowers = await panel_borrowers(session_id)
    if not borrowers:
        return
    channel = bot.get_channel(int(draft.draft_chat_channel))
    if channel is None:
        return
    # Claim before sending, so two racing callers cannot both post.
    if not await _set_mode(session_id, expect=None, library_panel_mode="borrow"):
        return
    # Here, not at the top: the view's gate reads panel_borrowers from this module.
    from library_views.panel_view import panel_view
    try:
        sent = await as_messageable(channel).send(
            embed=borrow_embed(borrowers), view=not_none(panel_view("borrow")))
    except Exception:
        await _set_mode(session_id, library_panel_mode=None)
        raise
    await _set_mode(session_id, library_panel_message_id=str(sent.id))
    logger.info("Posted library panel for session_id={}", session_id)


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


def return_embed(borrowers: list[Borrower], result: Optional[str], *,
                 closed: bool = False) -> discord.Embed:
    """The panel once the draft is decided: who still holds a deck, who has
    sent theirs back. A deck nobody collected was never lent, so is not listed.
    `closed` is its final face, once every deck is back."""
    lead = f"{result}\n\n" if result else ""
    ask = ("All decks are back. Thanks!" if closed
           else "If you borrowed a deck, please send it back. "
                "Other drafters are waiting on those cards.")
    embed = discord.Embed(title=TITLE, description=lead + ask)
    out = [f"**{b.name}**: {deck_summary(b.cards)}"
           for b in borrowers if b.state in OUT_STATES]
    back = [f"**{b.name}**" for b in borrowers if b.state == "returned"]
    # A field may not be empty, so a side with nobody on it is left off.
    if out:
        embed.add_field(name="Still out", value="\n".join(out), inline=False)
    if back:
        embed.add_field(name="Back", value="\n".join(back), inline=False)
    if not closed:
        embed.set_footer(text="Move the cards into your trade binder first")
    return embed


def all_back(borrowers: list[Borrower]) -> bool:
    return not any(b.state in OUT_STATES for b in borrowers)


def can_close(borrowers: list[Borrower]) -> bool:
    """Every deck is back and none is still on offer. A deck offered but not yet
    collected can still go out after the decision, until the watchdog retracts
    the offer, so the panel stays open for it."""
    return all_back(borrowers) and not any(b.state == "assigned" for b in borrowers)


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


async def _edit_panel(channel: Any, message_id: Optional[str], **fields: Any) -> None:
    """Edit the posted panel; one already gone is not an error."""
    message = await _fetch_panel(channel, message_id)
    if message is None:
        return
    try:
        await message.edit(**fields)
    except discord.NotFound:
        pass


async def switch_to_returns(bot: Any, session_id: str) -> None:
    """Once the draft is decided, turn the borrow panel into a return reminder.

    With every deck back (or none ever collected) the borrow panel just takes
    its final state in place. While a deck is still on offer it stays a borrow
    panel, for the watchdog to switch once the offer is collected or lapses.
    Otherwise it is posted again below the victory post, in return mode, and
    the old one deleted. Anything but a borrow panel on a settled draft:
    nothing happens. The mode's compare-and-set is the guard, so two racing
    callers post once.
    """
    draft = await DraftSession.get_by_session_id(session_id)
    if (draft is None or draft.library_panel_mode != "borrow"
            or not draft.draft_chat_channel):
        return
    if not (await draft_state(session_id)).settled:
        return
    channel = await _chat_channel(bot, draft.draft_chat_channel)
    if channel is None:
        return
    borrowers = await panel_borrowers(session_id)
    old_id = draft.library_panel_message_id

    if all_back(borrowers):
        if not can_close(borrowers):
            return
        if not await _set_mode(session_id, expect="borrow", library_panel_mode="closed"):
            return
        await _edit_panel(channel, old_id,
                          embed=borrow_embed(borrowers, closed=True), view=None)
        logger.info("Closed library panel for session_id={}", session_id)
        return

    if not await _set_mode(session_id, expect="borrow", library_panel_mode="return"):
        return
    from library_views.panel_view import panel_view  # see post_library_panel
    view = not_none(panel_view("return"))
    try:
        sent = await as_messageable(channel).send(
            embed=return_embed(borrowers, await result_line(draft)), view=view)
    except Exception:
        # Back to borrow, so the watchdog's reconcile tries again.
        await _set_mode(session_id, expect="return", library_panel_mode="borrow")
        raise
    await _set_mode(session_id, library_panel_message_id=str(sent.id))
    # Sticky before the old panel goes, though that one never was: deleting a
    # message drops the channel's sticky record only when it is that message.
    try:
        await _make_sticky(bot, draft, sent, view)
    except Exception:
        logger.exception("library panel: could not make the return panel for "
                         "session_id={} sticky", session_id)
    old = await _fetch_panel(channel, old_id)
    if old is not None:
        try:
            await old.delete()
        except discord.NotFound:
            pass
    logger.info("Library panel for session_id={} now asks for returns", session_id)


async def _make_sticky(bot: Any, draft: Any, message: Any, view: Any) -> None:
    # Here, not at the top: message_management imports views, which is cyclic.
    from database.message_management import (LIBRARY_PANEL_VIEW_TYPE,
                                             make_message_sticky)

    await make_message_sticky(
        str(draft.guild_id), str(draft.draft_chat_channel), message, view, bot,
        view_metadata={"view_type": LIBRARY_PANEL_VIEW_TYPE,
                       "draft_session_id": draft.session_id})


async def _drop_sticky(channel_id: str, session_id: str) -> None:
    """Forget this draft's panel as the chat's sticky, so it stops moving."""
    from database.message_management import (
        LIBRARY_PANEL_VIEW_TYPE, delete_sticky_message_record, fetch_sticky_message)

    async with AsyncSessionLocal() as s:
        sticky = await fetch_sticky_message(channel_id, s)
        if sticky is None:
            return
        meta = sticky.view_metadata or {}
        if (meta.get("view_type") == LIBRARY_PANEL_VIEW_TYPE
                and meta.get("draft_session_id") == session_id):
            await delete_sticky_message_record(sticky, s)
            await s.commit()


async def refresh_library_panel(bot: Any, session_id: str) -> None:
    """Redraw the panel from the loans as they are now, in the face it wears.

    Borrow mode re-renders in place, so "trade on its way" and "collected" are
    live. Return mode does too, until every deck is back and none is still on
    offer: then the panel closes, loses its button and stops being sticky.
    Closed, or never posted: nothing.
    Re-reads everything, so a burst of calls costs edits, not correctness.
    """
    draft = await DraftSession.get_by_session_id(session_id)
    if (draft is None or not draft.draft_chat_channel
            or draft.library_panel_mode not in ("borrow", "return")):
        return
    channel = await _chat_channel(bot, draft.draft_chat_channel)
    if channel is None:
        return
    borrowers = await panel_borrowers(session_id)
    message_id = draft.library_panel_message_id
    from library_views.panel_view import panel_view  # see post_library_panel

    if draft.library_panel_mode == "borrow":
        await _edit_panel(channel, message_id, embed=borrow_embed(borrowers),
                          view=panel_view("borrow"))
        return
    result = await result_line(draft)
    if not can_close(borrowers):
        await _edit_panel(channel, message_id, embed=return_embed(borrowers, result),
                          view=panel_view("return"))
        return
    if not await _set_mode(session_id, expect="return", library_panel_mode="closed"):
        return
    await _edit_panel(channel, message_id,
                      embed=return_embed(borrowers, result, closed=True), view=None)
    await _drop_sticky(str(draft.draft_chat_channel), session_id)
    logger.info("Every library deck from session_id={} is back; panel closed",
                session_id)


async def after_sticky_repost(bot: Any, session_id: str) -> None:
    """Bring the copy the sticky manager just posted in step with the loans.

    A refresh that closed the panel while the repost was in flight closed the
    old copy and left the new one live. So a closed panel gets its closed face
    again on whatever copy the committed pointer names now, and its sticky is
    dropped; both are idempotent. Anything else is an ordinary refresh.
    """
    draft = await DraftSession.get_by_session_id(session_id)
    if (draft is None or draft.library_panel_mode != "closed"
            or not draft.draft_chat_channel):
        await refresh_library_panel(bot, session_id)
        return
    channel = await _chat_channel(bot, draft.draft_chat_channel)
    if channel is None:
        return
    borrowers = await panel_borrowers(session_id)
    await _edit_panel(channel, draft.library_panel_message_id,
                      embed=return_embed(borrowers, await result_line(draft),
                                         closed=True), view=None)
    await _drop_sticky(str(draft.draft_chat_channel), session_id)


def loan_changed(loan: Any) -> None:
    """A loan moved: refresh its draft's panel in the background. Called from
    the lending code after its commits, so it never raises and never waits."""
    try:
        session_id = session_of(loan)
        if session_id is None:
            return
        import bot_registry

        bot = bot_registry.get_bot()
        if bot is None:
            return
        from helpers.money_gate import spawn_followup

        spawn_followup("library panel refresh", refresh_library_panel(bot, session_id))
    except Exception:
        logger.exception("library panel: could not schedule a refresh for loan {}",
                         getattr(loan, "id", None))


async def _repost_return_panel(bot: Any, draft: Any, channel: Any,
                               borrowers: list[Borrower]) -> None:
    """Post the return panel again, sticky, after the live copy went missing."""
    session_id = draft.session_id
    from library_views.panel_view import panel_view  # see post_library_panel
    view = not_none(panel_view("return"))
    sent = await as_messageable(channel).send(
        embed=return_embed(borrowers, await result_line(draft)), view=view)
    if not await _set_mode(session_id, expect="return",
                           library_panel_message_id=str(sent.id)):
        # Closed while we were posting: the copy just sent is not wanted.
        try:
            await sent.delete()
        except discord.NotFound:
            pass
        return
    try:
        await _make_sticky(bot, draft, sent, view)
    except Exception:
        logger.exception("library panel: could not make the reposted return panel "
                         "for session_id={} sticky", session_id)
    logger.info("Reposted the missing library return panel for session_id={}",
                session_id)


async def _reconcile_one(bot: Any, draft: Any) -> None:
    session_id = draft.session_id
    mode = draft.library_panel_mode
    channel = (await _chat_channel(bot, draft.draft_chat_channel)
               if draft.draft_chat_channel else None)
    if channel is None:
        # The chat is gone, so there is nowhere to show a panel: stop looking.
        if await _set_mode(session_id, expect=mode, library_panel_mode="closed"):
            logger.info("library panel: draft chat for session_id={} is gone; "
                        "panel closed", session_id)
        return
    if mode == "borrow":
        # Switches only a settled draft. An undecided one whose panel is
        # missing is left be: a second borrow panel is worse than none.
        await switch_to_returns(bot, session_id)
        return
    borrowers = await panel_borrowers(session_id)
    message_id = draft.library_panel_message_id
    if all_back(borrowers) or await _fetch_panel(channel, message_id) is not None:
        await refresh_library_panel(bot, session_id)
        return
    # The sticky manager may have just moved the panel, deleting the copy this
    # scan read: only a pointer that still names the missing message is stale.
    now = await DraftSession.get_by_session_id(session_id)
    if (now is None or now.library_panel_mode != "return"
            or now.library_panel_message_id != message_id):
        return
    await _repost_return_panel(bot, now, channel, borrowers)


async def reconcile_library_panels(bot: Any) -> None:
    """Bring every open panel in step with its draft, from the watchdog's tick.

    A settled draft still showing its borrow panel (a restart, or a failed
    victory post, between the decision and the switch) is switched. A return
    panel is redrawn, or reposted if its message is gone while decks are out.
    A panel whose chat no longer exists is closed. Closed and never-posted
    panels are not read at all.
    """
    async with AsyncSessionLocal() as s:
        drafts = (await s.execute(select(DraftSession).where(
            DraftSession.library_panel_mode.in_(("borrow", "return")))
        )).scalars().all()
    for draft in drafts:
        try:
            await _reconcile_one(bot, draft)
        except Exception:
            logger.exception("library panel: reconcile failed for session_id={}",
                             draft.session_id)
