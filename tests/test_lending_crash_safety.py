"""What must survive a crash, a lost response, or a scan that arrives too late.

Every lending trade is half here and half in the serve, and the two halves are
joined by exactly two things: the loan's `job_id` and the wallet ledger's
idempotency keys. Each test below is one way those joins can be broken --
losing a player's collateral, or applying one trade's outcome to another.

The rule the fixes share: **do the irreversible half first, and make it
idempotent, so the recoverable half can be retried.** Money moves before
`job_id` is cleared, because clearing `job_id` is what hides the loan from the
next scan -- and because the deposit is expressed as a TARGET holding rather
than a hold (see test_collateral_target_hold.py), settling the same loan twice
converges instead of paying twice.
"""
from unittest.mock import AsyncMock

import pytest

from database.db_session import AsyncSessionLocal, db_session
from models.card_loan import CardLoan
from services import wallet_service
import services.card_lending_service as svc
from conftest import FakeLendingServe, stub_library

pytestmark = pytest.mark.asyncio

GUILD, BORROWER, HANDLE = "g1", "u1", "Borrower01"
DECK = [{"name": "Swamp", "qty": 4}]
# What the library can hand over. The dispatch checks the shelf before it
# opens a trade, and these tests never write custody rows, so without this
# every one of them refuses with "short_cards".
_STOCK = {c["name"]: 99 for c in DECK}


@pytest.fixture
def rig(monkeypatch):
    client = FakeLendingServe()
    monkeypatch.setattr(svc, "get_lending_client", lambda: client)

    async def handle(_):
        return HANDLE
    monkeypatch.setattr(svc, "_mtgo_handle", handle)
    stub_library(monkeypatch, svc, collateral=5, stock=_STOCK)
    # A cube that charges needs the wallet enabled; the refusal for one
    # that does not has its own test.
    monkeypatch.setattr(svc, "is_money_server", lambda gid: True)

    async def no_debt(*a, **k):
        return []
    monkeypatch.setattr(svc, "on_inflow", no_debt)
    return client


async def _fund(n=5):
    async with db_session() as s:
        await wallet_service.transfer_in(s, GUILD, "system:test-seed",
                                         BORROWER, n, "seed:test", notes="opening")


async def _balances():
    async with db_session() as s:
        return (await wallet_service.balance_in(s, GUILD, BORROWER),
                await wallet_service.balance_in(s, GUILD, svc.collateral_holder(GUILD)))


async def _seed(state="assigned", job_id=None):
    async with AsyncSessionLocal() as s:
        loan = CardLoan(guild_id=GUILD, library_id="lib", borrower_id=BORROWER, cards=DECK, state=state,
                        job_id=job_id, source="fixture")
        s.add(loan)
        await s.commit()
        return loan.id


async def _row(loan_id):
    async with AsyncSessionLocal() as s:
        loan = await s.get(CardLoan, loan_id)
        return loan.state, loan.job_id


# --- 1. a trade the serve definitely refused must not keep the deposit -------

async def test_a_refused_trade_gives_the_deposit_straight_back(test_db, rig):
    """The collateral is taken before the cards move, so a serve that refuses
    the job leaves tix sitting in the holder against a loan that never left
    'assigned'. Nothing later looks at that loan again -- it has no job to
    settle -- so the money would stay there until a human noticed."""
    rig.response = None      # a definite refusal: no job was created
    await _fund()
    loan_id = await _seed()

    status, _ = await svc.start_borrow(GUILD, BORROWER)

    assert status == "dispatch_failed"
    borrower, holder = await _balances()
    assert (borrower, holder) == (5, 0), "the deposit must come back at once"


async def test_a_lost_response_keeps_the_deposit_held(test_db, rig):
    """Ambiguous is not refused. The POST may have reached the serve and opened
    a real trade; refunding here and letting them retry would hand out a second
    deck against no deposit. The hold stands until the serve says otherwise."""
    rig.response = {"_ambiguous": True}
    await _fund()
    loan_id = await _seed()

    status, _ = await svc.start_borrow(GUILD, BORROWER)

    assert status == "dispatch_unknown"
    borrower, holder = await _balances()
    assert (borrower, holder) == (0, 5), "the deposit stays put"


# --- 2. money moves before the job id is cleared ----------------------------

