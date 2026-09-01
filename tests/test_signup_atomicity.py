"""The charge and the sign-up land together, or neither does.

Money moved in one transaction and the roster written in another is the gap
this closes: a failure between the two leaves a player charged for a draft they
are not in, and nothing reconciles it -- check_pool deliberately says nothing
while the queue is open, because a contributor who is not yet signed up is
normal at that moment.
"""
import pytest
import pytest_asyncio
from sqlalchemy import select

from conftest import seed_session
from database.db_session import db_session
from models.draft_session import DraftSession
from services import draft_pool_service as pool
from services import wallet_service


@pytest_asyncio.fixture(autouse=True)
async def _queue(test_db):
    await seed_session("s1", guild="g", stype="staked", stage=None,
                       teams=(["p1"], ["b1"]), sign_ups={})


async def _sign_ups():
    async with db_session() as session:
        return (await session.execute(
            select(DraftSession.sign_ups)
            .where(DraftSession.session_id == "s1"))).scalars().first() or {}


@pytest.mark.asyncio
async def test_a_failed_signup_write_takes_the_charge_with_it(test_db):
    """The whole point: no charge without a seat."""
    await wallet_service.adjust("g", "p1", 500, "seed", "t")

    with pytest.raises(RuntimeError):
        async with wallet_service.MONEY_LOCK:
            async with db_session() as session:
                charged = await pool.entry_in(session, "g", "s1", "p1", 50, "joined")
                assert charged["ok"]
                raise RuntimeError("the sign-up write blew up")

    assert await wallet_service.get_balance("g", "p1") == 500, (
        "the player was charged for a draft they never joined")
    assert await pool.pool_balance("g", "s1") == 0
    assert await _sign_ups() == {}


@pytest.mark.asyncio
async def test_a_successful_signup_commits_the_money_and_the_seat_together(test_db):
    await wallet_service.adjust("g", "p1", 500, "seed", "t")

    async with wallet_service.MONEY_LOCK:
        async with db_session() as session:
            charged = await pool.entry_in(session, "g", "s1", "p1", 50, "joined")
            assert charged["ok"]
            draft = (await session.execute(
                select(DraftSession).where(DraftSession.session_id == "s1"))
            ).scalars().first()
            draft.sign_ups = {"p1": "Ada"}

    assert await wallet_service.get_balance("g", "p1") == 450
    assert await pool.pool_balance("g", "s1") == 50
    assert await _sign_ups() == {"p1": "Ada"}


@pytest.mark.asyncio
async def test_an_unaffordable_entry_writes_nothing(test_db):
    await wallet_service.adjust("g", "p1", 10, "seed", "t")

    async with wallet_service.MONEY_LOCK:
        async with db_session() as session:
            charged = await pool.entry_in(session, "g", "s1", "p1", 50, "joined")

    assert charged["ok"] is False and charged["deficit"] == 40
    assert await wallet_service.get_balance("g", "p1") == 10
    assert await pool.pool_balance("g", "s1") == 0
