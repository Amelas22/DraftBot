"""Waiting for the library when it is already trading with someone else.

The serve trades with ONE person at a time. Dispatching anyway does not fail --
it queues inside the serve -- so the borrower is told to accept a trade window
that will not open for minutes, which reads as a broken bot. Better to hold
their place, wait for the library to come free, and tell them when it is really
their turn.

Serialising here rather than relying on the serve's own queue also keeps the
collateral honest: the hold is taken at dispatch, so a borrower who waits ten
minutes is not charged for those ten minutes.
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

import services.card_lending_service as svc

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fresh_lock():
    svc._DISPATCH_LOCK = asyncio.Lock()
    yield


async def test_a_free_library_dispatches_straight_away(monkeypatch):
    monkeypatch.setattr(svc, "library_busy_reason", AsyncMock(return_value=None))
    started = AsyncMock(return_value=("dispatched", None))
    monkeypatch.setattr(svc, "start_borrow", started)

    status, waited = await svc.borrow_when_free("g1", "u1", poll_s=0)

    assert (status, waited) == ("dispatched", False)
    assert started.await_count == 1


async def test_a_busy_library_is_waited_for_rather_than_refused(monkeypatch):
    """Two 'busy' answers then free: the borrower keeps their place."""
    answers = ["busy with 1 other trade", "busy with 1 other trade", None]
    monkeypatch.setattr(svc, "library_busy_reason",
                        AsyncMock(side_effect=answers))
    started = AsyncMock(return_value=("dispatched", None))
    monkeypatch.setattr(svc, "start_borrow", started)

    status, waited = await svc.borrow_when_free("g1", "u1", poll_s=0)

    assert status == "dispatched"
    assert waited is True, "the caller needs to know it queued, to say so"
    assert started.await_count == 1, "dispatch happens once, after the wait"


async def test_giving_up_waiting_leaves_the_deck_untouched(monkeypatch):
    """A library busy for an hour must not leave a caller hanging forever, and
    must not dispatch on the way out."""
    monkeypatch.setattr(svc, "library_busy_reason",
                        AsyncMock(return_value="busy with 1 other trade"))
    started = AsyncMock(return_value=("dispatched", None))
    monkeypatch.setattr(svc, "start_borrow", started)

    status, _ = await svc.borrow_when_free("g1", "u1", poll_s=0, timeout_s=0)

    assert status == "still_busy"
    assert started.await_count == 0, "nothing dispatched after giving up"


async def test_two_borrowers_do_not_race_for_the_same_serve(monkeypatch):
    """The serve trades with one person at a time, so two /borrow calls landing
    together must not both open a trade window."""
    inside = {"now": 0, "most": 0}

    async def slow_start(guild_id, borrower_id, offering=None):
        inside["now"] += 1
        inside["most"] = max(inside["most"], inside["now"])
        await asyncio.sleep(0.02)
        inside["now"] -= 1
        return ("dispatched", None)

    monkeypatch.setattr(svc, "library_busy_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "start_borrow", slow_start)

    await asyncio.gather(svc.borrow_when_free("g1", "u1", poll_s=0),
                         svc.borrow_when_free("g1", "u2", poll_s=0))

    assert inside["most"] == 1, "two trades were opened at once"


async def test_a_return_does_not_race_a_borrow_for_the_same_serve(monkeypatch):
    """A return is a trade like any other. Letting /return dispatch straight
    past the queue means one player is told to accept a handover while another
    is told to accept a collection, and the serve can only open one window --
    so the second sits unexplained until it times out.
    """
    inside = {"now": 0, "most": 0}

    async def slow(guild_id, borrower_id, offering=None):
        inside["now"] += 1
        inside["most"] = max(inside["most"], inside["now"])
        await asyncio.sleep(0.02)
        inside["now"] -= 1
        return ("dispatched", None)

    monkeypatch.setattr(svc, "library_busy_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "start_borrow", slow)
    monkeypatch.setattr(svc, "start_return", slow)

    await asyncio.gather(svc.borrow_when_free("g1", "u1", poll_s=0),
                         svc.return_when_free("g1", "u2", poll_s=0))

    assert inside["most"] == 1, "a borrow and a return opened at once"


async def test_a_returner_waits_for_a_busy_library_too(monkeypatch):
    busy = AsyncMock(side_effect=["someone else is trading", None])
    monkeypatch.setattr(svc, "library_busy_reason", busy)
    monkeypatch.setattr(svc, "start_return", AsyncMock(return_value=("dispatched", None)))

    status, waited = await svc.return_when_free("g1", "u1", poll_s=0)

    assert (status, waited) == ("dispatched", True)
