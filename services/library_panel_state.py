"""What the draft chat's library panel should say, without saying it.

Who the panel lists, the embeds it shows, and which face it should wear. A
leaf: it imports no Discord views, so the panel's buttons (library_views) and
the code that posts the panel (services/library_panel) can both import it.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

import discord
from sqlalchemy import select

from database.db_session import AsyncSessionLocal
from models.card_loan import CardLoan
from models.draft_session import DraftSession
from services.draft_deck_assignment import loan_source
from services import wallet_service
from services.card_lending_service import owed_cards
from services.library_reminders import deck_summary
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


async def _cards_described(loan: CardLoan) -> Any:
    """The cards to describe this loan by: what the borrower has, not the pool.

    `loan.cards` is the whole drafted pool and stays so as the draft record, so
    a loan that moved three of forty cards would read as forty. Once cards are
    out, the claim ledger knows what crossed; before they go, the offer does.
    A ledger with nothing for a borrower who is out (a loan booked before the
    ledger knew) falls back to the pool, which is what it always said. Only the
    out states read the ledger, so a panel pays for the people holding cards.
    """
    if loan.state in {"borrowed", "return_pending"}:
        held = await owed_cards(wallet_service.library_scope(loan.library_id),
                                loan.borrower_id)
        return held or loan.cards
    if loan.state == "out_pending":
        return loan.offered_cards
    return loan.cards


async def borrowers_of(draft: Any) -> list[Borrower]:
    """Everyone the panel lists: the reserved plus anyone with a loan from this
    draft, each with their newest loan, sorted by name."""
    reserved = await library_users(draft)
    async with AsyncSessionLocal() as s:
        loans = (await s.execute(
            select(CardLoan).where(CardLoan.source == loan_source(draft.session_id))
            .order_by(CardLoan.id))).scalars().all()
    newest = {str(loan.borrower_id): loan for loan in loans}  # later ids win
    names = {str(k): v for k, v in (draft.sign_ups or {}).items()}
    out: list[Borrower] = []
    for uid in set(reserved) | set(newest):
        loan = newest.get(uid)
        out.append(Borrower(uid, names.get(uid, f"<@{uid}>"),
                            loan.state if loan else None,
                            await _cards_described(loan) if loan else None))
    return sorted(out, key=_sort_key)


async def has_library_involvement(draft: Any) -> bool:
    """Whether the draft ever touched the library: it reserved the cube's copy,
    someone agreed to borrow, or a deck was offered from it. A cheap test, so
    a draft with no library pays one indexed read, not borrowers_of."""
    if draft.library_reserved_at is not None or draft.library_agreed:
        return True
    async with AsyncSessionLocal() as s:
        found = await s.scalar(select(CardLoan.id).where(
            CardLoan.source == loan_source(draft.session_id)).limit(1))
    return found is not None


async def panel_borrowers(session_id: str) -> list[Borrower]:
    """borrowers_of the draft with this id; nobody for a draft that is gone."""
    draft = await DraftSession.get_by_session_id(session_id)
    return [] if draft is None else await borrowers_of(draft)


def all_back(borrowers: list[Borrower]) -> bool:
    return not any(b.state in OUT_STATES for b in borrowers)


def can_close(borrowers: list[Borrower]) -> bool:
    """Every deck is back and none is still on offer. A deck offered but not yet
    collected can still go out after the decision, until the watchdog retracts
    the offer, so the panel stays open for it."""
    return all_back(borrowers) and not any(b.state == "assigned" for b in borrowers)


class Face(Enum):
    """The face the panel wears. DraftSession.library_panel_mode stores its
    `mode`: NULL for NONE, else the value.

    CLOSED is one face whichever copy it closes: a borrow panel closes as a
    borrow panel ("back on the shelf"), a return panel as a return panel ("all
    decks are back"). Which one is decided by the copy that is live when it
    closes, i.e. by the face it had before; after that it is never drawn again.
    """
    NONE = "none"
    BORROW = "borrow"
    RETURN = "return"
    CLOSED = "closed"

    @classmethod
    def of(cls, mode: Optional[str]) -> "Face":
        """The face a stored library_panel_mode stands for."""
        return cls.NONE if mode is None else cls(mode)

    @property
    def mode(self) -> Optional[str]:
        """What library_panel_mode stores for this face."""
        return None if self is Face.NONE else self.value


def desired_face(current: Face, borrowers: list[Borrower], *, settled: bool,
                 chat_open: bool) -> Face:
    """The face the panel should wear now.

    `current` is the face it wears; `settled` is whether the draft is decided
    (a side clinched, or it was abandoned); `chat_open` is whether its draft
    chat still exists.

    - No panel yet: a borrow panel once the chat is open and someone is
      involved, while the draft is undecided. A panel is never first posted on
      a decided draft.
    - Borrow, undecided: stays borrow.
    - Borrow, decided: closes when every deck is back and none is on offer;
      stays borrow while a deck is on offer and none is out; otherwise asks for
      returns.
    - Return: closes when every deck is back and none is on offer.
    - Closed is final.
    - A posted panel whose chat is gone closes: there is nowhere to show it.
    """
    if current is Face.CLOSED:
        return Face.CLOSED
    if current is Face.NONE:
        if chat_open and borrowers and not settled:
            return Face.BORROW
        return Face.NONE
    if not chat_open:
        return Face.CLOSED
    if current is Face.RETURN:
        return Face.CLOSED if can_close(borrowers) else Face.RETURN
    if not settled:
        return Face.BORROW
    if can_close(borrowers):
        return Face.CLOSED
    if all_back(borrowers):  # a deck still on offer, none out
        return Face.BORROW
    return Face.RETURN


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
