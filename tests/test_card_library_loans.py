"""Borrowing and returning a deck from the card library: when the claim moves.

Named for the library rather than "the lending service" on purpose: the
unrelated tests/test_card_lending_service.py covers the multi-entity card DEBT
ledger from an earlier design, and a file that greps the same way is how the
wrong one gets read.

The library's cards physically live in another MTGO account, so every state
change here is half of a distributed operation -- DraftBot holds the pending
state, the TradeBot serve runs the trade, and the job's terminal state settles
it. The rule that keeps the two honest is that the claim moves ONLY when a job
finishes. Marking someone as holding a deck the moment we ASK for the trade
would leave the ledger asserting a handover that MTGO may never have made.
"""
import pytest

from database.db_session import AsyncSessionLocal
from models.card_loan import CardLoan
import services.card_lending_service as svc
from conftest import FakeLendingServe, stub_library

pytestmark = pytest.mark.asyncio

GUILD, BORROWER, HANDLE = "g1", "u1", "Borrower01"
DECK = [{"name": "Swamp", "qty": 7}, {"name": "Ghostly Wings", "qty": 1}]
# What the library can hand over. The dispatch checks the shelf before it
# opens a trade, and these tests never write custody rows, so without this
# every one of them refuses with "short_cards".
_STOCK = {c["name"]: 99 for c in DECK}


@pytest.fixture
def client(monkeypatch):
    c = FakeLendingServe()
    monkeypatch.setattr(svc, "get_lending_client", lambda: c)
    monkeypatch.setattr(svc, "_mtgo_handle", _fake_handle(HANDLE))
    # A library that lends for free. Absent config means the feature is OFF, so
    # the guild has to declare a figure before anything here can dispatch;
    # collateral itself is tested in test_card_loan_collateral.py.
    stub_library(monkeypatch, svc, collateral=0, stock=_STOCK)
    # A cube that charges needs the wallet enabled; the refusal for one
    # that does not has its own test.
    monkeypatch.setattr(svc, "is_money_server", lambda gid: True)
    return c


def _fake_handle(handle):
    async def _h(discord_user_id):
        return handle
    return _h


async def _seed(state="assigned", job_id=None):
    async with AsyncSessionLocal() as s:
        loan = CardLoan(guild_id=GUILD, library_id="lib", borrower_id=BORROWER, cards=DECK,
                        state=state, job_id=job_id, source="fixture:test")
        s.add(loan)
        await s.commit()
        return loan.id


async def _state(loan_id):
    async with AsyncSessionLocal() as s:
        loan = await s.get(CardLoan, loan_id)
        return loan.state, loan.job_id, loan.borrowed_at, loan.returned_at


# --- borrowing --------------------------------------------------------------

async def test_borrowing_offers_the_whole_deck_and_waits(test_db, client):
    loan_id = await _seed()

    status, _ = await svc.start_borrow(GUILD, BORROWER)

    assert status == "dispatched"
    assert client.lent == [(HANDLE, DECK)], "the deck goes out in one trade"
    state, job_id, borrowed_at, _ = await _state(loan_id)
    assert state == "out_pending", "not 'borrowed' -- MTGO has not moved anything yet"
    assert job_id == "job-1", "the job must be recorded, or a restart strands the loan"
    assert borrowed_at is None


async def test_asking_twice_does_not_offer_the_deck_twice(test_db, client):
    """A double click, or a player who thinks it did not work."""
    await _seed(state="out_pending", job_id="job-1")

    status, _ = await svc.start_borrow(GUILD, BORROWER)

    assert status == "already_in_flight"
    assert client.lent == [], "a second trade would hand out a second copy"


async def test_you_cannot_borrow_what_you_already_hold(test_db, client):
    await _seed(state="borrowed")

    status, _ = await svc.start_borrow(GUILD, BORROWER)

    assert status == "already_borrowed"
    assert client.lent == []


async def test_borrowing_without_a_deck_assigned_says_so(test_db, client):
    status, _ = await svc.start_borrow(GUILD, BORROWER)

    assert status == "no_loan"
    assert client.lent == []


