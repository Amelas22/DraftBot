"""A partial borrow that fails must leave the deck exactly as it was.

Trimming the loan at the moment the borrower says "take what's available"
looked right, but it changes the deck before anything has moved -- so a trade
that then fails leaves them permanently holding a smaller deck they never
received. Repeat it and the deck ratchets down every attempt.

The rest of this module already follows the rule this breaks: the claim moves
only when a job reaches a terminal state. What was offered is recorded
separately, and becomes the deck only once the handover completes.

The offer is also not written down until the trade that carries it is accepted.
An offer persisted in advance is one a queue timeout can strand and a second
click can rewrite while the first trade is still in flight -- so it travels as
an argument, and `pending_cards` is committed by the dispatch itself.
"""
import pytest

from database.db_session import AsyncSessionLocal
from models.card_loan import CardLoan
import services.card_lending_service as svc
from conftest import FakeLendingServe

pytestmark = pytest.mark.asyncio

GUILD, BORROWER = "g1", "u1"
FULL = [{"name": "Cathar's Companion", "qty": 2}, {"name": "Swamp", "qty": 8}]


async def _seed(state="assigned", cards=None, pending=None, job_id=None):
    async with AsyncSessionLocal() as s:
        loan = CardLoan(guild_id=GUILD, borrower_id=BORROWER, state=state,
                        cards=cards or FULL, pending_cards=pending, job_id=job_id,
                        source="fixture")
        s.add(loan)
        await s.commit()
        return loan.id


async def _loan(loan_id):
    async with AsyncSessionLocal() as s:
        return await s.get(CardLoan, loan_id)

async def test_working_out_what_is_there_writes_nothing(test_db, monkeypatch):
    monkeypatch.setattr(svc, "get_lending_client",
                        lambda: FakeLendingServe({"Cathar's Companion": 1, "Swamp": 6}))
    loan_id = await _seed()

    trimmed = await svc.trim_to_available(GUILD, loan_id)

    assert trimmed == [{"name": "Cathar's Companion", "qty": 1},
                       {"name": "Swamp", "qty": 6}]
    loan = await _loan(loan_id)
    assert loan.cards == FULL, "nothing has moved, so the deck is untouched"
    assert loan.pending_cards is None, "and nothing is staged until a trade carries it"

async def test_a_failed_partial_borrow_leaves_the_deck_whole(test_db, monkeypatch):
    """The ratchet: every failure used to cost cards that never moved."""
    client = FakeLendingServe({"Swamp": 6}, job={"state": "failed", "detail": "nope"})
    monkeypatch.setattr(svc, "get_lending_client", lambda: client)
    loan_id = await _seed(state="out_pending", job_id="job-1",
                          pending=[{"name": "Swamp", "qty": 6}])

    await svc.settle_in_flight()

    loan = await _loan(loan_id)
    assert loan.cards == FULL, "a failed handover must not shrink the deck"
    assert loan.pending_cards is None
    assert loan.state == "assigned"

async def test_a_partial_borrow_owes_what_landed_and_keeps_the_deck_whole(test_db, monkeypatch):
    """The claim is what actually crossed; the deck stays what was drafted.

    Those are two different facts and they used to be one field. Rewriting the
    deck down to the delivered part loses the only record of what is still
    missing, so `/borrow` could never work out a remainder to go back for --
    and a deck that arrives in five trades, three of which land, is the normal
    shape here rather than an edge case.
    """
    from services import debt_service
    from services import wallet_service

    partial = [{"name": "Swamp", "qty": 6}]
    client = FakeLendingServe({"Swamp": 6}, job={"state": "done", "give": partial})
    monkeypatch.setattr(svc, "get_lending_client", lambda: client)
    loan_id = await _seed(state="out_pending", job_id="job-1", pending=partial)

    await svc.settle_in_flight()

    loan = await _loan(loan_id)
    assert loan.cards == FULL, "the drafted deck is still what they were owed"
    assert loan.pending_cards is None
    assert loan.state == "borrowed"

    owed = await debt_service.get_open_card_positions(
        GUILD, BORROWER, wallet_service.HOUSE_MTGO)
    assert [(p["card_name"], p["net"]) for p in owed] == [("Swamp", -6)], \
        "they owe the 6 that arrived, not the 8 the deck asks for"

