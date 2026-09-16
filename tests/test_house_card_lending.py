"""House card lending: the vault lends real cards and takes exactly those copies back.

Two rules carry the whole feature, and both are about NOT creating a false record:

  * The claim is written ONLY on 'done'. A trade that failed moved no cards, so it must
    leave no obligation behind. This is the rule the TradeBot's false-failure bug broke on
    2026-08-13, where a completed trade was reported failed.
  * DraftBot never learns a printing. The serve records which printings crossed and pins
    them itself on the way back, so every request here is by NAME. A test that asserted a
    catId travelling would be asserting the old design.
"""
import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from database.models_base import Base
from database.db_session import AsyncSessionLocal
from models.debt_ledger import DebtLedger
from models.mtgo_job import MtgoJob
from services import mtgo_resolution_service as mrs
from services import wallet_service

GUILD, PLAYER, MTGO_USER, CARD = "g1", "1234567890", "jasper", "Lightning Bolt"


@pytest_asyncio.fixture
async def test_db():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    AsyncSessionLocal.configure(bind=engine)
    yield engine
    await engine.dispose()
    os.unlink(tmp.name)


def _client(**overrides):
    """A serve client stub. Enabled and idle unless a test says otherwise."""
    c = AsyncMock()
    c.enabled = True
    c.borrow = AsyncMock(return_value={"id": "job-1"})
    c.return_cards = AsyncMock(return_value={"id": "job-2"})
    c.positions = AsyncMock(return_value={"user": MTGO_USER, "held": [], "lent": []})
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


def _serve(client, *, busy=None):
    """Patch the client and the busy gate together — every start_* consults both."""
    return (patch.object(mrs, "get_client", return_value=client),
            patch.object(mrs, "serve_busy_reason", new=AsyncMock(return_value=busy)))


async def _card_rows(card=CARD):
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(DebtLedger).where(DebtLedger.card_name == card))).scalars().all()
    return rows


async def _position(player, card=CARD):
    """Net copies `player` owes for `card` (negative = they hold them)."""
    return sum(r.amount for r in await _card_rows(card) if r.player_id == player)


# ---- borrow -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_completed_loan_books_the_obligation_to_the_house(test_db):
    client = _client()
    p1, p2 = _serve(client)
    with p1, p2:
        started = await mrs.start_borrow(GUILD, PLAYER, MTGO_USER, CARD, 4)
        assert started["ok"], started
        with patch.object(mrs, "_poll_job", new=AsyncMock(return_value=("done", {}))):
            done = await mrs.finish_borrow(started["job_id"], GUILD, PLAYER, CARD, 4)
    assert done["ok"] and done["outcome"] == "done"

    # The borrower owes 4; the house is owed 4. Double entry, same as tix.
    assert await _position(PLAYER) == -4
    assert await _position(wallet_service.HOUSE_MTGO) == 4


@pytest.mark.asyncio
async def test_a_failed_loan_leaves_no_obligation(test_db):
    """The rule the 2026-08-13 false-failure bug broke: nothing moved, nothing owed."""
    client = _client()
    p1, p2 = _serve(client)
    with p1, p2:
        started = await mrs.start_borrow(GUILD, PLAYER, MTGO_USER, CARD, 4)
        with patch.object(mrs, "_poll_job",
                          new=AsyncMock(return_value=("failed", {"detail": "partner declined"}))):
            res = await mrs.finish_borrow(started["job_id"], GUILD, PLAYER, CARD, 4)
    assert not res["ok"] and res["outcome"] == "failed"
    assert "declined" in res["error"]
    assert await _card_rows() == [], "a failed trade must not book a claim"


