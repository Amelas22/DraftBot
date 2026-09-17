"""A trade's cards are booked once, however many settlers look at it.

`poll_until_settled` runs a guild-wide scan every 5s for every command in
flight, and the watchdog scans again every 10 minutes. So the same finished
trade is read by several settlers at once, and each one books what it carried.
Without a key on the booking they all succeed, and the borrower owes two or
three times what they actually hold.

The tix side never had this problem: its claims are keyed by job id. Cards were
keyed by a fresh uuid, which is a key that cannot collide and therefore cannot
protect anything.
"""
import asyncio

import pytest

from database.db_session import AsyncSessionLocal
from models.card_loan import CardLoan
from models.mtgo_job import MtgoJob
from services import debt_service, wallet_service
import services.card_lending_service as svc

pytestmark = pytest.mark.asyncio

GUILD, BORROWER = "g1", "u1"
MOVED = [{"name": "Swamp", "qty": 8}]


async def _owed():
    rows = await debt_service.get_open_card_positions(
        GUILD, BORROWER, wallet_service.HOUSE_MTGO)
    return {r["card_name"]: -r["net"] for r in rows}


async def test_two_settlers_booking_the_same_trade_owe_it_once(test_db):
    await asyncio.gather(*(svc._book_claim(GUILD, BORROWER, "borrow", MOVED, "job-1")
                           for _ in range(3)))

    assert await _owed() == {"Swamp": 8}, "three settlers, one obligation"


async def test_a_replay_after_a_crash_does_not_book_again(test_db):
    """A crash between booking and marking the batch resolved replays the
    booking on the next scan."""
    await svc._book_claim(GUILD, BORROWER, "borrow", MOVED, "job-1")
    await svc._book_claim(GUILD, BORROWER, "borrow", MOVED, "job-1")

    assert await _owed() == {"Swamp": 8}


async def test_a_different_trade_of_the_same_cards_is_its_own_claim(test_db):
    """Two real trades moving the same card are two obligations -- the key is
    the trade, not the card."""
    await svc._book_claim(GUILD, BORROWER, "borrow", MOVED, "job-1")
    await svc._book_claim(GUILD, BORROWER, "borrow", MOVED, "job-2")

    assert await _owed() == {"Swamp": 16}


async def test_the_library_does_not_claim_the_house_features_jobs(test_db):
    """/cards lend writes MtgoJob rows with kind='borrow' for the same guild
    and player. Those belong to the OTHER serve: polling one here asks the
    library about a job it has never heard of, reads the 404 as failure, and
    rolls back a live loan while refunding its deposit."""
    async with AsyncSessionLocal() as s:
        s.add(MtgoJob(job_id="house-1", kind="borrow", guild_id=GUILD,
                      player_id=BORROWER, mtgo_user="u", amount=4,
                      card_name="Lightning Bolt", status="pending"))
        s.add(MtgoJob(job_id="lib-1", kind="borrow", guild_id=GUILD,
                      player_id=BORROWER, mtgo_user="u", amount=8,
                      card_name=None, status="pending"))
        await s.commit()

    found = [j.job_id for j in await svc._batches_for(GUILD, BORROWER)]

    assert found == ["lib-1"], f"picked up another feature's job: {found}"
