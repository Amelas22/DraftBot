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
import pytest

from database.db_session import AsyncSessionLocal, db_session
from models.card_loan import CardLoan
from services import wallet_service
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
        loan = CardLoan(guild_id=GUILD, borrower_id=BORROWER, cards=DECK, state=state,
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

    with pytest.raises(RuntimeError):
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
    assert await _row(loan_id) == ("assigned", None)


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

    async def overtaken(guild, borrower):
        found = await real_batches(guild, borrower)
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
