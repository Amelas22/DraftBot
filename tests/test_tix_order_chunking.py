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

# The order runners report `moved` -- one key for both directions, because the
# loop is one loop. Asserting `credited`/`delivered` here is how a duplicated,
# shadowing copy of that loop stayed green while the cog KeyError'd on every
# order: these tests were exercising the dead copy.


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
    assert res["moved"] == 500
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

    assert res["moved"] == 300, "only the trade that completed counts"
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

    assert res["moved"] == 500
    assert balances == [200, 0], (
        "after the first trade is dispatched only its 300 has left the wallet; "
        "committing the whole order up front would read [0, 0]")


@pytest.mark.asyncio
async def test_a_busy_custodian_stops_before_any_trade(test_db, monkeypatch):  # noqa: F811
    async def busy_start(g, p, u, n, **kw):
        return {"ok": False, "error": "custodian is busy", "busy": True}

    monkeypatch.setattr(resolution, "start_deposit", busy_start)

    res = await resolution.run_deposit_order(GUILD, PLAYER, MTGO, 500)

    assert res["moved"] == 0 and res["busy"] is True
    assert res["jobs"] == [], "nothing was opened"


# ---- pending is not failure --------------------------------------------------------

@pytest.mark.asyncio
async def test_a_trade_the_player_has_not_accepted_yet_is_not_a_failure(test_db, monkeypatch):  # noqa: F811
    """`finish_*` returns "pending" when the poll times out -- the trade is
    still open in MTGO and the watchdog credits it whenever it completes.

    It stops the run, because the custodian takes one trade at a time. But it
    must not be reported like a failed chunk: a player told to deposit the
    remainder again would accept the original trade AND send the tix a second
    time.
    """
    monkeypatch.setenv("MTGO_MAX_CARDS_PER_TRADE", "300")

    async def fake_start(g, p, u, n, **kw):
        return {"ok": True, "job_id": "job-slow"}

    async def fake_finish(job_id, g, p, n, u):
        return {"ok": False, "outcome": "pending"}

    monkeypatch.setattr(resolution, "start_deposit", fake_start)
    monkeypatch.setattr(resolution, "finish_deposit", fake_finish)

    res = await resolution.run_deposit_order(GUILD, PLAYER, MTGO, 500)

    assert res["moved"] == 0, "nothing has credited yet"
    assert res["pending"] is True, "the caller must be able to tell this from a failure"


@pytest.mark.asyncio
async def test_a_failed_trade_is_not_reported_as_pending(test_db, monkeypatch):  # noqa: F811
    """The other half of the pair: a real failure must stay distinguishable, or
    the player is never told to try again."""
    monkeypatch.setenv("MTGO_MAX_CARDS_PER_TRADE", "300")

    async def fake_start(g, p, u, n, **kw):
        return {"ok": True, "job_id": "job-bad"}

    async def fake_finish(job_id, g, p, n, u):
        return {"ok": False, "outcome": "failed", "error": "trade declined"}

    monkeypatch.setattr(resolution, "start_deposit", fake_start)
    monkeypatch.setattr(resolution, "finish_deposit", fake_finish)

    res = await resolution.run_deposit_order(GUILD, PLAYER, MTGO, 500)

    assert res["pending"] is False
    assert res["error"] == "trade declined"


# ---- a chunked order must not adopt its own earlier trade ------------------------

