"""Exercise the real ledger/dispatch boundary with two shelves in one vault."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from conftest import a_library, FakeLendingServe
from database.db_session import AsyncSessionLocal, db_session
from models.card_loan import CardLoan
from models.debt_ledger import DebtLedger
from models.library import Library
from models.library_member import LibraryMember
from models.library_server import LibraryServer
from models.mtgo_job import MtgoJob
from services import card_lending_service as lending, card_deposit_service as deposits
from services import debt_service, wallet_service
from services.library_access_service import may_borrow

pytestmark = pytest.mark.asyncio
CARD = [{"name": "Swamp", "qty": 1}]


@pytest_asyncio.fixture
async def shelves(test_db, monkeypatch):
    await a_library("a", guild="ga")
    await a_library("b", guild="gb")
    serve = FakeLendingServe({"Swamp": 20}, job={"state": "done"})
    for module in (lending, deposits):
        monkeypatch.setattr(module, "get_lending_client", lambda: serve)
        monkeypatch.setattr(module, "_mtgo_handle", AsyncMock(return_value="Player"))
    monkeypatch.setattr(lending, "on_inflow", AsyncMock())
    monkeypatch.setattr(lending, "is_money_server", lambda _: True)
    return serve


async def donate(library, qty=1, owner="donor", name="Swamp"):
    await debt_service.create_card_loan(
        guild_id=wallet_service.library_scope(library), lender_id=owner,
        borrower_id=wallet_service.HOUSE_LIBRARY, card_name=name,
        quantity=qty, created_by="test")


async def assigned(guild="ga", borrower="player", cards=None):
    return await lending.assign_deck(guild, borrower, CARD if cards is None else cards)


async def rebind(guild="ga", library="b"):
    async with AsyncSessionLocal() as s:
        (await s.get(LibraryServer, guild)).library_id = library
        await s.commit()


async def test_missing_entitlement_is_short_even_when_vault_has_it(shelves):
    await donate("b")
    loan = await assigned()
    assert await lending.shortfall("ga", CARD, loan) == [{"name": "Swamp", "want": 1, "have": 0}]
    assert await lending.trim_to_available("ga", loan) == []
    assert (await lending.start_borrow("ga", "player"))[0] == "short_cards"
    assert not shelves.lent


async def test_collecting_own_reservation_and_hidden_vault_card(shelves):
    await donate("a")
    shelves.stock = {}  # Only the physical listing is truncated.
    loan = await assigned()
    assert await lending.shortfall("ga", CARD, loan) == []
    assert await lending.trim_to_available("ga", loan) == CARD
    assert (await lending.start_borrow("ga", "player"))[0] == "dispatched"
    async with AsyncSessionLocal() as s:
        assert (await s.get(MtgoJob, "job-1")).library_id == "a"
        assert (await s.get(CardLoan, loan)).library_id == "a"


async def test_other_reservation_cannot_be_readded_after_clamping(shelves):
    await donate("a")
    await assigned(borrower="other")
    loan = await assigned()
    assert await lending.trim_to_available("ga", loan) == []
    assert (await lending.start_borrow("ga", "player"))[0] == "short_cards"


async def test_partial_offer_rechecked_after_waiting(shelves):
    await donate("a")
    loan = await assigned()
    partial = await lending.trim_to_available("ga", loan)
    await assigned(borrower="other")
    assert (await lending.borrow_when_free("ga", "player", offering=partial))[0] == "short_cards"
    assert not shelves.lent


@pytest.mark.parametrize("offer", [[], CARD * 2, [{"name": "Black Lotus", "qty": 1}]])
async def test_offer_must_be_a_nonempty_subset_of_the_assignment(shelves, offer):
    await donate("a", 10)
    await donate("a", name="Black Lotus")
    await assigned()
    assert (await lending.start_borrow("ga", "player", offer))[0] == "short_cards"
    assert not shelves.lent


async def test_withdrawal_cannot_use_other_library_to_cover_a_loan(shelves):
    await donate("a")
    await donate("b")
    await assigned()
    assert (await deposits.start_withdrawal("ga", "donor"))[0] == "some_on_loan"
    assert not shelves.withdrawn


async def test_withdrawal_names_only_this_librarys_cards(shelves):
    await donate("a")
    await donate("b", 5, name="Black Lotus")
    assert await deposits.withdrawal_orders("donor", "a") == [CARD]
    assert (await deposits.start_withdrawal("ga", "donor"))[0] == "dispatched"
    assert shelves.withdrawn == [("Player", CARD)]
    await rebind()
    await deposits.settle_deposits()
    assert await deposits.held_for("donor", "a") == []
    assert await deposits.held_for("donor", "b") == [{"name": "Black Lotus", "qty": 5}]


@pytest.mark.parametrize("cards", [CARD * 2, [{"name": "Black Lotus", "qty": 1}]])
async def test_explicit_withdrawal_cannot_exceed_personal_custody(shelves, cards):
    await donate("a")
    await donate("a", name="Black Lotus", owner="another")
    assert (await deposits.start_withdrawal("ga", "donor", cards))[0] == "nothing_held"
    assert not shelves.withdrawn


async def test_deposit_settles_into_dispatch_library_after_rebind(shelves):
    assert (await deposits.start_deposit("ga", "donor", CARD))[0] == "dispatched"
    await rebind()
    await deposits.settle_deposits()
    assert await deposits.held_for("donor", "a") == CARD
    assert await deposits.held_for("donor", "b") == []


async def test_borrow_and_return_claims_stay_on_original_library(shelves):
    await donate("a")
    loan_id = await assigned()
    assert (await lending.start_borrow("ga", "player"))[0] == "dispatched"
    await rebind()
    await lending.settle_in_flight()
    async with AsyncSessionLocal() as s:
        claims = list((await s.scalars(select(DebtLedger).where(DebtLedger.created_by == "card-library"))).all())
    assert {r.guild_id for r in claims} == {"library:a"}
    shelves.job_id = "return-1"
    assert (await lending.start_return("gb", "player"))[0] == "dispatched"
    async with AsyncSessionLocal() as s:
        job = await s.get(MtgoJob, "return-1")
        assert (job.guild_id, job.library_id) == ("ga", "a")
    await lending.settle_in_flight()
    async with AsyncSessionLocal() as s:
        assert (await s.get(CardLoan, loan_id)).state == "returned"
    assert await lending._still_owed("library:a", "player") == []


@pytest.mark.parametrize("kind", ["card-deposit", "borrow"])
async def test_null_job_stamp_stays_unsettled_without_writing_claims(shelves, kind):
    async with AsyncSessionLocal() as s:
        s.add(MtgoJob(job_id="null-job", kind=kind, guild_id="ga", player_id="player",
                      mtgo_user="Player", amount=1, status="pending"))
        if kind == "borrow":
            s.add(CardLoan(guild_id="ga", library_id="a", borrower_id="player", cards=CARD,
                           state="out_pending", job_id="null-job"))
        await s.commit()
    shelves.jobs["null-job"] = {"state": "done", "give": CARD, "receive": CARD}

    # The scan SURVIVES it rather than propagating. Booking a claim refuses a
    # job that names no library -- there is nowhere correct to put the cards --
    # but letting that escape the loop would leave every later job unsettled on
    # this pass and on every pass after it, which is a wedge visible only as a
    # traceback. The row stays pending for a human instead.
    await (deposits.settle_deposits() if kind == "card-deposit"
           else lending.settle_in_flight())

    async with AsyncSessionLocal() as s:
        assert (await s.get(MtgoJob, "null-job")).status == "pending"
        assert not list((await s.scalars(select(DebtLedger))).all())


async def test_unbinding_stops_stale_partial_view_and_pricing(shelves):
    await donate("a")
    loan_id = await assigned()
    async with AsyncSessionLocal() as s:
        loan = await s.get(CardLoan, loan_id)
        await s.execute(delete(LibraryServer).where(LibraryServer.guild_id == "ga"))
        await s.commit()
    assert await lending.collateral_for(loan, "ga") is None
    assert await lending.trim_to_available("ga", loan_id) == []
    assert (await lending.start_borrow("ga", "player", CARD))[0] == "unavailable"
    assert (await deposits.start_deposit("ga", "donor", CARD))[0] == "unavailable"
    assert (await deposits.start_withdrawal("ga", "donor"))[0] == "unavailable"
    assert not await may_borrow(None, "player")


async def test_deleted_stamped_library_never_reprices_to_current_binding(shelves):
    loan_id = await assigned()
    await rebind()
    async with AsyncSessionLocal() as s:
        loan = await s.get(CardLoan, loan_id)
        await s.execute(delete(Library).where(Library.id == "a"))
        await s.commit()
    assert await lending.collateral_for(loan, "ga") is None


async def test_original_library_invites_are_checked_after_rebind(shelves):
    await donate("a")
    await assigned()
    await rebind()
    async with AsyncSessionLocal() as s:
        s.add(LibraryMember(library_id="a", player_id="someone-else"))
        await s.commit()
    assert (await lending.start_borrow("ga", "player"))[0] == "not_invited"
    assert not shelves.lent


async def test_refused_return_preserves_original_guild_collateral(shelves):
    await donate("a")
    loan = await assigned()
    async with db_session() as s:
        await wallet_service.transfer_in(s, "ga", "system:test", "player", 10, "fund")
    await lending.set_collateral("ga", "player", loan, 5)
    async with AsyncSessionLocal() as s:
        (await s.get(CardLoan, loan)).state = "borrowed"
        await s.commit()
    shelves.response = None
    assert (await lending.start_return("gb", "player"))[0] == "dispatch_failed"
    async with db_session() as s:
        assert await wallet_service.balance_in(s, "ga", lending.collateral_holder("ga")) == 5
        assert await wallet_service.balance_in(s, "gb", "player") == 0
