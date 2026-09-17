"""Two deposits landing at once must charge once, quietly.

The target-hold rule is read-modify-write: read what is held, move the
difference. asyncio interleaves at every await and Discord dispatches each
interaction as its own task, so two clicks from one borrower can both read
"holds nothing" and both try to charge.

There are two defences, and it is worth being precise about which does what.
The ledger's unique index on (source, kind) is the hard one: overlapping calls
compute the SAME movement-counted key, so the second insert cannot land. Run
these tests with MONEY_LOCK neutered and they do not double-charge -- they raise
IntegrityError, in front of a player who clicked twice.

MONEY_LOCK is what turns that into a no-op. Held across the whole
read-modify-write rather than only around the transfer, it makes the second
caller read the deposit the first one booked and find nothing left to do. So
these tests pin the quiet behaviour; the money is safe either way.
"""
import asyncio

import pytest

from database.db_session import db_session
from services import wallet_service
import services.card_lending_service as svc

pytestmark = pytest.mark.asyncio

GUILD, BORROWER = "g1", "u1"


@pytest.fixture(autouse=True)
def _fresh_money_lock():
    """MONEY_LOCK is module-level, so it binds to whichever event loop first
    takes it and raises for every later test. Same treatment _DISPATCH_LOCK
    gets in test_library_queue.py."""
    wallet_service.MONEY_LOCK = asyncio.Lock()
    yield


async def _balances():
    async with db_session() as s:
        return (await wallet_service.balance_in(s, GUILD, BORROWER),
                await wallet_service.balance_in(s, GUILD, svc.collateral_holder(GUILD)))


async def test_two_simultaneous_deposits_charge_once(test_db):
    async with db_session() as s:
        await wallet_service.transfer_in(s, GUILD, "system:seed", BORROWER, 20,
                                         "seed", notes="opening")

    await asyncio.gather(*(svc.set_collateral(GUILD, BORROWER, 1, 5) for _ in range(4)))

    borrower, holder = await _balances()
    assert (borrower, holder) == (15, 5), \
        f"four overlapping deposits cost {20 - borrower} tix, not 5"


async def test_a_deposit_and_its_refund_racing_still_balance(test_db):
    """Whichever order they land in, the borrower is never left holding less
    than they started with and the holder never goes negative."""
    async with db_session() as s:
        await wallet_service.transfer_in(s, GUILD, "system:seed", BORROWER, 20,
                                         "seed", notes="opening")

    await asyncio.gather(
        svc.set_collateral(GUILD, BORROWER, 1, 5),
        svc.set_collateral(GUILD, BORROWER, 1, 0),
        svc.set_collateral(GUILD, BORROWER, 1, 5),
        svc.set_collateral(GUILD, BORROWER, 1, 0),
    )

    borrower, holder = await _balances()
    assert holder >= 0, f"the library wallet went negative: {holder}"
    assert borrower + holder == 20, f"tix were created or destroyed: {borrower}+{holder}"
