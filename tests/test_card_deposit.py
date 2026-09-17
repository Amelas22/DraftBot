"""Putting cards INTO the library, and what the bot then owes you.

A deposit is a loan in reverse: the depositor hands cards over and the library
owes them back. That is the same mirrored pair the debt ledger already writes
for a loan, with the roles swapped -- so one signed view answers both "what am
I holding of theirs" and "what are they holding of mine", with no second table.

The claim moves only when the trade reports done, for the same reason it does
on the way out: a deposit booked on dispatch would have the ledger owing cards
to someone who never sent them.
"""
import pytest

from database.db_session import AsyncSessionLocal
from models.mtgo_job import MtgoJob
from services import debt_service, wallet_service
import services.card_deposit_service as svc
from conftest import FakeLendingServe

pytestmark = pytest.mark.asyncio

GUILD, OWNER, HANDLE = "g1", "u1", "Depositor01"
CARDS = [{"name": "Adarkar Valkyrie", "qty": 1}, {"name": "Auramancer", "qty": 2}]


@pytest.fixture
def rig(monkeypatch):
    client = FakeLendingServe()
    monkeypatch.setattr(svc, "get_lending_client", lambda: client)

    async def handle(_):
        return HANDLE
    monkeypatch.setattr(svc, "_mtgo_handle", handle)

    async def free():
        return None
    monkeypatch.setattr(svc, "library_busy_reason", free)
    return client


def _stock(stock):
    """available_now, stubbed: what the library can physically hand over."""
    async def _avail(guild_id):
        return dict(stock)
    return _avail


async def _owed_to(player):
    """What the library holds of theirs -- custody, not the loan counterparty."""
    rows = await debt_service.get_open_card_positions(
        GUILD, player, wallet_service.HOUSE_LIBRARY)
    return {r["card_name"]: r["net"] for r in rows}


async def test_a_deposit_is_offered_to_the_serve(test_db, rig):
    status, _ = await svc.start_deposit(GUILD, OWNER, CARDS)

    assert status == "dispatched"
    assert rig.deposited == [(HANDLE, CARDS)]


async def test_nothing_is_owed_until_the_trade_completes(test_db, rig):
    """Booked on dispatch, the ledger would owe cards to someone who never
    sent them -- the mirror of the rule on the way out."""
    await svc.start_deposit(GUILD, OWNER, CARDS)

    assert await _owed_to(OWNER) == {}


async def test_a_completed_deposit_is_owed_back_to_the_depositor(test_db, rig):
    rig.jobs["job-1"] = {"state": "done", "receive": CARDS}
    await svc.start_deposit(GUILD, OWNER, CARDS)

    await svc.settle_deposits(GUILD)

    assert await _owed_to(OWNER) == {"Adarkar Valkyrie": 1, "Auramancer": 2}, \
        "positive: the library owes these back"


async def test_a_failed_deposit_owes_nobody_anything(test_db, rig):
    rig.jobs["job-1"] = {"state": "failed", "detail": "binder was short"}
    await svc.start_deposit(GUILD, OWNER, CARDS)

    await svc.settle_deposits(GUILD)

    assert await _owed_to(OWNER) == {}


async def test_what_arrived_is_what_is_owed(test_db, rig):
    """Read off the serve's record of the trade, not off what was offered: a
    depositor whose binder was short sends fewer than they meant to."""
    rig.jobs["job-1"] = {"state": "done", "receive": [{"name": "Auramancer", "qty": 1}]}
    await svc.start_deposit(GUILD, OWNER, CARDS)

    await svc.settle_deposits(GUILD)

    assert await _owed_to(OWNER) == {"Auramancer": 1}


async def test_a_deposit_too_big_for_one_trade_is_refused(test_db, rig):
    big = [{"name": "Swamp", "qty": 9999}]

    status, detail = await svc.start_deposit(GUILD, OWNER, big)

    assert status == "too_large"
    assert rig.deposited == [], "nothing may reach the serve"


