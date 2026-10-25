"""The "Library decks" panel in a draft's shared chat.

One message that says who has a library deck and where it stands. It is posted
once, when the chat opens, for a draft that reserved library decks or already
has loans. library_panel_mode on the draft is both the "already posted" guard
and the face the panel wears: NULL (none), 'borrow', 'return' or 'closed'.
"""
from dataclasses import dataclass
from typing import Any, Optional

import discord
from loguru import logger
from sqlalchemy import select, update

from database.db_session import AsyncSessionLocal
from helpers.utils import as_messageable, not_none
from models.card_loan import CardLoan
from models.draft_session import DraftSession
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
        footer = "Use the library command to collect a deck that is ready."
    return discord.Embed(title=TITLE, description="\n".join(lines) + f"\n\n{footer}")


async def _set_mode(session_id: str, expect_null: bool, **values: Any) -> bool:
    cond = DraftSession.session_id == session_id
    if expect_null:
        cond = cond & DraftSession.library_panel_mode.is_(None)
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
    if not await _set_mode(session_id, True, library_panel_mode="borrow"):
        return
    # Here, not at the top: the view's gate reads panel_borrowers from this module.
    from library_views.panel_view import panel_view
    try:
        sent = await as_messageable(channel).send(
            embed=borrow_embed(borrowers), view=not_none(panel_view("borrow")))
    except Exception:
        await _set_mode(session_id, False, library_panel_mode=None)
        raise
    await _set_mode(session_id, False, library_panel_message_id=str(sent.id))
    logger.info("Posted library panel for session_id={}", session_id)
