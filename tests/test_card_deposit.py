"""Putting cards INTO the library, and what the bot then owes you.

A deposit is a loan in reverse: the depositor hands cards over and the library
owes them back. That is the same mirrored pair the debt ledger already writes
for a loan, with the roles swapped -- so one signed view answers both "what am
I holding of theirs" and "what are they holding of mine", with no second table.

The claim moves only when the trade reports done, for the same reason it does
on the way out: a deposit booked on dispatch would have the ledger owing cards
to someone who never sent them.
"""
import asyncio

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


async def test_settling_the_same_trade_twice_books_it_once(test_db, rig):
    """The watchdog and the command's own poller both settle, and they overlap
    by design -- the poller calls settle_deposits in a loop while the watchdog
    is doing its rounds. The ledger is append-only, so a second booking is not
    an overwrite; it is a second claim on the same cards that nothing removes.
    """
    rig.jobs["job-1"] = {"state": "done", "receive": CARDS}
    await svc.start_deposit(GUILD, OWNER, CARDS)

    await svc.settle_deposits(GUILD)
    once = await _owed_to(OWNER)
    await svc.settle_deposits(GUILD)

    assert once == {"Adarkar Valkyrie": 1, "Auramancer": 2}
    assert await _owed_to(OWNER) == once, "settling again must not re-book"


async def test_a_trade_the_serve_has_forgotten_is_failed_not_left_pending(test_db, rig):
    """The serve restarted and lost its job list, so this job can never report.
    Left pending it would be polled forever; booked it would invent cards. It
    fails, and nothing was booked on dispatch, so nothing has to unwind."""
    await svc.start_deposit(GUILD, OWNER, CARDS)
    rig.jobs["job-1"] = {"_missing": True}

    settled = await svc.settle_deposits(GUILD)

    assert settled["job-1"]["state"] == "failed"
    assert await _owed_to(OWNER) == {}

    async with AsyncSessionLocal() as session:
        assert (await session.get(MtgoJob, "job-1")).status == "failed"


async def test_a_cube_too_big_for_one_trade_becomes_several(test_db, rig):
    """What /deposit actually does with a big cube. The service refuses an
    oversized ORDER (below); the command never hands it one, because splitting
    is the caller's job -- so this is the behaviour a depositor sees."""
    from cogs.card_deposit_commands import chunk_cards

    big = [{"name": "Swamp", "qty": 25}]

    for chunk in chunk_cards(big, 10):
        assert (await svc.start_deposit(GUILD, OWNER, chunk))[0] == "dispatched"

    assert [sum(c["qty"] for c in cards) for _, cards in rig.deposited] == [10, 10, 5]


async def test_an_order_too_big_for_one_trade_is_refused(test_db, rig):
    """The backstop under that: an order that arrives unsplit is refused rather
    than handed to the serve, which would answer it by running several trades
    of its own with nothing tying them back to the order."""
    big = [{"name": "Swamp", "qty": 9999}]

    status, _ = await svc.start_deposit(GUILD, OWNER, big)

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

    # The real loop, not a stand-in for it: the line under test is the
    # watchdog's own call into the deposit half, and calling settle_deposits
    # by hand here would pass just as well with that line deleted. It runs
    # forever and refuses to start twice, so the guard is cleared and the task
    # cancelled once it has done its round.
    monkeypatch.setattr(lending, "_watchdog_running", False)
    task = asyncio.create_task(lending.lending_jobs_watchdog(interval_s=0.01))
    try:
        for _ in range(200):
            await asyncio.sleep(0.01)
            # Waits on the JOB, not on the ledger: the claim is written card by
            # card and the row is resolved after the last one, so a poll that
            # stopped at "something is owed" would read a half-booked trade.
            async with AsyncSessionLocal() as session:
                if (await session.get(MtgoJob, "job-1")).status != "pending":
                    break
    finally:
        task.cancel()

    assert await _owed_to(OWNER) == {"Adarkar Valkyrie": 1, "Auramancer": 2}


# --- taking them back out ---------------------------------------------------