@pytest.mark.asyncio
async def test_a_pending_loan_books_nothing_yet(test_db):
    """A poll that times out is not an outcome. The watchdog resolves it later."""
    client = _client()
    p1, p2 = _serve(client)
    with p1, p2:
        started = await mrs.start_borrow(GUILD, PLAYER, MTGO_USER, CARD, 2)
        with patch.object(mrs, "_poll_job", new=AsyncMock(return_value=("pending", {}))):
            res = await mrs.finish_borrow(started["job_id"], GUILD, PLAYER, CARD, 2)
    assert res["outcome"] == "pending"
    assert await _card_rows() == []
    async with AsyncSessionLocal() as s:
        job = await s.get(MtgoJob, started["job_id"])
    assert job is not None and job.status == "pending", "the resumer needs this row"


@pytest.mark.asyncio
async def test_the_loan_is_requested_by_name_and_carries_no_printing(test_db):
    """The delegation, asserted. DraftBot has no catId to send and must not invent one."""
    client = _client()
    p1, p2 = _serve(client)
    with p1, p2:
        await mrs.start_borrow(GUILD, PLAYER, MTGO_USER, CARD, 3)
    client.borrow.assert_awaited_once()
    args, kwargs = client.borrow.await_args
    assert args[0] == MTGO_USER and args[1] == CARD and args[2] == 3
    assert "catId" not in str(kwargs) and "cat_id" not in str(kwargs)


@pytest.mark.asyncio
async def test_the_job_row_records_the_card_and_quantity(test_db):
    client = _client()
    p1, p2 = _serve(client)
    with p1, p2:
        started = await mrs.start_borrow(GUILD, PLAYER, MTGO_USER, CARD, 4)
    async with AsyncSessionLocal() as s:
        job = await s.get(MtgoJob, started["job_id"])
    assert job.kind == "borrow" and job.card_name == CARD and job.amount == 4
    assert job.mtgo_user == MTGO_USER


@pytest.mark.asyncio
async def test_a_rejected_post_books_nothing_and_reports_why(test_db):
    client = _client(borrow=AsyncMock(return_value=None))
    p1, p2 = _serve(client)
    with p1, p2:
        res = await mrs.start_borrow(GUILD, PLAYER, MTGO_USER, CARD, 4)
    assert not res["ok"] and "did not accept" in res["error"]
    assert await _card_rows() == []


@pytest.mark.asyncio
async def test_a_busy_custodian_defers_the_loan(test_db):
    client = _client()
    p1, p2 = _serve(client, busy="custodian is busy with 1 job")
    with p1, p2:
        res = await mrs.start_borrow(GUILD, PLAYER, MTGO_USER, CARD, 1)
    assert not res["ok"] and res.get("busy")
    client.borrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_nonsense_quantities_are_refused_before_any_trade(test_db):
    client = _client()
    p1, p2 = _serve(client)
    with p1, p2:
        assert not (await mrs.start_borrow(GUILD, PLAYER, MTGO_USER, CARD, 0))["ok"]
        assert not (await mrs.start_borrow(GUILD, PLAYER, MTGO_USER, CARD, -3))["ok"]
        assert not (await mrs.start_borrow(GUILD, PLAYER, MTGO_USER, "  ", 1))["ok"]
    client.borrow.assert_not_awaited()


# ---- return -----------------------------------------------------------------------

LENT_4 = {"user": MTGO_USER, "held": [],
          "lent": [{"card": CARD, "catId": 111, "qty": 3, "since": "2026-09-01T00:00:00Z"},
                   {"card": CARD, "catId": 222, "qty": 1, "since": "2026-09-02T00:00:00Z"}]}


@pytest.mark.asyncio
async def test_a_completed_return_settles_the_obligation(test_db):
    client = _client(positions=AsyncMock(return_value=LENT_4))
    p1, p2 = _serve(client)
    with p1, p2:
        b = await mrs.start_borrow(GUILD, PLAYER, MTGO_USER, CARD, 4)
        with patch.object(mrs, "_poll_job", new=AsyncMock(return_value=("done", {}))):
            await mrs.finish_borrow(b["job_id"], GUILD, PLAYER, CARD, 4)
            assert await _position(PLAYER) == -4

            r = await mrs.start_return(GUILD, PLAYER, MTGO_USER, CARD)
            assert r["ok"] and r["quantity"] == 4, r
            await mrs.finish_return(r["job_id"], GUILD, PLAYER, CARD, 4)

    assert await _position(PLAYER) == 0, "the loan should be fully settled"
    assert await _position(wallet_service.HOUSE_MTGO) == 0