async def test_an_unlinked_borrower_is_refused_before_any_trade(test_db, client, monkeypatch):
    """The serve trades with an MTGO handle; without one there is nobody to
    trade with, and dispatching would burn a job that can never complete."""
    monkeypatch.setattr(svc, "_mtgo_handle", _fake_handle(None))
    await _seed()

    status, _ = await svc.start_borrow(GUILD, BORROWER)

    assert status == "not_linked"
    assert client.lent == []


async def test_a_disabled_library_refuses_rather_than_pretending(test_db, client):
    client.enabled = False
    loan_id = await _seed()

    status, _ = await svc.start_borrow(GUILD, BORROWER)

    assert status == "unavailable"
    assert (await _state(loan_id))[0] == "assigned", "state must not move"


# --- settling the trade -----------------------------------------------------

async def test_the_deck_is_only_borrowed_once_the_trade_completes(test_db, client):
    loan_id = await _seed(state="out_pending", job_id="job-1")
    client.jobs["job-1"] = {"state": "done", "give": DECK}

    await svc.settle_in_flight()

    state, job_id, borrowed_at, _ = await _state(loan_id)
    assert state == "borrowed"
    assert borrowed_at is not None
    assert job_id is None, "the job is finished; leaving it set would re-settle it"


async def test_a_failed_handover_puts_the_deck_back_on_the_shelf(test_db, client):
    """MTGO refused or the borrower never showed. Nothing moved, so the loan
    returns to where it was and can be tried again."""
    loan_id = await _seed(state="out_pending", job_id="job-1")
    client.jobs["job-1"] = {"state": "failed", "detail": "cancelled by operator"}

    await svc.settle_in_flight()

    state, job_id, borrowed_at, _ = await _state(loan_id)
    assert (state, job_id, borrowed_at) == ("assigned", None, None)


async def test_a_running_job_settles_nothing(test_db, client):
    loan_id = await _seed(state="out_pending", job_id="job-1")
    client.jobs["job-1"] = {"state": "running"}

    await svc.settle_in_flight()

    assert (await _state(loan_id))[0] == "out_pending"


# --- returning --------------------------------------------------------------

async def test_returning_names_no_cards_and_lets_the_serve_pin_them(test_db, client):
    """A whole-loan return sends no card list. The serve recorded which
    printings it lent and settles everything open for this borrower, so naming
    them again could only disagree with what actually crossed -- a borrow that
    half-landed would otherwise ask for cards they never received."""
    loan_id = await _seed(state="borrowed")

    status, _ = await svc.start_return(GUILD, BORROWER)

    assert status == "dispatched"
    assert client.collected == [(HANDLE, None)], "no card list goes out"
    assert (await _state(loan_id))[0] == "return_pending"


async def test_you_cannot_return_a_deck_you_never_took(test_db, client):
    await _seed(state="assigned")

    status, _ = await svc.start_return(GUILD, BORROWER)

    assert status == "not_borrowed"
    assert client.collected == []


async def test_the_loan_closes_only_when_the_cards_are_back(test_db, client):
    loan_id = await _seed(state="return_pending", job_id="job-1")
    client.jobs["job-1"] = {"state": "done", "receive": DECK}

    await svc.settle_in_flight()

    state, job_id, _, returned_at = await _state(loan_id)
    assert state == "returned"
    assert returned_at is not None
    assert job_id is None


async def test_a_failed_return_leaves_the_borrower_still_holding_it(test_db, client):
    """The cards did not come back, so the debt does not disappear."""
    loan_id = await _seed(state="return_pending", job_id="job-1")
    client.jobs["job-1"] = {"state": "failed"}

    await svc.settle_in_flight()

    state, job_id, _, returned_at = await _state(loan_id)
    assert (state, job_id, returned_at) == ("borrowed", None, None)


async def test_a_closed_loan_frees_the_borrower_to_take_another(test_db, client):
    """The database enforces one active loan; this is the path that releases it."""
    loan_id = await _seed(state="return_pending", job_id="job-1")
    client.jobs["job-1"] = {"state": "done", "receive": DECK}
    await svc.settle_in_flight()

    new_id = await svc.assign_deck(GUILD, BORROWER, DECK, source="fixture:second")

    assert new_id is not None and new_id != loan_id