async def test_a_crash_paying_out_leaves_the_loan_settleable(test_db, rig, monkeypatch):
    """Clearing job_id is what hides a loan from the next scan. Doing that
    before the refund means a crash in between loses the player's deposit AND
    the only record of which trade it belonged to."""
    await _fund()
    loan_id = await _seed("out_pending", job_id="job-1")
    async with db_session() as s:
        await wallet_service.transfer_in(s, GUILD, BORROWER, svc.collateral_holder(GUILD),
                                         5, f"loan:{loan_id}:{BORROWER}:0:0-5",
                                         notes="deposit")
    rig.jobs["job-1"] = {"state": "failed", "detail": "nobody accepted"}

    boom = {"n": 0}
    real_set = svc.set_collateral

    async def flaky(*a, **k):
        boom["n"] += 1
        if boom["n"] == 1:
            raise RuntimeError("wallet unavailable")
        return await real_set(*a, **k)
    monkeypatch.setattr(svc, "set_collateral", flaky)

    # The scan no longer lets one loan's failure escape: booking a claim can
    # refuse outright now, and a raise here would leave every LATER loan
    # unsettled on this pass and every pass after it. The loan is logged and
    # left in flight, which is what the assertions below are really about.
    await svc.settle_in_flight()

    assert await _row(loan_id) == ("out_pending", "job-1"), \
        "nothing may be committed until the money has moved"

    await svc.settle_in_flight()       # the watchdog's next pass

    assert await _row(loan_id) == ("assigned", None)
    assert await _balances() == (5, 0), "the deposit came home on the retry"


# --- 3. a scan that arrives late must not settle the wrong trade ------------

async def test_an_outcome_is_only_applied_to_the_trade_it_came_from(test_db, rig, monkeypatch):
    """settle_in_flight reads the pending loans, then asks the serve about each
    one -- and the loan can move on during that await. A player whose borrow
    settles and who immediately runs /return has a NEW job on the same row; the
    first job's outcome must not be applied to it."""
    loan_id = await _seed("out_pending", job_id="job-1")
    rig.jobs["job-1"] = {"state": "done", "give": DECK}

    async def get_job(job_id, *, mark_missing=False):
        # The borrow settles and the return is dispatched while we are asking.
        async with AsyncSessionLocal() as s:
            loan = await s.get(CardLoan, loan_id)
            loan.state, loan.job_id = "return_pending", "job-2"
            await s.commit()
        return rig.jobs.get(job_id)
    monkeypatch.setattr(rig, "get_job", get_job)

    await svc.settle_in_flight()

    assert await _row(loan_id) == ("return_pending", "job-2"), \
        "the live return must be untouched by the finished borrow"


async def test_a_loan_that_finished_in_the_meantime_is_left_alone(test_db, rig, monkeypatch):
    """The same race with no new job: the row is already at rest. `borrowed` is
    not a key of the settle/rollback tables, so applying an outcome to it used
    to raise rather than skip."""
    loan_id = await _seed("out_pending", job_id="job-1")
    rig.jobs["job-1"] = {"state": "done", "give": DECK}

    async def get_job(job_id, *, mark_missing=False):
        async with AsyncSessionLocal() as s:
            loan = await s.get(CardLoan, loan_id)
            loan.state, loan.job_id = "borrowed", None
            await s.commit()
        return rig.jobs.get(job_id)
    monkeypatch.setattr(rig, "get_job", get_job)

    await svc.settle_in_flight()

    assert await _row(loan_id) == ("borrowed", None)


# --- 3. a lost response is recovered, not handed to a human -----------------

async def test_a_lost_response_adopts_the_trade_that_did_open(test_db, rig):
    """The POST reached the serve and only the answer was lost, so a real trade
    is open. Finding it puts the loan back on the normal settling path: the
    deposit is released or kept by whatever the trade actually does, with no
    human in the loop. This is what the wallet path already does for tix."""
    await _fund()
    loan_id = await _seed()
    rig.response = {"_ambiguous": True}
    rig.orphan = {"id": "job-9", "type": "borrow", "state": "running"}

    status, _ = await svc.start_borrow(GUILD, BORROWER)

    assert status == "dispatched"
    assert await _row(loan_id) == ("out_pending", "job-9"), \
        "the loan follows the trade that really opened"


async def test_a_lost_response_with_no_trade_still_asks_for_help(test_db, rig):
    """Nothing to adopt means the request genuinely may not have landed. The
    deposit stays put rather than being refunded against a trade that might yet
    appear -- refunding and inviting a retry is how a second deck goes out."""
    await _fund()
    loan_id = await _seed()
    rig.response = {"_ambiguous": True}
    rig.orphan = None

    status, _ = await svc.start_borrow(GUILD, BORROWER)

    assert status == "dispatch_unknown"
    assert await _balances() == (0, 5), "the deposit stays held"
    # Parked, not left assigned: an assigned loan is one the dispatcher will
    # happily send again, which is the second trade this whole path exists to
    # avoid. See test_a_dispatch_we_cannot_account_for_cannot_be_retried.
    assert await _row(loan_id) == ("dispatch_unknown", None)


# --- 4. a refund belongs to the attempt that was checked --------------------

