"""Reading the tix ledger for a person to look at.

`services/wallet_service.py` owns the money: append-only rows, balances, the
reconciliation invariant. This module owns the other half -- what a row MEANS --
and never writes anything.

Every writer already sets a structured `source`, because it doubles as the
transfer pair's idempotency key: `draft-entry:<session_id>:...`,
`tourney:<tournament_id>:<team_id>`, `debt:<link_id>`, `serve`, `wd:<key>`. That
makes the key a reliable description of the event, so the display text is derived
from it at read time rather than stored. The ledger is append-only by design: a
stored description would be frozen at the wording it was born with, and the rows
already booked would keep theirs forever.

Nothing here imports back into wallet_service's write path, and wallet_service
imports nothing from here -- which is why the paged query lives in this module
rather than beside get_history.
"""
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import select

from database.db_session import db_session
from models.draft_session import DraftSession
from models.tournament import Tournament
from models.wallet_tx import WalletTx
from services.wallet_service import is_system_account

DRAFT = "draft"
TOURNAMENT = "tournament"
DEBT = "debt"
MTGO = "mtgo"
TRANSFER = "transfer"
ADJUST = "adjust"


@dataclass(frozen=True)
class Origin:
    """What a ledger row was for. Derived from the row alone -- no I/O."""
    category: str            # DRAFT | TOURNAMENT | DEBT | MTGO | TRANSFER | ADJUST
    event: str               # entry | winnings | refund | prize | deposit | withdraw | returned | settled | pay | adjust
    ref: str | None = None   # the draft session_id or tournament id to name
    detail: str | None = None  # a draft refund's reason, which its key carries


# (prefix, category, event, ref index, detail index) -- the two indices are
# positions within the remainder of the key after the prefix, or None for a
# shape that carries no such segment.
#
# No prefix in the table is a prefix of another -- every entry is a complete
# distinct key prefix. When adding a new row, verify the new prefix is not a
# prefix of any existing one and vice versa. The indices differ per shape: a
# draft refund's key puts the reason first, so its session id is the second
# segment (index 1) and its reason the first, while every other shape leads
# with its ref and carries no reason at all.
KNOWN_PREFIXES: tuple[tuple[str, str, str, int | None, int | None], ...] = (
    ("draft-entry:",    DRAFT,      "entry",    0,    None),
    ("draft-payout:",   DRAFT,      "winnings", 0,    None),
    ("draft-refund:",   DRAFT,      "refund",   1,    0),
    ("refund:tourney:", TOURNAMENT, "refund",   0,    None),
    ("tourney:",        TOURNAMENT, "entry",    0,    None),
    ("payout:",         TOURNAMENT, "prize",    0,    None),
    ("debt:",           DEBT,       "settled",  None, None),
    ("wd:",             MTGO,       "withdraw", None, None),
    ("return:",         MTGO,       "returned", None, None),
)

# Exact-match sources. Not prefixes: 'serve' and 'admin' are whole keys.
_EXACT = {
    "serve": (MTGO, "deposit"),
    "admin": (ADJUST, "adjust"),
}

# The two `kind` values that mean tix crossed the boundary between here and
# MTGO. Named because they are the one part of an MTGO row that is reliable:
# the withdraw leg carries no source at all, so `kind` is all there is to
# recognize it by, and anything else that has to find those rows must agree
# with classify() about which kinds they are.
_MTGO_BOUNDARY_KINDS = ("deposit", "withdraw")


def classify(tx: WalletTx) -> Origin:
    """What this row was for. Pure: a function of the row's own columns.

    Falls through to a plain transfer for anything unrecognized, so a new writer
    that ships before this table learns its shape renders plainly instead of
    raising in front of someone reading their own balance.
    """
    source = tx.source or ""
    # A boundary crossing is identified by kind, not source: the withdraw row
    # carries no source at all (mtgo_resolution_service.py:278).
    if tx.kind in _MTGO_BOUNDARY_KINDS:
        return Origin(MTGO, tx.kind)
    if source in _EXACT:
        category, event = _EXACT[source]
        return Origin(category, event)
    for prefix, category, event, ref_index, detail_index in KNOWN_PREFIXES:
        if source.startswith(prefix):
            remainder = source[len(prefix):]
            return Origin(category, event,
                          ref=_segment(remainder, ref_index),
                          detail=_segment(remainder, detail_index))
    return Origin(TRANSFER, "pay")


def _segment(remainder: str, index: int | None) -> str | None:
    if index is None:
        return None
    parts = remainder.split(":")
    return parts[index] if len(parts) > index and parts[index] else None


async def resolve_labels(origins: Iterable[Origin]) -> dict[tuple[str, str], str]:
    """Names for the drafts and tournaments a page of rows points at.

    Two queries for a whole page, not two per row, and keyed by (category, ref)
    because a draft's ref is a session_id string while a tournament's is an
    integer id -- they share a namespace otherwise.

    A missing name is simply absent from the result. Cancelling a draft deletes
    its DraftSession row while the refund it books stays in the ledger forever,
    so absent is normal and the caller renders the event without a name.
    """
    origins = list(origins)
    drafts = {o.ref for o in origins if o.category == DRAFT and o.ref}
    tourneys = {o.ref for o in origins if o.category == TOURNAMENT and o.ref}
    labels: dict[tuple[str, str], str] = {}
    if not drafts and not tourneys:
        return labels

    async with db_session() as session:
        if drafts:
            rows = await session.execute(
                select(DraftSession.session_id, DraftSession.friendly_id)
                .where(DraftSession.session_id.in_(drafts)))
            labels.update({(DRAFT, sid): friendly
                           for sid, friendly in rows if friendly})
        # Tournament ids are integers in the database and strings in the key.
        ids = [int(t) for t in tourneys if t.isdigit()]
        if ids:
            rows = await session.execute(
                select(Tournament.id, Tournament.name).where(Tournament.id.in_(ids)))
            labels.update({(TOURNAMENT, str(tid)): name for tid, name in rows if name})
    return labels


