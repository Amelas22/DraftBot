"""What the library can actually lend right now, and what to do when it is short.

The vault says what exists; live loans say what is already out. The difference
is what a new borrower can be given. Without this check a deck the library
cannot cover is dispatched anyway, and the shortfall surfaces minutes later as
"partner's binder does not hold enough" -- after the player has been told to go
and accept a trade.

When the library is short the borrower is told exactly how short, and may take
what there is. Taking a partial deck REWRITES the loan to what was actually
lent: the return reads the same list, so borrowing 3 Swamps and being asked for
4 back is not a state this can reach.
"""
import pytest
import pytest_asyncio

from database.db_session import AsyncSessionLocal
from models.card_loan import CardLoan
import services.card_lending_service as svc
from conftest import FakeLendingServe

pytestmark = pytest.mark.asyncio

GUILD, LIB = "g1", "lib"


async def _loan(borrower, state, cards):
    async with AsyncSessionLocal() as s:
        loan = CardLoan(guild_id=GUILD, borrower_id=borrower, state=state,
                        library_id=LIB, cards=cards, source="fixture")
        s.add(loan)
        await s.commit()
        return loan.id


@pytest_asyncio.fixture(autouse=True)
async def _stocked(test_db):
    """A library that owns plenty, bound to this server.

    What can be lent is the lesser of what the shelf physically holds and what
    the ledger says this library is owed -- several libraries share one MTGO
    account, so the vault alone cannot say whose a card is. These tests are
    about the VAULT side of that, so the ledger is stocked past anything they
    ask for and the vault stays the binding constraint.
    """
    from conftest import a_library
    from services import debt_service, wallet_service
    await a_library(LIB, guild=GUILD)
    for name in ("Swamp", "Island", "Mountain", "Forest", "Plains",
                 "Black Lotus", "Cathar's Companion", "Ghostly Wings",
                 # Entitled in the ledger and deliberately absent from every
                 # vault listing below: that pair is what the two absences
                 # test asks about.
                 "Some Hidden Card"):
        await debt_service.create_card_loan(
            guild_id=wallet_service.library_scope(LIB), lender_id="donor",
            borrower_id=wallet_service.HOUSE_LIBRARY, card_name=name,
            quantity=99, created_by="test", source_id=f"seed-{name}")


async def test_a_collected_deck_is_not_subtracted_twice(test_db, monkeypatch):
    """Measured against the live serve on 2026-09-16: a deck out on loan drops
    out of /vault entirely (24 Swamp -> 0 while 24 were lent). Cards the
    borrower is holding are already gone from the figure, so subtracting them
    again would refuse borrows the library is physically able to make."""
    monkeypatch.setattr(svc, "get_lending_client", lambda: FakeLendingServe({"Swamp": 8}))
    await _loan("u1", "borrowed", [{"name": "Swamp", "qty": 10}])
    await _loan("u2", "return_pending", [{"name": "Swamp", "qty": 4}])

    assert (await svc.available_now())["Swamp"] == 8


async def test_a_dispatched_but_uncollected_deck_is_still_subtracted(test_db, monkeypatch):
    """An offered trade has not moved anything yet, so those cards are still in
    the vault -- but they are promised. Offering them to a second borrower would
    mean two people accepting trades for one stack."""
    monkeypatch.setattr(svc, "get_lending_client", lambda: FakeLendingServe({"Swamp": 24}))
    await _loan("u1", "out_pending", [{"name": "Swamp", "qty": 6}])

    assert (await svc.available_now())["Swamp"] == 18


async def test_what_is_subtracted_is_the_offer_not_the_original_deck(test_db, monkeypatch):
    """A borrower who took a partial deck has only that much promised to them."""
    monkeypatch.setattr(svc, "get_lending_client", lambda: FakeLendingServe({"Swamp": 24}))
    loan_id = await _loan("u1", "out_pending", [{"name": "Swamp", "qty": 20}])
    async with AsyncSessionLocal() as s:
        loan = await s.get(CardLoan, loan_id)
        loan.pending_cards = [{"name": "Swamp", "qty": 4}]
        await s.commit()

    assert (await svc.available_now())["Swamp"] == 20, "20 free, not 4"