async def test_a_stale_settler_cannot_refund_a_newer_attempt(test_db, rig, monkeypatch):
    """The job check and the money are two transactions, so a settler can pass
    the check and then be overtaken: the loan settles, the borrower retries,
    and a NEW deposit is taken. Refunding "whatever is held" at that point
    hands back the deposit for a trade that is live -- the borrower ends up
    with a deck and their tix, and the library is short.

    The window is one await wide, which is exactly the kind that opens under
    load; the check therefore has to happen inside the transaction that moves
    the money.
    """
    await _fund(10)
    loan_id = await _seed("out_pending", job_id="job-1")
    await svc.set_collateral(GUILD, BORROWER, loan_id, 5)
    rig.jobs["job-1"] = {"state": "failed", "detail": "nobody accepted"}

    real_batches = svc._batches_for

    async def overtaken(loan_row):
        found = await real_batches(loan_row)
        # Between reading the order and moving the money: this attempt settles
        # and the next one is dispatched, taking its own deposit.
        async with AsyncSessionLocal() as s:
            loan = await s.get(CardLoan, loan_id)
            loan.state, loan.job_id = "out_pending", "job-2"
            await s.commit()
        return found
    monkeypatch.setattr(svc, "_batches_for", overtaken)

    await svc.settle_in_flight()

    borrower, holder = await _balances()
    assert holder == 5, f"the live attempt's deposit was refunded (holder={holder})"
    assert await _row(loan_id) == ("out_pending", "job-2"), "and its trade is untouched"


async def test_a_dispatch_we_cannot_account_for_cannot_be_retried(test_db, rig, monkeypatch):
    """The request reached the serve and only the answer was lost, with no job
    to adopt -- so a trade may be open and moving the library's cards.

    The message asks the borrower not to retry, and a message is not a guard:
    the dispatcher accepts an "assigned" loan, and set_collateral is a target
    rather than a charge, so a retry costs nothing and opens a SECOND trade
    against the first. The loan is taken out of reach instead.
    """
    await _fund()
    loan_id = await _seed("assigned")
    # A lost response, and nothing in the job list to adopt.
    rig.response = {"_ambiguous": True}
    rig.orphan = None

    status, _ = await svc.start_borrow(GUILD, BORROWER)
    assert status == "dispatch_unknown"

    assert (await _row(loan_id))[0] == "dispatch_unknown", \
        "an assigned loan here is a second deck waiting to go out"

    # ...and the retry the player will try anyway finds nothing to dispatch.
    again, _ = await svc.start_borrow(GUILD, BORROWER)
    assert again != "dispatched", f"a second trade opened against a live one: {again}"


async def test_the_borrower_still_counts_as_holding_a_deck(test_db, rig):
    """Parked is ACTIVE: while a trade may be on its way to them, they must not
    be handed a second deck at the next draft."""
    from models.card_loan import ACTIVE_STATES

    assert "dispatch_unknown" in ACTIVE_STATES


async def test_an_orphaned_trade_is_not_adopted_into_a_later_order(test_db, rig):
    """Batches used to be found by borrower, on the reasoning that a borrower
    has one live loan so their pending rows must all be its trades.

    A crashed dispatch breaks that: it leaves a pending row with nothing
    pointing at it. Found by borrower, the orphan joins the NEXT order and --
    because the settler reads the kind off the first row it finds -- turns a
    return into a borrow. The real return is never booked, the orphan's cards
    are booked as a second loan, and the loan can never be closed.
    """
    from models.mtgo_job import MtgoJob

    loan_id = await _seed("return_pending", job_id="job-live")
    async with AsyncSessionLocal() as s:
        loan = await s.get(CardLoan, loan_id)
        # The orphan: an older BORROW trade from a dispatch that died before it
        # could update the loan. Same guild, same borrower, still pending.
        s.add(MtgoJob(job_id="job-orphan", kind="borrow", guild_id=GUILD,
                      library_id="lib", order_id="loan:999",
                      player_id=BORROWER, mtgo_user=HANDLE, amount=4,
                      card_name=None, status="pending"))
        # ...and this order's own trade.
        s.add(MtgoJob(job_id="job-live", kind="return", guild_id=GUILD,
                      library_id="lib", order_id=svc.order_of(loan),
                      player_id=BORROWER, mtgo_user=HANDLE, amount=4,
                      card_name=None, status="pending"))
        await s.commit()

    async with AsyncSessionLocal() as s:
        this_loan = await s.get(CardLoan, loan_id)
        found = await svc._batches_for(this_loan)

    assert [b.job_id for b in found] == ["job-live"], \
        "an orphan from another order is not this order's trade"
    assert all(b.kind == "return" for b in found), \
        "and cannot flip this order's kind"