async def test_the_trade_offers_what_was_agreed_not_the_whole_deck(test_db, monkeypatch):
    partial = [{"name": "Swamp", "qty": 6}]
    client = FakeLendingServe({"Swamp": 6})
    monkeypatch.setattr(svc, "get_lending_client", lambda: client)

    async def handle(_):
        return "Borrower01"
    monkeypatch.setattr(svc, "_mtgo_handle", handle)
    monkeypatch.setattr(svc, "card_library_collateral", lambda gid: 0)
    loan_id = await _seed()

    await svc.start_borrow(GUILD, BORROWER, partial)

    assert client.sent == [partial], "the agreed subset goes out, not the full deck"
    assert (await _loan(loan_id)).pending_cards == partial, \
        "and the dispatch records what it sent"

async def test_a_lost_response_keeps_the_offer(test_db, monkeypatch):
    """The mirror case. If the request may have reached MTGO, the trade that
    may be open is for the TRIMMED deck, and dropping the offer would settle it
    as though the whole deck had been handed over."""
    client = FakeLendingServe({"Swamp": 6})

    async def ambiguous(user, cards, **kw):
        return {"_ambiguous": True}
    client.borrow = ambiguous
    monkeypatch.setattr(svc, "get_lending_client", lambda: client)
    monkeypatch.setattr(svc, "card_library_collateral", lambda gid: 0)

    async def handle(_):
        return "Borrower01"
    monkeypatch.setattr(svc, "_mtgo_handle", handle)

    loan_id = await _seed(pending=[{"name": "Swamp", "qty": 6}])

    assert (await svc.start_borrow(GUILD, BORROWER))[0] == "dispatch_unknown"
    assert (await _loan(loan_id)).pending_cards == [{"name": "Swamp", "qty": 6}]

async def test_giving_up_on_the_queue_leaves_nothing_staged(test_db, monkeypatch):
    """The offer used to be written before the borrower even reached the serve,
    so a five-minute queue timeout left it on the loan -- and the next borrow
    read it in preference to the deck, handing over the smaller deck long after
    the library had restocked. Nothing is written now until a trade carries it.
    """
    monkeypatch.setattr(svc, "get_lending_client", lambda: FakeLendingServe({"Swamp": 2}))

    async def always_busy():
        return "trading with someone else"
    monkeypatch.setattr(svc, "library_busy_reason", always_busy)
    loan_id = await _seed()

    trimmed = await svc.trim_to_available(GUILD, loan_id)
    status, _ = await svc.borrow_when_free(GUILD, BORROWER, poll_s=0, timeout_s=0,
                                           offering=trimmed)

    assert status == "still_busy"
    assert (await _loan(loan_id)).pending_cards is None, "nothing may be left behind"

async def test_a_second_click_cannot_change_a_trade_in_flight(test_db, monkeypatch):
    """The button outlives the borrow it starts, so a second click can land
    while the first trade is open. It must be refused, not applied -- settlement
    adopts the offer as the deck, so changing it would ask the borrower to
    return cards they were never sent."""
    client = FakeLendingServe({"Swamp": 6})
    monkeypatch.setattr(svc, "get_lending_client", lambda: client)

    async def handle(_):
        return "Borrower01"
    monkeypatch.setattr(svc, "_mtgo_handle", handle)
    monkeypatch.setattr(svc, "card_library_collateral", lambda gid: 0)
    sent = [{"name": "Swamp", "qty": 6}]
    loan_id = await _seed()
    await svc.start_borrow(GUILD, BORROWER, sent)

    status, _ = await svc.start_borrow(GUILD, BORROWER, [{"name": "Swamp", "qty": 1}])

    assert status == "already_in_flight"
    assert client.sent == [sent], "the second click opened no trade"
    assert (await _loan(loan_id)).pending_cards == sent, "and changed no obligation"