@pytest.mark.asyncio
async def test_a_return_spanning_two_printings_is_one_job(test_db):
    """The serve pins both printings itself, so DraftBot sees one card and one job —
    the thing the August design could not express."""
    client = _client(positions=AsyncMock(return_value=LENT_4))
    p1, p2 = _serve(client)
    with p1, p2:
        r = await mrs.start_return(GUILD, PLAYER, MTGO_USER, CARD)
    assert r["ok"] and r["quantity"] == 4
    client.return_cards.assert_awaited_once()
    async with AsyncSessionLocal() as s:
        job = await s.get(MtgoJob, r["job_id"])
    assert job.kind == "return" and job.amount == 4


@pytest.mark.asyncio
async def test_returning_more_than_is_out_is_capped_at_the_open_position(test_db):
    client = _client(positions=AsyncMock(return_value=LENT_4))
    p1, p2 = _serve(client)
    with p1, p2:
        r = await mrs.start_return(GUILD, PLAYER, MTGO_USER, CARD, 99)
    assert r["ok"] and r["quantity"] == 4, "cannot return more than the vault lent"


@pytest.mark.asyncio
async def test_returning_a_card_that_was_never_lent_is_refused(test_db):
    client = _client()   # positions: nothing lent
    p1, p2 = _serve(client)
    with p1, p2:
        res = await mrs.start_return(GUILD, PLAYER, MTGO_USER, CARD)
    assert not res["ok"] and "has not lent you" in res["error"]
    client.return_cards.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_return_leaves_the_obligation_standing(test_db):
    """Symmetric to the borrow rule: the cards did not come back, so the debt remains."""
    client = _client(positions=AsyncMock(return_value=LENT_4))
    p1, p2 = _serve(client)
    with p1, p2:
        b = await mrs.start_borrow(GUILD, PLAYER, MTGO_USER, CARD, 4)
        with patch.object(mrs, "_poll_job", new=AsyncMock(return_value=("done", {}))):
            await mrs.finish_borrow(b["job_id"], GUILD, PLAYER, CARD, 4)
        r = await mrs.start_return(GUILD, PLAYER, MTGO_USER, CARD)
        with patch.object(mrs, "_poll_job",
                          new=AsyncMock(return_value=("failed", {"detail": "trade cancelled"}))):
            res = await mrs.finish_return(r["job_id"], GUILD, PLAYER, CARD, 4)
    assert not res["ok"]
    assert await _position(PLAYER) == -4, "a failed return must not clear the debt"


# ---- the house as a counterparty ---------------------------------------------------

def test_the_house_is_a_synthetic_holder_so_renderers_skip_it():
    """Every place that lists people filters on is_system_account. The house must land on
    the synthetic side or it shows up in the UI as a player nobody can pay."""
    assert wallet_service.is_system_account(wallet_service.HOUSE_MTGO)
    assert not wallet_service.is_system_account(PLAYER)


# ---- position reads ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_outstanding_reads_the_serve_not_the_ledger():
    """These two must stay independent: the serve watched the cards cross, the ledger is
    the claim. Deriving one from the other would hide exactly the divergence worth finding.
    """
    client = _client(positions=AsyncMock(return_value=LENT_4))
    with patch.object(mrs, "get_client", return_value=client):
        assert await mrs.outstanding_with_house(MTGO_USER, CARD) == 4
        assert await mrs.outstanding_with_house(MTGO_USER, "Brainstorm") == 0
        assert await mrs.outstanding_with_house(MTGO_USER) == 4