async def test_an_assigned_deck_has_not_taken_anything_yet(test_db, monkeypatch):
    """Assigning a deck reserves nothing physical -- the cards only leave on a
    borrow, and the vault reflects that by itself."""
    monkeypatch.setattr(svc, "get_lending_client", lambda: FakeLendingServe({"Swamp": 24}))
    await _loan("u1", "assigned", [{"name": "Swamp", "qty": 10}])

    assert (await svc.available_now())["Swamp"] == 24


async def test_a_deck_the_library_can_cover_reports_no_shortfall(test_db, monkeypatch):
    monkeypatch.setattr(svc, "get_lending_client", lambda: FakeLendingServe({"Swamp": 24}))
    short = await svc.shortfall(GUILD, [{"name": "Swamp", "qty": 4}])
    assert short == []


async def test_a_short_library_reports_how_short_per_card(test_db, monkeypatch):
    # 22 Swamps are already out, so the vault itself reports the 2 that remain.
    monkeypatch.setattr(svc, "get_lending_client",
                        lambda: FakeLendingServe({"Swamp": 2, "Ghostly Wings": 1}))
    await _loan("u1", "borrowed", [{"name": "Swamp", "qty": 22}])

    short = await svc.shortfall(GUILD, [{"name": "Swamp", "qty": 10},
                                        {"name": "Ghostly Wings", "qty": 1}])

    assert short == [{"name": "Swamp", "want": 10, "have": 2}], \
        "only the card that is short, with what there is"


async def test_a_card_the_vault_never_lists_is_not_assumed_missing(test_db, monkeypatch):
    """The two absences read opposite ways round, and this is the vault one.

    /vault truncates its listing, so a card missing from it is unknown rather
    than gone -- refusing on unknown would block legitimate borrows, and the
    trade itself will say so if we are wrong. A card missing from the LEDGER
    is the other case entirely: nobody gave this library that card, and lending
    it would be spending another library's stock. So this card is entitled in
    the ledger (see the fixture) and simply not listed by the vault."""
    monkeypatch.setattr(svc, "get_lending_client", lambda: FakeLendingServe({"Swamp": 24}))
    short = await svc.shortfall(GUILD, [{"name": "Some Hidden Card", "qty": 1}])
    assert short == []


async def test_taking_what_there_is_returns_the_offer(test_db, monkeypatch):
    """Worked out and handed back, not written down: a handover that fails moved
    nothing and must cost nothing (see test_partial_no_ratchet). The dispatch
    records what it actually sent, and that becomes the deck on success, so the
    return asks for what was lent."""
    monkeypatch.setattr(svc, "get_lending_client", lambda: FakeLendingServe({"Swamp": 2}))
    await _loan("u1", "borrowed", [{"name": "Swamp", "qty": 22}])
    loan_id = await _loan("u2", "assigned", [{"name": "Swamp", "qty": 10}])

    assert await svc.trim_to_available(GUILD, loan_id) == [{"name": "Swamp", "qty": 2}]

    async with AsyncSessionLocal() as s:
        loan = await s.get(CardLoan, loan_id)
    assert loan.pending_cards is None, "nothing is staged ahead of the trade"
    assert loan.cards == [{"name": "Swamp", "qty": 10}], "the deck itself is untouched"


async def test_a_card_with_none_left_is_dropped_from_the_deck(test_db, monkeypatch):
    # The single is already out, so the vault no longer lists it at all.
    monkeypatch.setattr(svc, "get_lending_client",
                        lambda: FakeLendingServe({"Swamp": 4, "Ghostly Wings": 0}))
    await _loan("u1", "borrowed", [{"name": "Ghostly Wings", "qty": 1}])
    loan_id = await _loan("u2", "assigned", [{"name": "Ghostly Wings", "qty": 1},
                                             {"name": "Swamp", "qty": 2}])

    trimmed = await svc.trim_to_available(GUILD, loan_id)

    assert trimmed == [{"name": "Swamp", "qty": 2}], \
        "the single is gone entirely, so it is dropped rather than offered as zero"
