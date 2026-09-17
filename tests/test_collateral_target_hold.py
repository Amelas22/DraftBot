"""The library's deposit, expressed as a target holding rather than a hold.

`set_collateral(..., n)` means "make this borrower's holding in the library's
collateral wallet equal n" -- the same rule draft_pool_service.set_entry uses
for a stake, and for the same reason. A borrow that fails and is retried is the
ORDINARY path here, not an edge case, so the question an idempotency key has to
answer is "has this movement happened?", never "is this player already paid up?"

Keying on state is what breaks: after a refund the borrower is back to holding
nothing, so a key derived from the balance repeats the first attempt's key and
is swallowed as a retry -- taking nothing and handing over the deck for free.
The key therefore counts MOVEMENTS with the holder, exactly as the draft pool's
does, and the amount to move is always the delta from what is actually held.

That leaves nothing for a row on the loan to cache: the holding IS the deposit.
"""
import pytest

from database.db_session import db_session
from services import wallet_service
import services.card_lending_service as svc

pytestmark = pytest.mark.asyncio

GUILD, BORROWER = "g1", "u1"


async def _fund(n):
    async with db_session() as s:
        await wallet_service.transfer_in(s, GUILD, "system:test-seed", BORROWER, n,
                                         f"seed:{n}", notes="opening")


async def _balances():
    async with db_session() as s:
        return (await wallet_service.balance_in(s, GUILD, BORROWER),
                await wallet_service.balance_in(s, GUILD, svc.collateral_holder(GUILD)))


async def test_taking_a_deposit_moves_it_into_the_library_wallet(test_db):
    await _fund(5)

    assert (await svc.set_collateral(GUILD, BORROWER, 1, 5))["ok"]

    assert await _balances() == (0, 5)


async def test_asking_for_what_is_already_held_moves_nothing(test_db):
    """The property that makes this safe to call from a retry, a watchdog, or a
    replayed command: it converges on the target instead of adding to it."""
    await _fund(10)
    await svc.set_collateral(GUILD, BORROWER, 1, 5)

    assert (await svc.set_collateral(GUILD, BORROWER, 1, 5))["ok"]

    assert await _balances() == (5, 5), "a second call must not charge again"


async def test_a_borrower_who_cannot_afford_it_is_told_how_short(test_db):
    await _fund(2)

    result = await svc.set_collateral(GUILD, BORROWER, 1, 5)

    assert result == {"ok": False, "deficit": 3}
    assert await _balances() == (2, 0), "nothing moves on a refusal"


async def test_setting_it_to_zero_gives_back_whatever_is_held(test_db):
    await _fund(5)
    await svc.set_collateral(GUILD, BORROWER, 1, 5)

    assert (await svc.set_collateral(GUILD, BORROWER, 1, 0))["ok"]

    assert await _balances() == (5, 0)


async def test_a_retried_borrow_charges_again_rather_than_being_swallowed(test_db):
    """The minting bug, at the level it is actually prevented. Hold, refund,
    hold again: the borrower ends up 5 lighter and the holder 5 heavier, not
    holding a deck they paid nothing for."""
    await _fund(5)

    await svc.set_collateral(GUILD, BORROWER, 1, 5)     # borrow
    await svc.set_collateral(GUILD, BORROWER, 1, 0)     # ...the handover fails
    await svc.set_collateral(GUILD, BORROWER, 1, 5)     # they try again

    assert await _balances() == (0, 5), "the retry must really take the deposit"


async def test_a_whole_cycle_leaves_nobody_up_or_down(test_db):
    await _fund(5)
    start, _ = await _balances()

    for _ in range(3):                                   # three failed attempts
        await svc.set_collateral(GUILD, BORROWER, 1, 5)
        await svc.set_collateral(GUILD, BORROWER, 1, 0)
    await svc.set_collateral(GUILD, BORROWER, 1, 5)      # one that lands
    await svc.set_collateral(GUILD, BORROWER, 1, 0)      # ...and comes back

    borrower, holder = await _balances()
    assert borrower == start, f"tix were created or destroyed: {start} -> {borrower}"
    assert holder == 0, f"the library wallet is out of balance: {holder}"


async def test_a_second_loan_is_charged_independently(test_db):
    """Loans finish and new ones start. The second deposit must not be read as
    a retry of the first just because the balances line up again."""
    await _fund(5)
    await svc.set_collateral(GUILD, BORROWER, 1, 5)
    await svc.set_collateral(GUILD, BORROWER, 1, 0)

    assert (await svc.set_collateral(GUILD, BORROWER, 2, 5))["ok"]

    assert await _balances() == (0, 5)
