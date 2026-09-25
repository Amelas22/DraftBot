"""A withdrawal whose answer was lost must not be abandoned.

The POST reached the serve; only the reply went missing. A real trade may be
open, and if the owner accepts it the cards leave the LIBRARY. With no job row,
nothing ever books the withdrawal: the ledger goes on saying the library owes
them, /mydeposits goes on listing them, and library_holdings goes on counting
them as lendable stock for everyone else -- so the same cards can be withdrawn
twice.

That is strictly worse than losing a deposit the same way, which the deposit
path already guards: there the cards are ones the library GAINED and did not
record, recoverable by hand from the boundary's own positions.
"""
from unittest.mock import AsyncMock

import pytest

from conftest import a_library
from database.db_session import AsyncSessionLocal
from models.mtgo_job import MtgoJob
import services.card_deposit_service as svc

pytestmark = pytest.mark.asyncio

GUILD, OWNER, LIB = "g-amb", "u-amb", "lib-amb"
HELD = [{"name": "Swamp", "qty": 2}]


def _client(orphan):
    """A serve that lost the answer, and whose job scan then finds the trade.

    The scan honours `exclude_ids` as the real one does -- it is the caller's
    only defence against adopting a trade of its own, and a stub that ignores
    the argument would keep passing if that defence were deleted.
    """
    c = AsyncMock()
    c.enabled = True
    c.withdraw_cards = AsyncMock(return_value={"_ambiguous": True})

    async def scan(job_type, mtgo_user, cards, exclude_ids=(), **kw):
        if orphan and str(orphan["id"]) in {str(i) for i in exclude_ids}:
            return None
        return orphan
    c.find_recent_deck_job = scan
    return c


async def _arrange(monkeypatch, orphan):
    await a_library(LIB, guild=GUILD, collateral=0)
    client = _client(orphan)
    monkeypatch.setattr(svc, "get_lending_client", lambda: client)
    monkeypatch.setattr(svc, "_mtgo_handle", AsyncMock(return_value="someone"))
    monkeypatch.setattr(svc, "library_busy_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "held_for", AsyncMock(return_value=HELD))
    # The shelf physically has them and none are out on loan, so the withdrawal
    # reaches the serve -- which is the only part these tests are about.
    monkeypatch.setattr(svc, "available_now",
                        AsyncMock(return_value={"Swamp": 99}))
    monkeypatch.setattr(svc, "library_available",
                        AsyncMock(return_value={"Swamp": 99}))


async def test_the_trade_the_scan_finds_is_adopted(test_db, monkeypatch):
    """Adopted means a job row exists, so settlement will find it and book the
    cards as gone -- which is what stops them being handed out again."""
    await _arrange(monkeypatch, {"id": "orphan-1"})

    status, job_id = await svc._start_withdrawal(GUILD, OWNER, cards=HELD)

    assert (status, job_id) == ("dispatched", "orphan-1")
    async with AsyncSessionLocal() as s:
        row = await s.get(MtgoJob, "orphan-1")
    assert row is not None and row.kind == "card-withdraw"
    assert row.library_id == LIB


async def test_no_matching_trade_is_still_parked_for_a_human(test_db, monkeypatch):
    """Nothing to adopt is not permission to invent one. The order stays
    unknown rather than being booked or written off."""
    await _arrange(monkeypatch, None)

    status, job_id = await svc._start_withdrawal(GUILD, OWNER, cards=HELD)

    assert status == "dispatch_unknown" and job_id is None
    async with AsyncSessionLocal() as s:
        assert (await s.get(MtgoJob, "orphan-1")) is None


async def test_a_second_run_does_not_adopt_the_trade_the_first_one_opened(
        test_db, monkeypatch):
    """The whole position rarely fits in one trade, so /withdraw runs several --
    and two of them can be identical on the three things the /jobs scan matches
    (type, handle, card list).

    Run one opens a trade and the poller gives up on it, leaving it pending for
    the watchdog. Run two's answer is lost, the scan offers back run one's
    still-open trade, and adopting it reports that trade's outcome as this
    one's: cards booked out of the library twice for one movement, and run
    two's real trade -- if it opened -- left with no row at all.
    """
    await _arrange(monkeypatch, {"id": "run-one-job"})
    client = svc.get_lending_client()
    client.withdraw_cards = AsyncMock(return_value={"id": "run-one-job"})

    first, _ = await svc._start_withdrawal(GUILD, OWNER, cards=HELD)
    assert first == "dispatched", "run one opened a trade nobody has settled"

    client.withdraw_cards = AsyncMock(return_value={"_ambiguous": True})
    status, job_id = await svc._start_withdrawal(GUILD, OWNER, cards=HELD)

    assert (status, job_id) == ("dispatch_unknown", None), \
        "run one's live trade is not run two's to adopt"
