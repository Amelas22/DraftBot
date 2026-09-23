"""A tix order larger than one trade goes as several, and pays out as it goes.

The custodian REFUSES an order above its per-trade limit rather than splitting
it, so anything larger has to be sent as several trades. Chunking is a
convenience over running the command that many times, and it behaves like it:
each chunk is an ordinary job that settles on its own, so a run that stops
part-way credits what completed and leaves the rest untouched.
"""
from unittest.mock import AsyncMock, patch

import pytest

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import db_session
from services import mtgo_resolution_service as resolution
from services import wallet_service
from services.mtgo_resolution_service import chunk_amounts

GUILD, PLAYER, MTGO = "g1", "p1", "Someone"


@pytest.fixture(autouse=True)
def _serve_is_free(monkeypatch):
    """The busy check reaches the real client otherwise: money_gate holds its
    own get_client reference, so patching it on the resolution service does not
    cover it, and the suite would depend on a live serve."""
    async def free():
        return None
    monkeypatch.setattr(resolution, "serve_busy_reason", free)


# ---- the plan ---------------------------------------------------------------------

def test_an_order_that_fits_is_one_trade():
    assert chunk_amounts(300, 300) == [300]
    assert chunk_amounts(1, 300) == [1]


def test_a_larger_order_fills_whole_trades_first():
    """The remainder goes last because it is the piece most likely to be left
    undone, and a player is better off having been handed the big ones."""
    assert chunk_amounts(500, 300) == [300, 200]
    assert chunk_amounts(1000, 300) == [300, 300, 300, 100]


def test_an_exact_multiple_has_no_remainder_trade():
    assert chunk_amounts(600, 300) == [300, 300], "no trailing zero-tix trade"


# ---- running the order ------------------------------------------------------------

async def _fund(n):
    async with db_session() as s:
        await wallet_service.transfer_in(s, GUILD, "system:seed", PLAYER, n,
                                         "seed", notes="opening")


async def _balance():
    async with db_session() as s:
        return await wallet_service.balance_in(s, GUILD, PLAYER)


@pytest.mark.asyncio
async def test_a_large_withdraw_opens_one_trade_per_chunk(test_db, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MTGO_MAX_CARDS_PER_TRADE", "300")
    await _fund(500)
    starts, finishes = [], []

    async def fake_start(g, p, u, n, **kw):
        starts.append(n)
        return {"ok": True, "job_id": f"job-{len(starts)}"}

    async def fake_finish(job_id, g, p, n, u):
        finishes.append((job_id, n))
        return {"ok": True}

    monkeypatch.setattr(resolution, "start_withdraw", fake_start)
    monkeypatch.setattr(resolution, "finish_withdraw", fake_finish)

    res = await resolution.run_withdraw_order(GUILD, PLAYER, MTGO, 500)

    assert starts == [300, 200], "each trade asks for its own share"
    assert finishes == [("job-1", 300), ("job-2", 200)]
    assert res["delivered"] == 500
    assert res["jobs"] == ["job-1", "job-2"]


@pytest.mark.asyncio
async def test_a_chunk_that_fails_stops_the_run_and_keeps_what_landed(test_db, monkeypatch):  # noqa: F811
    """Partial is a normal outcome, not an error to unwind. The player is
    credited for the trade that completed and still holds the rest."""
    monkeypatch.setenv("MTGO_MAX_CARDS_PER_TRADE", "300")
    starts = []

    async def fake_start(g, p, u, n, **kw):
        starts.append(n)
        return {"ok": True, "job_id": f"job-{len(starts)}"}

    async def fake_finish(job_id, g, p, n, u):
        return {"ok": True} if job_id == "job-1" else {"ok": False, "error": "trade timed out"}

    monkeypatch.setattr(resolution, "start_deposit", fake_start)
    monkeypatch.setattr(resolution, "finish_deposit", fake_finish)

    res = await resolution.run_deposit_order(GUILD, PLAYER, MTGO, 800)

    assert res["credited"] == 300, "only the trade that completed counts"
    assert res["error"] == "trade timed out"
    assert starts == [300, 300], "the third trade never opens"


@pytest.mark.asyncio
async def test_a_withdraw_commits_each_chunk_at_its_own_dispatch(test_db, monkeypatch):  # noqa: F811
    """The safety property, and the reason the run needs no unwinding.

    Committing the whole order up front would strand the remainder in in-flight
    when a later trade never opens. Committing per chunk leaves it in the
    wallet, where the player can simply ask for it again.

    The real start_withdraw runs here -- mocking it would mock away the commit
    this test is about -- with only the serve stubbed out.
    """
    monkeypatch.setenv("MTGO_MAX_CARDS_PER_TRADE", "300")
    await _fund(500)
    balances = []

    async def fake_finish(job_id, g, p, n, u):
        balances.append(await _balance())
        return {"ok": True}

    monkeypatch.setattr(resolution, "finish_withdraw", fake_finish)

    with patch("services.mtgo_resolution_service.get_client") as gc:
        gc.return_value = AsyncMock(enabled=True)
        gc.return_value.withdraw_tix = AsyncMock(
            side_effect=[{"id": "job-1"}, {"id": "job-2"}])
        res = await resolution.run_withdraw_order(GUILD, PLAYER, MTGO, 500)

    assert res["delivered"] == 500
    assert balances == [200, 0], (
        "after the first trade is dispatched only its 300 has left the wallet; "
        "committing the whole order up front would read [0, 0]")


@pytest.mark.asyncio
async def test_a_busy_custodian_stops_before_any_trade(test_db, monkeypatch):  # noqa: F811
    async def busy_start(g, p, u, n, **kw):
        return {"ok": False, "error": "custodian is busy", "busy": True}

    monkeypatch.setattr(resolution, "start_deposit", busy_start)

    res = await resolution.run_deposit_order(GUILD, PLAYER, MTGO, 500)

    assert res["credited"] == 0 and res["busy"] is True
    assert res["jobs"] == [], "nothing was opened"