async def _deposit_and_settle(rig, cards, job="job-1"):
    """Leaves the serve handing out a FRESH id, the way a real one does -- a
    withdrawal that reused the deposit's job id would be adopting its trade."""
    rig.job_id = job
    rig.jobs[job] = {"state": "done", "receive": cards}
    await svc.start_deposit(GUILD, OWNER, cards)
    await svc.settle_deposits(GUILD)
    rig.job_id = f"{job}-next"


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


# --- which side of the trade a kind reads -----------------------------------

async def test_each_kind_reads_the_side_the_bot_was_on():
    """One table for all four kinds, because they pair off into opposite sides
    and a second copy is a second chance to get a pair backwards. A kind read
    off the wrong side reports an empty trade, which settles as "nothing
    crossed" -- the ledger then says the cards never moved and nobody looks
    for them again.
    """
    from services.card_lending_service import _items_moved

    job = {"give": [{"name": "Swamp", "qty": 2}],
           "receive": [{"name": "Island", "qty": 3}]}

    assert await _items_moved(job, "borrow") == [{"name": "Swamp", "qty": 2}]
    assert await _items_moved(job, "card-withdraw") == [{"name": "Swamp", "qty": 2}]
    assert await _items_moved(job, "return") == [{"name": "Island", "qty": 3}]
    assert await _items_moved(job, "card-deposit") == [{"name": "Island", "qty": 3}]


async def test_an_unknown_kind_is_refused_rather_than_guessed():
    from services.card_lending_service import _items_moved

    with pytest.raises(ValueError):
        await _items_moved({"give": [], "receive": []}, "card-donate")


# --- who gets there first --------------------------------------------------

async def test_a_trade_the_watchdog_settled_is_still_reported_to_the_depositor(
        test_db, rig):
    """settle_deposits reports only what IT resolved, and the watchdog is
    scanning the same rows on its own schedule. Whichever wins takes the row
    out of "pending", so the loser sees nothing -- and the command's poller
    would wait out its whole timeout on a trade that had already finished,
    tell the depositor it was still open, and stop a multi-trade run that
    could have carried on.
    """
    rig.jobs["job-1"] = {"state": "done", "receive": CARDS}
    await svc.start_deposit(GUILD, OWNER, CARDS)

    await svc.settle_deposits(GUILD)          # the watchdog gets there first
    outcome = await svc.poll_until_settled(GUILD, "job-1", timeout_s=0)

    assert outcome["state"] == "done", "the row is the answer, not who settled it"


async def test_adoption_refuses_an_earlier_attempt_s_finished_trade(test_db, rig):
    """Two chunks of one cube can be identical, and the /jobs scan matches on
    type, handle and card list -- so a lost response on chunk two can match the
    trade chunk one already completed. Adopting it would report that trade's
    outcome as this one's, crediting cards twice over for one movement."""
    rig.jobs["job-1"] = {"state": "done", "receive": CARDS}
    await svc.start_deposit(GUILD, OWNER, CARDS)
    await svc.settle_deposits(GUILD)

    rig.response = {"_ambiguous": True}
    rig.orphan = {"id": "job-1"}
    status, _ = await svc.start_deposit(GUILD, OWNER, CARDS)

    assert status == "dispatch_unknown"
    assert await _owed_to(OWNER) == {"Adarkar Valkyrie": 1, "Auramancer": 2}


async def test_one_name_listed_twice_in_a_trade_is_not_half_lost(test_db, rig):
    """The claim for a trade is keyed by job and card name, so a serve that
    reports a name in two entries would have the second read as the same
    movement and dropped as already booked -- quietly losing those copies."""
    rig.jobs["job-1"] = {"state": "done",
                         "receive": [{"name": "Auramancer", "qty": 2},
                                     {"name": "Auramancer", "qty": 3}]}
    await svc.start_deposit(GUILD, OWNER, [{"name": "Auramancer", "qty": 5}])

    await svc.settle_deposits(GUILD)

    assert await _owed_to(OWNER) == {"Auramancer": 5}
