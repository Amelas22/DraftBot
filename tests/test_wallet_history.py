"""What a wallet ledger row was FOR.

Every writer sets a structured `source` -- it is the transfer pair's idempotency
key, so it is well-formed and unique per event. These tests pin the mapping from
that key to something a player can read.
"""
import pytest

from models.wallet_tx import WalletTx
from services import wallet_history as wh


def _tx(kind="pay", amount=-10, source=None, counterparty_id=None, notes=None):
    """An unattached ledger row. classify() is pure, so no database is needed."""
    return WalletTx(kind=kind, amount=amount, source=source,
                    counterparty_id=counterparty_id, notes=notes)


@pytest.mark.parametrize("source, kind, category, event, ref", [
    ("draft-entry:sid1:p1:0:0-10", "pay", wh.DRAFT, "entry", "sid1"),
    ("draft-payout:sid1:p1", "receive", wh.DRAFT, "winnings", "sid1"),
    ("draft-refund:cancelled:sid1:p1:0", "receive", wh.DRAFT, "refund", "sid1"),
    ("tourney:3:18", "pay", wh.TOURNAMENT, "entry", "3"),
    ("tourney:3:18:1", "pay", wh.TOURNAMENT, "entry", "3"),
    ("refund:tourney:3:18", "receive", wh.TOURNAMENT, "refund", "3"),
    ("payout:3:1", "receive", wh.TOURNAMENT, "prize", "3"),
    ("debt:93c531db", "receive", wh.DEBT, "settled", None),
    ("serve", "deposit", wh.MTGO, "deposit", None),
    ("wd:commit-1", "pay", wh.MTGO, "withdraw", None),
    ("return:wd:commit-1", "receive", wh.MTGO, "withdraw", None),
    (None, "withdraw", wh.MTGO, "withdraw", None),
    ("admin", "receive", wh.ADJUST, "adjust", None),
    ("430aa70e-cf55-4f0e-86ef-36e7174518fb", "pay", wh.TRANSFER, "pay", None),
])
def test_classify_maps_every_source_the_codebase_writes(source, kind, category, event, ref):
    origin = wh.classify(_tx(kind=kind, source=source))
    assert (origin.category, origin.event, origin.ref) == (category, event, ref)


def test_refund_carries_its_reason():
    """The reason is in the key, so a cancelled draft's refund can still explain
    itself after the draft row it points at has been deleted."""
    assert wh.classify(_tx(source="draft-refund:cancelled:sid1:p1:0")).detail == "cancelled"


def test_a_tournament_refund_is_not_read_as_an_entry():
    """Verify a tournament refund classifies as a refund, not an entry."""
    origin = wh.classify(_tx(source="refund:tourney:3:18", kind="receive"))
    assert origin.event == "refund"


def test_an_unknown_source_degrades_to_a_plain_transfer():
    """A writer that ships a new source shape before this table learns it must
    produce a plainer line, never an exception in front of a player's money."""
    origin = wh.classify(_tx(source="some-future-thing:9", kind="pay"))
    assert origin.category == wh.TRANSFER
