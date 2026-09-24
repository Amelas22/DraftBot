"""A settled loan does not say, by itself, what just happened to it.

A borrow that succeeded and a return that failed BOTH come to rest at
'borrowed' with no job in flight -- the first because the cards arrived, the
second because they never came home. Reading only the resting state therefore
tells a player who could not return their deck that it was collected
successfully, which is what happened to loan 1 on 2026-09-16.

The poller has to be told which direction it is watching.
"""
import pytest

from database.db_session import AsyncSessionLocal
from models.card_loan import CardLoan
import services.card_lending_service as svc

pytestmark = pytest.mark.asyncio

GUILD, BORROWER = "g1", "u1"
DECK = [{"name": "Swamp", "qty": 4}]


@pytest.fixture(autouse=True)
def _no_settling(monkeypatch):
    """The states below are already settled; polling must read, not re-settle."""
    async def noop(guild_id=None):
        return {}
    monkeypatch.setattr(svc, "settle_in_flight", noop)


async def _seed(state, job_id=None):
    async with AsyncSessionLocal() as s:
        loan = CardLoan(guild_id=GUILD, borrower_id=BORROWER, cards=DECK,
                        state=state, job_id=job_id, source="fixture")
        s.add(loan)
        await s.commit()
        return loan.id


async def test_a_completed_borrow_reads_as_borrowed(test_db):
    await _seed("borrowed")
    assert await svc.poll_until_settled(GUILD, BORROWER, "borrowed", timeout_s=0) == ("borrowed", None)


async def test_a_failed_borrow_reads_as_failed(test_db):
    await _seed("assigned")
    assert (await svc.poll_until_settled(GUILD, BORROWER, "borrowed", timeout_s=0))[0] == "failed"


async def test_a_failed_return_is_not_mistaken_for_a_borrow(test_db):
    """The reported bug: same resting state, opposite meaning."""
    await _seed("borrowed")
    assert (await svc.poll_until_settled(GUILD, BORROWER, "returned", timeout_s=0))[0] == "failed"


async def test_a_completed_return_reads_as_returned(test_db):
    await _seed("returned")
    assert await svc.poll_until_settled(GUILD, BORROWER, "returned", timeout_s=0) == ("returned", None)


async def test_a_trade_still_in_flight_reads_as_running(test_db):
    async with AsyncSessionLocal() as s:
        s.add(CardLoan(guild_id=GUILD, borrower_id=BORROWER, cards=DECK,
                       state="out_pending", job_id="job-1", source="fixture"))
        await s.commit()
    assert await svc.poll_until_settled(GUILD, BORROWER, "borrowed", timeout_s=0) == ("running", None)


async def test_a_poller_reports_on_its_own_trade_only(test_db, monkeypatch):
    """The poller watches a borrower, not a job, so a trade that finished and
    was replaced could be reported as this one's outcome.

    Reachable: a borrow fails, the bot tells the player to try again, they do --
    and the FIRST command's poller is still running. It then sees the second
    attempt reach 'borrowed' and tells the player their first trade succeeded.
    """
    loan_id = await _seed("out_pending", job_id="job-1")

    async def settle(guild_id=None):
        # job-1 fails and job-2 is dispatched before this poller looks again.
        async with AsyncSessionLocal() as s:
            loan = await s.get(CardLoan, loan_id)
            loan.state, loan.job_id = "out_pending", "job-2"
            await s.commit()
        return {}
    monkeypatch.setattr(svc, "settle_in_flight", settle)

    outcome, _ = await svc.poll_until_settled(GUILD, BORROWER, "borrowed",
                                              timeout_s=0, interval_s=0,
                                              job_id="job-1")

    assert outcome == "running", "a different job's progress is not ours to report"