@pytest.mark.asyncio
async def test_an_unreachable_serve_reports_nothing_outstanding_not_a_crash():
    client = _client(positions=AsyncMock(return_value=None))
    with patch.object(mrs, "get_client", return_value=client):
        assert await mrs.outstanding_with_house(MTGO_USER, CARD) == 0


# ---------------------------------------------------------------------------
# batched orders — MTGO caps a trade at a few hundred cards, so an order above
# the serve's limit comes back as several jobs instead of one
#
# The invariant these guard: every card of the order is recorded against SOME job.
# A batch booked against no job is a player credited for less than they handed over
# (deposit) or tix stranded in in-flight with nothing able to resolve them (withdraw).
# ---------------------------------------------------------------------------
def _batched(*sizes, kind="receive"):
    """A serve batched response: no top-level id, one entry per trade."""
    return {"batched": True, "batchCount": len(sizes),
            "batches": [{"id": f"b{i}", "give": [] if kind == "receive" else [{"qty": n}],
                         "receive": [{"qty": n}] if kind == "receive" else []}
                        for i, n in enumerate(sizes)]}


def test_a_single_job_books_the_amount_the_caller_asked_for():
    """Not re-derived from the response: a thin body must not book zero."""
    assert mrs._jobs_from({"id": "job-1"}, 4) == [("job-1", 4)]


def test_a_batched_response_yields_every_job_with_its_own_share():
    assert mrs._jobs_from(_batched(300, 300, 100), 700) == [("b0", 300), ("b1", 300), ("b2", 100)]


def test_a_split_that_loses_cards_is_refused_outright():
    """700 asked, 600 across the batches -> 100 would be recorded against no job at all."""
    assert mrs._jobs_from(_batched(300, 300), 700) == []


def test_a_response_with_no_id_and_no_batches_is_refused():
    """The serve omits the top-level id when it splits; anything unrecognised must not
    silently become 'one job for the whole order'."""
    assert mrs._jobs_from({"batched": True, "batches": []}, 700) == []
    assert mrs._jobs_from({}, 700) == []
    assert mrs._jobs_from(None, 700) == []


def test_started_hides_job_id_once_an_order_splits():
    """A caller still written against one id must break loudly, not book one batch and
    silently drop the rest."""
    one = mrs._started([("j1", 4)])
    assert one["job_id"] == "j1" and one["jobs"] == [{"id": "j1", "n": 4}]
    many = mrs._started([("b0", 300), ("b1", 100)])
    assert "job_id" not in many
    assert many["jobs"] == [{"id": "b0", "n": 300}, {"id": "b1", "n": 100}]


@pytest.mark.asyncio
async def test_a_batched_borrow_records_one_job_per_batch(test_db):
    client = _client(borrow=AsyncMock(return_value=_batched(300, 120, kind="give")))
    p1, p2 = _serve(client)
    with p1, p2:
        started = await mrs.start_borrow(GUILD, PLAYER, MTGO_USER, CARD, 420)
    assert started["ok"] and "job_id" not in started
    assert started["jobs"] == [{"id": "b0", "n": 300}, {"id": "b1", "n": 120}]
    async with AsyncSessionLocal() as s:
        rows = {j.job_id: j.amount for j in (await s.execute(select(MtgoJob))).scalars()}
    assert rows == {"b0": 300, "b1": 120}, "every batch needs its own durable row"


@pytest.mark.asyncio
async def test_a_borrow_whose_batches_lose_cards_records_nothing(test_db):
    client = _client(borrow=AsyncMock(return_value=_batched(300, 100, kind="give")))
    p1, p2 = _serve(client)
    with p1, p2:
        started = await mrs.start_borrow(GUILD, PLAYER, MTGO_USER, CARD, 420)
    assert not started["ok"]
    async with AsyncSessionLocal() as s:
        assert (await s.execute(select(MtgoJob))).scalars().all() == []