async def test_deposit_jobs_are_their_own_kind(test_db, rig):
    """The wallet's TIX deposit is already kind='deposit' with no card name, so
    a card deposit that reused it would be picked up by the wallet's resumer
    and polled against the wrong serve -- the collision that cost a live loan
    its deposit on the lending side."""
    await svc.start_deposit(GUILD, OWNER, CARDS)

    async with AsyncSessionLocal() as s:
        kinds = [j.kind for j in (await s.scalars(__import__("sqlalchemy").select(MtgoJob))).all()]
    assert kinds == ["card-deposit"], f"got {kinds}"


async def test_the_library_watchdog_settles_deposits_too(test_db, rig, monkeypatch):
    """Deposits run against the same serve as loans, so they are settled by the
    same loop: a second watchdog polling one serve would only take turns waiting
    for the first. A deposit whose command poller died -- a timeout, a restart --
    has to be picked up by something."""
    import services.card_lending_service as lending

    rig.jobs["job-1"] = {"state": "done", "receive": CARDS}
    await svc.start_deposit(GUILD, OWNER, CARDS)
    monkeypatch.setattr(lending, "get_lending_client", lambda: rig)

    seen = {}
    real = svc.settle_deposits

    async def watched(guild_id=None):
        seen["ran"] = True
        return await real(guild_id)
    monkeypatch.setattr(svc, "settle_deposits", watched)

    await lending.settle_in_flight()          # the loan half
    await svc.settle_deposits()               # what the watchdog calls next

    assert await _owed_to(OWNER) == {"Adarkar Valkyrie": 1, "Auramancer": 2}


# --- taking them back out ---------------------------------------------------

async def _deposit_and_settle(rig, cards, job="job-1"):
    rig.job_id = job
    rig.jobs[job] = {"state": "done", "receive": cards}
    await svc.start_deposit(GUILD, OWNER, cards)
    await svc.settle_deposits(GUILD)


async def test_withdrawing_asks_for_everything_without_naming_it(test_db, rig, monkeypatch):
    """The serve pins the exact printings it received from its own record, so a
    whole-position withdraw names no cards -- and could only disagree with what
    actually crossed if it tried to."""
    await _deposit_and_settle(rig, CARDS)
    monkeypatch.setattr(svc, "available_now", _stock({"Adarkar Valkyrie": 1, "Auramancer": 2}))

    status, _ = await svc.start_withdrawal(GUILD, OWNER)

    assert status == "dispatched"
    assert rig.withdrawn == [(HANDLE, None)], "no card list goes out"


async def test_a_completed_withdrawal_clears_what_was_owed(test_db, rig, monkeypatch):
    await _deposit_and_settle(rig, CARDS)
    monkeypatch.setattr(svc, "available_now", _stock({"Adarkar Valkyrie": 1, "Auramancer": 2}))
    rig.job_id = "job-2"
    rig.jobs["job-2"] = {"state": "done", "give": CARDS}

    await svc.start_withdrawal(GUILD, OWNER)
    await svc.settle_deposits(GUILD)

    assert await _owed_to(OWNER) == {}, "the library owes them nothing now"


async def test_a_card_the_vault_does_not_list_is_not_assumed_missing(test_db, rig, monkeypatch):
    """/vault truncates its listing, so a deposit of any size runs off the end
    of it. Reading absence as "out on loan" would refuse the normal case."""
    await _deposit_and_settle(rig, CARDS)
    monkeypatch.setattr(svc, "available_now", _stock({}))

    status, _ = await svc.start_withdrawal(GUILD, OWNER)

    assert status == "dispatched", "an unlisted card is presumed present"


async def test_cards_out_on_loan_are_named_rather_than_traded_for(test_db, rig, monkeypatch):
    """Withdrawing what a borrower is holding opens a trade the bot cannot
    complete: its binder is short by exactly the cards that are out. Better to
    say which, and who has to bring them back, than to fail in MTGO."""
    await _deposit_and_settle(rig, CARDS)
    monkeypatch.setattr(svc, "available_now",
                        _stock({"Adarkar Valkyrie": 1, "Auramancer": 1}))

    status, detail = await svc.start_withdrawal(GUILD, OWNER)

    assert status == "some_on_loan"
    assert "Auramancer" in (detail or ""), f"say what is out: {detail}"
    assert rig.withdrawn == [], "no doomed trade is opened"


