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
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select

from database.db_session import db_session
from models.draft_session import DraftSession
from models.tournament import Tournament
from models.wallet_tx import WalletTx

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
    event: str               # entry | winnings | refund | prize | deposit | withdraw | settled | pay | adjust
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
    ("return:",         MTGO,       "withdraw", None, None),
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
