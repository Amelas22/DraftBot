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
import pytest_asyncio

from database.db_session import AsyncSessionLocal
from models.card_loan import CardLoan
import services.card_lending_service as svc
from conftest import FakeLendingServe, stub_library

pytestmark = pytest.mark.asyncio

GUILD, BORROWER, LIB = "g1", "u1", "lib"
FULL = [{"name": "Cathar's Companion", "qty": 2}, {"name": "Swamp", "qty": 8}]


@pytest_asyncio.fixture(autouse=True)
async def _stocked(test_db):
    """A library that owns plenty of both cards, bound to this server.

    What can be lent is the lesser of what the shelf holds and what the ledger
    says this library is owed. These tests are about the SHELF running short,
    so the ledger is stocked past anything they ask for.
    """
    from conftest import a_library
    from services import debt_service, wallet_service
    await a_library(LIB, guild=GUILD)
    for name in ("Cathar's Companion", "Swamp"):
        await debt_service.create_card_loan(
            guild_id=wallet_service.library_scope(LIB), lender_id="donor",
            borrower_id=wallet_service.HOUSE_LIBRARY, card_name=name,
            quantity=99, created_by="test", source_id=f"seed-{name}")


async def _seed(state="assigned", cards=None, pending=None, job_id=None):
    async with AsyncSessionLocal() as s:
        loan = CardLoan(guild_id=GUILD, borrower_id=BORROWER, state=state,
                        library_id=LIB, cards=cards or FULL,
                        pending_cards=pending, job_id=job_id, source="fixture")
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

    # Booked under the LIBRARY, not the guild: several libraries share one
    # MTGO account, so a claim filed against the server could not say whose
    # cards a borrower is holding.
    owed = await debt_service.get_open_card_positions(
        wallet_service.library_scope(LIB), BORROWER, wallet_service.HOUSE_MTGO)
    assert [(p["card_name"], p["net"]) for p in owed] == [("Swamp", -6)], \
        "they owe the 6 that arrived, not the 8 the deck asks for"

async def test_the_trade_offers_what_was_agreed_not_the_whole_deck(test_db, monkeypatch):
    partial = [{"name": "Swamp", "qty": 6}]
    client = FakeLendingServe({"Swamp": 6})
    monkeypatch.setattr(svc, "get_lending_client", lambda: client)

    async def handle(_):
        return "Borrower01"
    monkeypatch.setattr(svc, "_mtgo_handle", handle)
    stub_library(monkeypatch, svc, collateral=0)
    # A cube that charges needs the wallet enabled; the refusal for one
    # that does not has its own test.
    monkeypatch.setattr(svc, "is_money_server", lambda gid: True)
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
    stub_library(monkeypatch, svc, collateral=0)
    # A cube that charges needs the wallet enabled; the refusal for one
    # that does not has its own test.
    monkeypatch.setattr(svc, "is_money_server", lambda gid: True)

    async def handle(_):
        return "Borrower01"
    monkeypatch.setattr(svc, "_mtgo_handle", handle)

    offer = [{"name": "Swamp", "qty": 6}]
    loan_id = await _seed(pending=offer)

    # The agreed subset is what goes out and what may now be open -- the
    # dispatch checks the shelf against what it is actually sending, and the
    # shelf cannot cover the whole deck.
    assert (await svc.start_borrow(GUILD, BORROWER, offer))[0] == "dispatch_unknown"
    assert (await _loan(loan_id)).pending_cards == offer

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
    stub_library(monkeypatch, svc, collateral=0)
    # A cube that charges needs the wallet enabled; the refusal for one
    # that does not has its own test.
    monkeypatch.setattr(svc, "is_money_server", lambda gid: True)
    sent = [{"name": "Swamp", "qty": 6}]
    loan_id = await _seed()
    await svc.start_borrow(GUILD, BORROWER, sent)

    status, _ = await svc.start_borrow(GUILD, BORROWER, [{"name": "Swamp", "qty": 1}])

    assert status == "already_in_flight"
    assert client.sent == [sent], "the second click opened no trade"
    assert (await _loan(loan_id)).pending_cards == sent, "and changed no obligation"
