"""Retrying a borrow must not mint tix.

The ledger is append-only, so a refund does not remove the hold's legs -- it
books a compensating pair. That makes "has this hold already been booked?" the
wrong question for an idempotency key that has to survive a retry: the second
borrow finds the first hold's legs, concludes it is already held, takes nothing,
and hands out the deck for free. The eventual release then pays out of a holder
that was never funded, and the borrower ends up with more tix than they started.

A failed handover is the COMMON case -- an unaccepted MTGO trade times out and
the bot tells the player to run /borrow again -- so this is reachable in normal
use, and the invented tix are withdrawable against the real vault.

tournament_escrow_service carries an attempt counter in its entry source for
exactly this reason.
"""
import pytest

from database.db_session import AsyncSessionLocal, db_session
from models.card_loan import CardLoan
from services import wallet_service
import services.card_lending_service as svc
from conftest import FakeLendingServe, stub_library

pytestmark = pytest.mark.asyncio

GUILD, BORROWER, HANDLE = "g1", "u1", "Borrower01"
DECK = [{"name": "Swamp", "qty": 1}]
# What the library can hand over. The dispatch checks the shelf before it
# opens a trade, and these tests never write custody rows, so without this
# every one of them refuses with "short_cards".
_STOCK = {c["name"]: 99 for c in DECK}


@pytest.fixture
def rig(monkeypatch):
    client = FakeLendingServe()
    monkeypatch.setattr(svc, "get_lending_client", lambda: client)

    async def handle(_):
        return HANDLE
    monkeypatch.setattr(svc, "_mtgo_handle", handle)
    stub_library(monkeypatch, svc, collateral=5, stock=_STOCK)
    # A cube that charges needs the wallet enabled; the refusal for one
    # that does not has its own test.
    monkeypatch.setattr(svc, "is_money_server", lambda gid: True)

    async def no_debt(*a, **k):
        return []
    monkeypatch.setattr(svc, "on_inflow", no_debt)
    return client


async def _balances():
    async with db_session() as s:
        return (await wallet_service.balance_in(s, GUILD, BORROWER),
                await wallet_service.balance_in(s, GUILD, svc.collateral_holder(GUILD)))


async def test_a_retried_borrow_does_not_create_tix(test_db, rig):
    """The whole sequence: hold, fail, refund, retry, return."""
    # Seed from a neutral holder, NOT the collateral wallet -- funding the
    # borrower out of the holder would leave it at -5 before anything happened
    # and make every later assertion about it meaningless.
    async with db_session() as s:
        await wallet_service.transfer_in(s, GUILD, "system:test-seed",
                                         BORROWER, 5, "seed:test", notes="opening")
    start_borrower, _ = await _balances()

    async with AsyncSessionLocal() as s:
        loan = CardLoan(guild_id=GUILD, library_id="lib", borrower_id=BORROWER, cards=DECK,
                        state="assigned", source="fixture")
        s.add(loan)
        await s.commit()

    # borrow -> the hold is taken
    assert (await svc.start_borrow(GUILD, BORROWER))[0] == "dispatched"
    # ...and the handover fails, so the deposit comes back
    rig.jobs["job-1"] = {"state": "failed", "detail": "trade timed out"}
    await svc.settle_in_flight()

    # retry, which is exactly what the bot tells the player to do. A retry is a
    # NEW trade and the serve issues a new id for it -- reusing the failed
    # one's is a stub convenience the settler now rejects outright, because a
    # job whose row is already resolved cannot be the one this dispatch just
    # opened.
    rig.job_id = "job-2"
    assert (await svc.start_borrow(GUILD, BORROWER))[0] == "dispatched"
    rig.jobs["job-2"] = {"state": "done"}
    await svc.settle_in_flight()

    borrower, holder = await _balances()
    assert holder == 5, f"the retry must actually hold collateral, holder={holder}"

    # ...and give it back on return. A third trade, so a third job id.
    rig.job_id = "job-3"
    assert (await svc.start_return(GUILD, BORROWER))[0] == "dispatched"
    rig.jobs["job-3"] = {"state": "done"}
    await svc.settle_in_flight()

    borrower, holder = await _balances()
    assert borrower == start_borrower, f"tix were created: {start_borrower} -> {borrower}"
    assert holder == 0, f"the collateral holder is out of balance: {holder}"
