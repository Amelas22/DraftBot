"""Two clicks that overlap must not charge a player twice.

The bot is single-threaded, which is not the same as serialised: asyncio
interleaves at every await, and Discord dispatches each interaction as its own
task. set_entry reads what a player holds, reads whether the queue is open,
counts their movements, and only then moves money -- three awaits during which
another submission from the same player can run all the way through.

Charging the same amount twice is already harmless, because the idempotency key
comes out identical and the booked transfer is found and reused. Two DIFFERENT
amounts are the hazard: the keys differ, so both charge, and the player pays
for every submission in flight rather than the one they meant.

set_entry answers this by doing the whole read-modify-write in one transaction
under MONEY_LOCK -- the same lock every transfer already takes, so the delta it
computes is still true when it is written.
"""
import asyncio

import pytest
import pytest_asyncio

from conftest import seed_session
from services import draft_pool_service as pool
from services import wallet_service


@pytest_asyncio.fixture(autouse=True)
async def _fresh_lock(test_db):
    """Rebind MONEY_LOCK to this test's event loop.

    An asyncio.Lock binds to a loop only when it is actually contended, so the
    rest of the suite never notices. These tests contend it on purpose, and
    pytest-asyncio gives each test a new loop, so a lock bound by the previous
    test would raise "bound to a different event loop" here.
    """
    wallet_service.MONEY_LOCK = asyncio.Lock()
    await seed_session("s1", guild="g", stype="staked", stage=None,
                       teams=(["p1"], ["b1"]))


@pytest.mark.asyncio
async def test_the_same_stake_submitted_twice_is_charged_once(test_db):
    """Idempotency covers this one: both submissions build the same key."""
    await wallet_service.adjust("g", "p1", 500, "seed", "t")

    await asyncio.gather(pool.set_entry("g", "s1", "p1", 50),
                         pool.set_entry("g", "s1", "p1", 50))

    assert await pool.held_by("g", "s1", "p1") == 50
    assert await wallet_service.get_balance("g", "p1") == 450


@pytest.mark.asyncio
async def test_two_different_stakes_in_flight_charge_only_one(test_db):
    """The player meant one of them, not the sum of both.

    Without serialisation both submissions read held=0 and the same movement
    count, build different keys, and both go through: the player is charged
    50 AND 100 and ends up holding 150 for a stake they declared as at most 100.
    """
    await wallet_service.adjust("g", "p1", 500, "seed", "t")

    await asyncio.gather(pool.set_entry("g", "s1", "p1", 50),
                         pool.set_entry("g", "s1", "p1", 100))

    held = await pool.held_by("g", "s1", "p1")
    assert held in (50, 100), (
        f"holding {held}: both submissions were charged instead of one")
    assert await wallet_service.get_balance("g", "p1") == 500 - held


@pytest.mark.asyncio
async def test_a_burst_of_submissions_charges_only_the_last_one_through(test_db):
    """Eight overlapping clicks cost the player one stake, not their sum."""
    await wallet_service.adjust("g", "p1", 500, "seed", "t")
    amounts = [10 * (i + 1) for i in range(8)]

    await asyncio.gather(*[pool.set_entry("g", "s1", "p1", n) for n in amounts])

    held = await pool.held_by("g", "s1", "p1")
    assert held in amounts, (
        f"holding {held}, but the largest stake submitted was {max(amounts)} "
        f"-- overlapping clicks were charged cumulatively")
    assert await wallet_service.get_balance("g", "p1") == 500 - held
    await pool.check_pool("g", "s1")
