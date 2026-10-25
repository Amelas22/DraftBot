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
from services.library_reminders import deck_summary, draft_state
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


def return_embed(borrowers: list[Borrower], result: Optional[str]) -> discord.Embed:
    """The panel once the draft is decided: who still holds a deck, who has
    sent theirs back. A deck nobody collected was never lent, so is not listed."""
    lead = f"{result}\n\n" if result else ""
    embed = discord.Embed(
        title=TITLE,
        description=lead + "If you borrowed a deck, please send it back. "
                           "Other drafters are waiting on those cards.")
    out = [f"**{b.name}**: {deck_summary(b.cards)}"
           for b in borrowers if b.state in OUT_STATES]
    back = [f"**{b.name}**" for b in borrowers if b.state == "returned"]
    # A field may not be empty, so a side with nobody on it is left off.
    if out:
        embed.add_field(name="Still out", value="\n".join(out), inline=False)
    if back:
        embed.add_field(name="Back", value="\n".join(back), inline=False)
    embed.set_footer(text="Move the cards into your trade binder first")
    return embed


def all_back(borrowers: list[Borrower]) -> bool:
    return not any(b.state in OUT_STATES for b in borrowers)


async def _fetch_panel(channel: Any, message_id: Optional[str]) -> Optional[Any]:
    """The posted panel, or None when it is gone (deleted by hand, or never
    stored)."""
    if not message_id:
        return None
    try:
        return await as_messageable(channel).fetch_message(int(message_id))
    except discord.NotFound:
        return None


async def switch_to_returns(bot: Any, session_id: str) -> None:
    """Once the draft is decided, turn the borrow panel into a return reminder.

    With every deck back (or none ever collected) the borrow panel just takes
    its final state in place. Otherwise it is posted again below the victory
    post, in return mode, and the old one deleted. Anything but a borrow panel
    on a settled draft: nothing happens. The mode's compare-and-set is the
    guard, so two racing callers post once.
    """
    draft = await DraftSession.get_by_session_id(session_id)
    if (draft is None or draft.library_panel_mode != "borrow"
            or not draft.draft_chat_channel):
        return
    if not (await draft_state(session_id)).settled:
        return
    channel = bot.get_channel(int(draft.draft_chat_channel))
    if channel is None:
        return
    borrowers = await panel_borrowers(session_id)
    old_id = draft.library_panel_message_id

    if all_back(borrowers):
        if not await _set_mode(session_id, expect="borrow", library_panel_mode="closed"):
            return
        old = await _fetch_panel(channel, old_id)
        if old is not None:
            await old.edit(embed=borrow_embed(borrowers, closed=True), view=None)
        logger.info("Closed library panel for session_id={}", session_id)
        return

    if not await _set_mode(session_id, expect="borrow", library_panel_mode="return"):
        return
    from library_views.panel_view import panel_view  # see post_library_panel
    try:
        sent = await as_messageable(channel).send(
            embed=return_embed(borrowers, await result_line(draft)),
            view=not_none(panel_view("return")))
    except Exception:
        # Back to borrow, so the watchdog's reconcile tries again.
        await _set_mode(session_id, expect="return", library_panel_mode="borrow")
        raise
    await _set_mode(session_id, library_panel_message_id=str(sent.id))
    old = await _fetch_panel(channel, old_id)
    if old is not None:
        try:
            await old.delete()
        except discord.NotFound:
            pass
    logger.info("Library panel for session_id={} now asks for returns", session_id)
