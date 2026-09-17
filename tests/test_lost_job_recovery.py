"""A trade the library has forgotten must not strand the loan forever -- and a
trade it has NOT forgotten must not be rolled back.

Restarting the serve empties its job list. Any loan mid-trade then points at a
job id that answers 404, and treating "cannot read this job" as "still running"
leaves the loan in *_pending permanently: its owner cannot return (one is
already in flight) and cannot borrow (they hold an active loan). That is what
happened to loan 3 on 2026-09-16.

The mirror error is worse. Rolling a job back that is merely unanswered puts the
deck on the shelf while the borrower is looking at a live trade window, and lets
the next borrow hand the same cards out a second time.

So the two are told apart by asking the serve, which answers 404 for a job it
does not have and 500 -- or nothing at all -- when it cannot say. `get_job(...,
mark_missing=True)` reports the first as {"_missing": True} and the second as
None: a fact and an absence of one, never inferred from health or from a job
listing that might simply be truncated.
"""
from unittest.mock import AsyncMock

import pytest

from database.db_session import AsyncSessionLocal
from models.card_loan import CardLoan
import services.card_lending_service as svc

pytestmark = pytest.mark.asyncio

GUILD, BORROWER = "g1", "u1"
DECK = [{"name": "Swamp", "qty": 4}]
JOB = "gone-job"


def _client(job=None):
    """`job` is exactly what get_job answers: a projection, {"_missing": True}
    for a job the serve denies having, or None when it could not say."""
    c = AsyncMock()
    c.enabled = True
    c.get_job = AsyncMock(return_value=job)
    return c


async def _seed(state, job_id=JOB):
    async with AsyncSessionLocal() as s:
        loan = CardLoan(guild_id=GUILD, borrower_id=BORROWER, cards=DECK,
                        state=state, job_id=job_id, source="fixture")
        s.add(loan)
        await s.commit()
        return loan.id


async def _state(loan_id):
    async with AsyncSessionLocal() as s:
        loan = await s.get(CardLoan, loan_id)
        return loan.state, loan.job_id


async def test_a_forgotten_return_lets_the_borrower_try_again(test_db, monkeypatch):
    """The live case: the serve is up and has never heard of the job."""
    monkeypatch.setattr(svc, "get_lending_client", lambda: _client(job={"_missing": True}))
    loan_id = await _seed("return_pending")

    await svc.settle_in_flight()

    assert await _state(loan_id) == ("borrowed", None), \
        "they still hold the deck, and /return must be possible again"


async def test_a_forgotten_handover_frees_the_deck(test_db, monkeypatch):
    monkeypatch.setattr(svc, "get_lending_client", lambda: _client(job={"_missing": True}))
    loan_id = await _seed("out_pending")

    await svc.settle_in_flight()

    assert await _state(loan_id) == ("assigned", None)


async def test_a_job_the_serve_could_not_answer_for_is_left_alone(test_db, monkeypatch):
    """One failed read -- a 500, a dropped connection, a serve that is down.
    The trade may be on the borrower's screen right now."""
    monkeypatch.setattr(svc, "get_lending_client", lambda: _client(job=None))
    loan_id = await _seed("out_pending")

    await svc.settle_in_flight()

    assert await _state(loan_id) == ("out_pending", JOB), "nothing may move"


async def test_a_running_job_is_still_left_alone(test_db, monkeypatch):
    monkeypatch.setattr(svc, "get_lending_client",
                        lambda: _client(job={"state": "running"}))
    loan_id = await _seed("out_pending")

    await svc.settle_in_flight()

    assert await _state(loan_id) == ("out_pending", JOB)