@pytest.mark.asyncio
async def test_a_lost_post_does_not_adopt_the_previous_chunks_job(test_db, monkeypatch):  # noqa: F811
    """The collision chunking creates.

    `find_recent_job` matches only (type, user, qty) within two minutes, and an
    order of 600 is two trades of EXACTLY 300 seconds apart. If the second
    chunk's POST response is lost, the adoption scan would hand back the first
    chunk's completed job: booking is idempotent by job_id so nothing new is
    written, yet the second chunk's tix were already committed to in-flight.
    They would never be debited, never returned, and no job row would point at
    them -- while the player is told the whole order went out.
    """
    monkeypatch.setenv("MTGO_MAX_CARDS_PER_TRADE", "300")
    await _fund(600)
    seen_exclusions = []

    posts = []

    class FakeClient:
        enabled = True

        async def withdraw_tix(self, user, n, **kw):
            # First chunk answers normally; the second loses its response.
            posts.append(n)
            return {"id": "job-1"} if len(posts) == 1 else {"_ambiguous": True}

        async def find_recent_job(self, job_type, user, qty, max_age_s=120.0,
                                  exclude_ids=()):
            seen_exclusions.append(set(exclude_ids or ()))
            # The serve really does still list chunk 1: same type, user and qty.
            return None if "job-1" in (exclude_ids or ()) else {"id": "job-1"}

    async def fake_finish(job_id, g, p, n, u):
        return {"ok": True}

    monkeypatch.setattr(resolution, "finish_withdraw", fake_finish)
    monkeypatch.setattr(resolution, "get_client", lambda: FakeClient())

    res = await resolution.run_withdraw_order(GUILD, PLAYER, MTGO, 600)

    assert seen_exclusions, "the second chunk ran the adoption scan"
    assert "job-1" in seen_exclusions[0], \
        "the scan must be told which jobs this run already owns"
    assert res["moved"] == 300, "only the trade that really happened counts"
    assert res["jobs"] == ["job-1"], "chunk 1's job is not claimed twice"


# ---- what a stopped run says is still moving ---------------------------------------

@pytest.mark.asyncio
async def test_a_pending_chunk_reports_only_its_own_size(test_db, monkeypatch):  # noqa: F811
    """One chunk is open, not "the rest".

    The run stops at the first trade that does not complete, so everything
    after it was never dispatched and is still the player's to ask for.
    Reporting the whole remainder as in-flight tells them to wait for tix
    nobody is moving -- and the pending branch deliberately does NOT invite a
    retry, so those tix are simply lost to them.
    """
    monkeypatch.setenv("MTGO_MAX_CARDS_PER_TRADE", "300")
    sent = []

    async def fake_start(g, p, u, n, **kw):
        sent.append(n)
        return {"ok": True, "job_id": f"job-{len(sent)}"}

    async def fake_finish(job_id, g, p, n, u):
        return {"ok": True} if job_id == "job-1" else {"ok": False, "outcome": "pending"}

    monkeypatch.setattr(resolution, "start_deposit", fake_start)
    monkeypatch.setattr(resolution, "finish_deposit", fake_finish)

    res = await resolution.run_deposit_order(GUILD, PLAYER, MTGO, 900)

    assert res["moved"] == 300, "the trade that completed"
    assert res["open"] == 300, "the trade still open -- not the 600 outstanding"
    assert res["pending"] is True
    assert sent == [300, 300], "the third chunk never opened"


@pytest.mark.asyncio
async def test_a_failed_chunk_leaves_nothing_open(test_db, monkeypatch):  # noqa: F811
    """A failure is not a trade in progress: there is nothing to wait for."""
    monkeypatch.setenv("MTGO_MAX_CARDS_PER_TRADE", "300")

    async def fake_start(g, p, u, n, **kw):
        return {"ok": True, "job_id": "job-1"}

    async def fake_finish(job_id, g, p, n, u):
        return {"ok": False, "outcome": "failed", "error": "declined"}

    monkeypatch.setattr(resolution, "start_deposit", fake_start)
    monkeypatch.setattr(resolution, "finish_deposit", fake_finish)

    res = await resolution.run_deposit_order(GUILD, PLAYER, MTGO, 600)

    assert res["open"] == 0 and res["pending"] is False


# ---- an order nobody should be able to ask for --------------------------------------

@pytest.mark.asyncio
async def test_an_absurd_order_is_refused_before_it_is_planned(test_db, monkeypatch):  # noqa: F811
    """Discord accepts any integer up to 2**53, and planning materialises one
    element per trade. Without a ceiling `/wallet deposit 1000000000000`
    allocates its way through the process inside a background task, which is an
    OOM kill rather than a caught error.

    Refused in the service, not only in the command, so a second caller cannot
    reintroduce it.
    """
    monkeypatch.setenv("MTGO_MAX_CARDS_PER_TRADE", "300")
    opened = []

    async def fake_start(g, p, u, n, **kw):
        opened.append(n)
        return {"ok": True, "job_id": "job-1"}

    monkeypatch.setattr(resolution, "start_deposit", fake_start)

    res = await resolution.run_deposit_order(GUILD, PLAYER, MTGO, 10 ** 12)

    assert res["moved"] == 0 and not opened, "nothing was dispatched"
    assert "separate MTGO trades" in res["error"]
