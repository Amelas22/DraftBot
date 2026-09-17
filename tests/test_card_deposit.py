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


async def _owed_to(player):
    rows = await debt_service.get_open_card_positions(
        GUILD, player, wallet_service.HOUSE_MTGO)
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