FIELD_LIMIT = 1024  # Discord's cap on one embed field's value

# The most of a resolved name, a note or an MTGO username a line will show.
#
# A page of ten lines shares FIELD_LIMIT, so a line has around a hundred
# characters to work in, and the fixed half of one -- the amount, the event
# text, the separators and the timestamp -- accounts for about fifty. Every
# other part of a line is bounded by whatever writes it; these three are not
# (Tournament.name is String(128), a wallet note String(256), a deposit's
# counterparty MTGO username String(64)), so they are the only parts that can
# push a page over the cap. What happens then is worse than a long line:
# fit_field drops the tail, and a dropped row is not shortened but absent,
# while the balance above it still counts the money it moved.
DETAIL_LIMIT = 50

_EVENT_TEXT = {
    (DRAFT, "entry"): "Entry fee",
    (DRAFT, "winnings"): "Draft winnings",
    (DRAFT, "refund"): "Draft refund",
    (TOURNAMENT, "entry"): "Tournament entry",
    (TOURNAMENT, "refund"): "Tournament refund",
    (TOURNAMENT, "prize"): "Tournament prize",
    (DEBT, "settled"): "Debt settled",
    (MTGO, "deposit"): "Deposit from MTGO",
    (MTGO, "withdraw"): "Withdrawal to MTGO",
    (MTGO, "returned"): "Withdrawal returned",
    (ADJUST, "adjust"): "Adjustment",
}


def _short(text: str) -> str:
    """A name or note cut to what one line can hold."""
    return text if len(text) <= DETAIL_LIMIT else f"{text[:DETAIL_LIMIT - 1]}…"


def _person(counterparty_id: str | None) -> str | None:
    """A mention, but only for a real person: the counterparty may be an MTGO
    username or a synthetic holder (in-flight, a prize pool)."""
    if not counterparty_id or is_system_account(counterparty_id):
        return None
    return f"<@{counterparty_id}>"


def _what(tx: WalletTx, origin: Origin, labels: dict[tuple[str, str], str]) -> str:
    text = _EVENT_TEXT.get((origin.category, origin.event), origin.event.capitalize())
    if origin.category == TRANSFER:
        who = _person(tx.counterparty_id)
        direction = "Sent" if tx.amount < 0 else "Received"
        preposition = "to" if tx.amount < 0 else "from"
        return f"{direction} {preposition} {who}" if who else direction
    if origin.category == DEBT:
        who = _person(tx.counterparty_id)
        return f"{text} ↔ {who}" if who else text
    if origin.category == MTGO:
        # The MTGO username is what makes a deposit traceable to a trade, and a
        # deposit is the only MTGO row that carries one. The withdraw and return
        # legs name `system:in-flight` -- our own bookkeeping holder, meaningless
        # to the person reading their ledger.
        #
        # Named positively rather than as `not is_system_account(...)`, which
        # reads as the right guard and is not: it recognizes real holders by
        # being Discord snowflakes, so it calls every MTGO username synthetic
        # too and would drop the one id here that is worth printing.
        #
        # And cut like every other variable-length part of a line: the username
        # is WalletTx.counterparty_id, String(64), copied at link time from an
        # unvalidated String(128), so it is bounded by nothing a page can
        # afford.
        if origin.event == "deposit" and tx.counterparty_id:
            return f"{text} ({_short(tx.counterparty_id)})"
        return text
    if origin.category == ADJUST:
        return f"{text} · {_short(tx.notes)}" if tx.notes else text
    name = labels.get((origin.category, origin.ref or ""))
    if name:
        return f"{text} · {_short(name)}"
    # No name: a cancelled draft's row is gone, but its refund key still says why.
    return f"{text} ({origin.detail})" if origin.detail else text


def line(tx: WalletTx, origin: Origin, labels: dict[tuple[str, str], str]) -> str:
    """One rendered history line: signed amount, what it was, when.

    The timestamp is Discord's relative form, which renders in each viewer's own
    timezone and needs no formatting decision from us.
    """
    sign = "+" if tx.amount >= 0 else "−"
    rendered = f"`{sign}{abs(tx.amount)}` {_what(tx, origin, labels)}"
    when = tx.created_at
    return f"{rendered} — <t:{int(when.timestamp())}:R>" if when else rendered


async def describe_rows(rows: Sequence[WalletTx]) -> list[str]:
    """The display lines for a page of ledger rows, in the order given."""
    origins = [classify(tx) for tx in rows]
    labels = await resolve_labels(origins)
    return [line(tx, origin, labels) for tx, origin in zip(rows, origins)]


def fit_field(lines: Sequence[str]) -> str:
    """The lines as one embed field value, trimmed to what Discord accepts.

    A backstop, not the usual path: DETAIL_LIMIT bounds the one part of a line
    that has no length of its own, so a page of ten rows fits with room to
    spare. This catches what that does not -- a page grown longer, or a shape
    nothing here anticipated -- because dropping the overflow beats letting an
    over-long page fail to send.
    """
    field = ""
    for text in lines:
        candidate = f"{field}\n{text}" if field else text
        if len(candidate) > FIELD_LIMIT - 2:
            if field:
                return f"{field}\n…"
            # The ellipsis says something was cut; a lone line that fits the
            # field on its own had nothing cut from it.
            return text if len(text) <= FIELD_LIMIT else f"{text[:FIELD_LIMIT - 1]}…"
        field = candidate
    return field
