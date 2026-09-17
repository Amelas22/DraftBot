"""Tix held against a borrowed deck.

The hold is taken BEFORE the cards leave and released only once they are back.
Both reversals are keyed by source and therefore idempotent, which is what makes
it safe for the command's poller and the watchdog to both attempt them -- the
same property release_draft_pool relies on for every path that ends a draft
early.

Nothing here forfeits: a deck that never comes back leaves its collateral held
until a human intervenes. That is deliberate, so no code path can quietly take
somebody's tix on a timer.
"""
from unittest.mock import AsyncMock

import pytest

from database.db_session import AsyncSessionLocal
from models.card_loan import CardLoan
import services.card_lending_service as svc
from conftest import FakeLendingServe

pytestmark = pytest.mark.asyncio

GUILD, BORROWER, HANDLE = "g1", "u1", "Borrower01"
DECK = [{"name": "Swamp", "qty": 4}]


@pytest.fixture
def rig(monkeypatch):
    client = FakeLendingServe()
    monkeypatch.setattr(svc, "get_lending_client", lambda: client)

    async def handle(_):
        return HANDLE
    monkeypatch.setattr(svc, "_mtgo_handle", handle)
    monkeypatch.setattr(svc, "card_library_collateral", lambda gid: 5)

    # Every deposit movement, as a target: ("set", borrower, amount). The real
    # figures are covered in test_collateral_target_hold.py; what matters here
    # is WHEN the deposit is asked for relative to the cards moving.
    moves = []

    async def set_it(guild_id, borrower_id, loan_id, amount, expect_job=None):
        moves.append(("set", borrower_id, amount))
        return {"ok": True, "deficit": 0}

    monkeypatch.setattr(svc, "set_collateral", AsyncMock(side_effect=set_it))
    return client, moves


async def _seed(state="assigned", job_id=None):
    async with AsyncSessionLocal() as s:
        loan = CardLoan(guild_id=GUILD, borrower_id=BORROWER, cards=DECK, state=state,
                        job_id=job_id, source="fixture")
        s.add(loan)
        await s.commit()
        return loan.id


async def _loan(loan_id):
    async with AsyncSessionLocal() as s:
        return await s.get(CardLoan, loan_id)


async def test_the_hold_is_taken_before_the_cards_leave(test_db, rig):
    client, moves = rig
    loan_id = await _seed()

    status, _ = await svc.start_borrow(GUILD, BORROWER)

    assert status == "dispatched"
    assert moves[0] == ("set", BORROWER, 5), "the deposit is taken before dispatching"
    assert client.lent, "and the trade still goes out"


async def test_a_borrower_who_cannot_cover_it_keeps_their_deck_on_the_shelf(test_db, rig, monkeypatch):
    client, moves = rig
    monkeypatch.setattr(svc, "set_collateral",
                        AsyncMock(return_value={"ok": False, "deficit": 3}))
    loan_id = await _seed()

    status, _ = await svc.start_borrow(GUILD, BORROWER)

    assert status == "short_funds"
    assert client.lent == [], "no cards move when the hold fails"
    assert (await _loan(loan_id)).state == "assigned"


async def test_a_failed_handover_gives_the_collateral_back(test_db, rig):
    """The borrower paid for cards that never arrived -- the same unwind as a
    cancelled draft, not a special case."""
    client, moves = rig
    loan_id = await _seed(state="out_pending", job_id="job-1")
    client.jobs["job-1"] = {"state": "failed", "detail": "trade timed out"}

    await svc.settle_in_flight()

    assert ("set", BORROWER, 0) in moves, "the deposit goes back"
    assert (await _loan(loan_id)).state == "assigned"


async def test_a_completed_handover_keeps_the_collateral_held(test_db, rig):
    """They have the cards now; the tix stay held until the deck comes back."""
    client, moves = rig
    loan_id = await _seed(state="out_pending", job_id="job-1")
    client.jobs["job-1"] = {"state": "done", "give": DECK}

    await svc.settle_in_flight()

    assert moves == [], "nothing is returned while the deck is out"
    assert (await _loan(loan_id)).state == "borrowed"


async def test_returning_the_deck_releases_the_collateral(test_db, rig):
    client, moves = rig
    loan_id = await _seed(state="return_pending", job_id="job-1")
    client.jobs["job-1"] = {"state": "done", "receive": DECK}

    await svc.settle_in_flight()

    assert ("set", BORROWER, 0) in moves, "the deposit goes back"
    assert (await _loan(loan_id)).state == "returned"


async def test_a_failed_return_keeps_the_collateral_held(test_db, rig):
    """The cards did not come back, so neither do the tix."""
    client, moves = rig
    loan_id = await _seed(state="return_pending", job_id="job-1")
    client.jobs["job-1"] = {"state": "failed"}

    await svc.settle_in_flight()

    assert moves == []
    assert (await _loan(loan_id)).state == "borrowed"


async def test_a_library_that_charges_nothing_touches_no_wallet(test_db, rig, monkeypatch):
    """Most guilds will run without collateral; they must not acquire a money
    dependency by existing."""
    client, moves = rig
    monkeypatch.setattr(svc, "card_library_collateral", lambda gid: 0)
    loan_id = await _seed()

    status, _ = await svc.start_borrow(GUILD, BORROWER)

    assert status == "dispatched"
    assert moves == [], "no deposit, no wallet involvement at all"


async def test_a_returned_deposit_is_drawn_against_what_they_owe(test_db, rig, monkeypatch):
    """Debts are not discretionary in this bot.

    on_inflow is documented as the thing every path putting tix into a wallet
    must call, and the tournament escrow refund does. Without it a borrower who
    owes the league gets their deposit back as spendable tix, where every other
    inflow would have settled it -- which makes the library a way to hold money
    out of reach of the debt system.
    """
    client, moves = rig
    seen = []

    async def fake_inflow(guild_id, player_id, *a, **k):
        seen.append((guild_id, player_id))
        return []

    monkeypatch.setattr(svc, "on_inflow", fake_inflow)
    loan_id = await _seed(state="return_pending", job_id="job-1")
    client.jobs["job-1"] = {"state": "done", "receive": DECK}

    await svc.settle_in_flight()

    assert ("set", BORROWER, 0) in moves, "the deposit came back"
    assert seen == [(GUILD, BORROWER)], "and the debt system was told about it"