async def test_withdrawing_nothing_says_so(test_db, rig):
    status, _ = await svc.start_withdrawal(GUILD, OWNER)

    assert status == "nothing_held"


# --- a deposit and a loan are different obligations --------------------------

async def test_a_deposit_and_a_loan_do_not_cancel_each_other(test_db, rig):
    """They run in opposite directions against the same people, so netting them
    into one position makes a deposit invisible -- and makes a LIVE loan read as
    settled, freeing its borrower for another deck while the cards are still out.
    The serve honours the two separately; the ledger has to as well.
    """
    from services import debt_service
    import services.card_lending_service as lending

    # the library lends them 2 Auramancer...
    await debt_service.create_card_loan(
        guild_id=GUILD, lender_id=wallet_service.HOUSE_MTGO, borrower_id=OWNER,
        card_name="Auramancer", quantity=2, created_by="test")
    # ...and separately holds 2 of their own
    await _deposit_and_settle(rig, [{"name": "Auramancer", "qty": 2}])

    assert await svc.held_for(GUILD, OWNER) == [{"name": "Auramancer", "qty": 2}], \
        "their deposit is still theirs"
    assert await lending._still_owed(GUILD, OWNER) == [{"name": "Auramancer", "qty": 2}], \
        "and they still owe the deck they borrowed"


async def test_a_deposit_is_watched_until_it_lands(test_db, rig, monkeypatch):
    """The trade waits ten minutes for a human to accept, so asking the serve
    once -- immediately -- finds it queued every time. The depositor was told to
    accept a trade and then heard nothing ever again, because the watchdog books
    it minutes later and messages nobody."""
    rig.jobs["job-1"] = {"state": "queued"}
    status, job_id = await svc.start_deposit(GUILD, OWNER, CARDS)
    assert status == "dispatched"

    # ...they accept it while the poller is waiting
    async def accepted(job, *a, **k):
        rig.jobs["job-1"] = {"state": "done", "receive": CARDS}
        return await FakeLendingServe.get_job(rig, job, *a, **k)
    monkeypatch.setattr(rig, "get_job", accepted)

    outcome = await svc.poll_until_settled(GUILD, job_id, timeout_s=1, interval_s=0)

    assert outcome["state"] == "done"
    assert await _owed_to(OWNER) == {"Adarkar Valkyrie": 1, "Auramancer": 2}


async def test_a_trade_that_outlives_the_poller_is_left_to_the_watchdog(test_db, rig):
    """Giving up waiting is not failing: the job keeps its row and the watchdog
    settles it. Saying 'it failed' here would be a lie about a live trade."""
    rig.jobs["job-1"] = {"state": "queued"}
    _, job_id = await svc.start_deposit(GUILD, OWNER, CARDS)

    outcome = await svc.poll_until_settled(GUILD, job_id, timeout_s=0, interval_s=0)

    assert outcome["state"] == "running"


async def test_a_lost_deposit_response_adopts_the_trade_that_did_open(test_db, rig):
    """The POST reached the serve and only the answer was lost, so a real trade
    may be open -- and if the depositor accepts it, their cards are inside the
    library with no job row and nothing that will ever look for them. Worse than
    the lending equivalent: there the exposure is the house's, here it is
    somebody's own property."""
    rig.response = {"_ambiguous": True}
    rig.orphan = {"id": "job-9", "type": "deposit", "state": "running"}

    status, job_id = await svc.start_deposit(GUILD, OWNER, CARDS)

    assert status == "dispatched"
    assert job_id == "job-9", "the loan follows the trade that really opened"


async def test_a_lost_response_with_no_trade_says_nothing_was_recorded(test_db, rig):
    rig.response = {"_ambiguous": True}
    rig.orphan = None

    status, _ = await svc.start_deposit(GUILD, OWNER, CARDS)

    assert status == "dispatch_unknown"
