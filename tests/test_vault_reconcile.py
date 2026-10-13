"""A tix trade is booked by what the vault shows, not only what the serve says.

2026-10-03: a 160-tix withdraw left the vault, the serve reported it failed
(its trade events had stopped), and the refund paid the player twice. Every
tix trade now records the vault's count before it goes out, and settling
compares the vault afterwards:

  withdraw  dropped by n  -> delivered, never refunded -- whatever the serve said
            unchanged     -> refunded only if the serve's failure PROVES the trade closed
  deposit   rose by n     -> credited -- whatever the serve said
            unchanged     -> failed
  anything else           -> settled on the assumption that protects the vault: a
                             withdraw delivered, a deposit not arrived; an admin is told.
                             An unreadable vault waits, then is assumed too.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import db_session
from models.mtgo_job import MtgoJob
from services import mtgo_resolution_service as resolution
from services import vault_check, wallet_service
from services.vault_check import VaultReading, classify

CLOSED = "trade closed before approval"                       # proves nothing moved
NEVER_CONFIRMED = ("the trade never confirmed as complete — treat as NOT completed")
# ^ what the serve reports while the trade can still be open on MTGO's confirm screen

GUILD, PLAYER, MTGO = "g1", "p1", "Someone"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
BASE_AT = NOW - timedelta(minutes=5)   # when the trade's baseline was read


# ---- reading the vault ----------------------------------------------------------------------

def test_the_vault_change_is_classified():
    assert classify(1000, 840, -160) == "moved"
    assert classify(1000, 1000, -160) == "none"
    assert classify(1000, 920, -160) == "other", "a partial move is not a delivery"
    assert classify(1000, 1050, +50) == "moved"


def test_a_timestamp_without_a_zone_cannot_be_placed():
    assert vault_check.parse_utc("2026-10-05T12:00:00") is None
    assert vault_check.parse_utc("2026-10-05T12:00:00.1234567Z") == NOW.replace(microsecond=123456)


def _client(jobs, readings):
    """A serve whose /jobs is `jobs` and whose /vault returns `readings` in turn."""
    client = AsyncMock()
    client.list_jobs = AsyncMock(return_value=jobs)
    client.vault = AsyncMock(side_effect=[
        {"available": True, "tix": tix, "at": at.isoformat()} for tix, at in readings])
    return client


@pytest.mark.asyncio
async def test_a_baseline_waits_out_a_cached_reading_from_before_the_last_trade(monkeypatch):
    """/vault caches; a trade the serve missed doesn't refresh it. A reading from
    before the last job settled would be a baseline from before that trade."""
    monkeypatch.setattr(vault_check.asyncio, "sleep", AsyncMock())
    last_done = NOW - timedelta(seconds=30)
    client = _client([{"state": "done", "finishedAt": last_done.isoformat()}],
                     [(1000, last_done - timedelta(seconds=5)),   # stale: before it finished
                      (840, last_done + timedelta(seconds=20)),   # ended in time, began who knows
                      (840, last_done + timedelta(seconds=81))])  # the next scan: began in time

    reading = await vault_check.baseline(client)
    assert reading.tix == 840 and reading.scan_from == last_done + timedelta(seconds=20)


@pytest.mark.asyncio
async def test_no_fresh_reading_means_no_baseline(monkeypatch):
    monkeypatch.setattr(vault_check, "_WAIT_FOR_FRESH_S", 0)
    last_done = NOW
    client = _client([{"state": "done", "finishedAt": last_done.isoformat()}], [(1000, last_done)])

    assert await vault_check.baseline(client) is None


@pytest.mark.asyncio
async def test_an_unreadable_job_list_means_no_baseline():
    client = _client(None, [])
    assert await vault_check.baseline(client) is None


@pytest.mark.asyncio
async def test_a_job_that_doesnt_say_when_it_finished_cant_be_checked():
    assert await vault_check.after(_client([], []), {"state": "failed"}, BASE_AT) is None


# ---- settling ---------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _serve_is_free(monkeypatch):
    async def free():
        return None
    monkeypatch.setattr(resolution, "serve_busy_reason", free)


async def _fund(n):
    async with db_session() as s:
        await wallet_service.transfer_in(s, GUILD, "system:seed", PLAYER, n, "seed", notes="opening")


async def _balances():
    async with db_session() as s:
        return (await wallet_service.balance_in(s, GUILD, PLAYER),
                await wallet_service.balance_in(s, GUILD, wallet_service.SYSTEM_IN_FLIGHT))


async def _status(job_id):
    async with db_session() as s:
        row = await s.get(MtgoJob, job_id)
        return row.status if row else None


async def _assumed(job_id):
    async with db_session() as s:
        row = await s.get(MtgoJob, job_id)
        return row.assumed if row else None


async def _withdraw_out(job_id, n=100, baseline=1000):
    """A withdraw as start_withdraw leaves it: n committed to in-flight, job pending."""
    await _fund(n)
    await wallet_service.pay(GUILD, PLAYER, wallet_service.SYSTEM_IN_FLIGHT, n,
                             source=f"wd:{job_id}", notes="commit")
    reading = VaultReading(baseline, NOW) if baseline is not None else None
    await resolution._record_job(job_id, "withdraw", GUILD, PLAYER, MTGO, n, reading)


def _settle_with(serve_state, vault_after, detail="x"):
    """The serve reports `serve_state` (with `detail`); the vault then reads `vault_after`
    (None: unreadable). A `serve_state` of "missing" is the serve answering 404."""
    client = AsyncMock(enabled=True)
    client.get_job = AsyncMock(return_value=(
        {"_missing": True} if serve_state == "missing" else
        {"state": serve_state, "detail": detail,
         "finishedAt": datetime.now(timezone.utc).isoformat()}))
    reading = VaultReading(vault_after, NOW + timedelta(minutes=1)) if vault_after is not None else None
    return (patch("services.mtgo_resolution_service.get_client", return_value=client),
            patch.object(resolution.vault_check, "after", AsyncMock(return_value=reading)))


async def _finish_withdraw(job_id, serve_state, vault_after, n=100, detail="x"):
    a, b = _settle_with(serve_state, vault_after, detail)
    with a, b:
        return await resolution.finish_withdraw(job_id, GUILD, PLAYER, n, MTGO, timeout_s=1)


async def _finish_deposit(job_id, serve_state, vault_after, n=50, detail="x"):
    a, b = _settle_with(serve_state, vault_after, detail)
    with a, b:
        return await resolution.finish_deposit(job_id, GUILD, PLAYER, n, MTGO, timeout_s=1)


@pytest.mark.asyncio
async def test_a_withdraw_reported_failed_that_left_the_vault_is_not_refunded(test_db):  # noqa: F811
    """The 2026-10-03 case, booked right."""
    await _withdraw_out("w1")
    res = await _finish_withdraw("w1", "failed", 900)

    assert res["outcome"] == "done"
    assert await _balances() == (0, 0), "delivered: debited, not refunded"


@pytest.mark.asyncio
async def test_a_withdraw_proven_closed_with_the_vault_unchanged_is_refunded(test_db):  # noqa: F811
    await _withdraw_out("w2")
    res = await _finish_withdraw("w2", "failed", 1000, detail=CLOSED)

    assert res["outcome"] == "failed"
    assert await _balances() == (100, 0)
    assert await _assumed("w2") is None


@pytest.mark.asyncio
async def test_a_withdraw_still_open_on_the_confirm_screen_is_never_refunded(test_db):  # noqa: F811
    """The double-pay that survived the vault check: the partner sits on MTGO's final
    confirm, the serve gives up and says failed with the trade still open, the vault
    reads unchanged -- and the player confirms afterwards. Unchanged + "never confirmed"
    is not proof, so it is booked as delivered."""
    await _withdraw_out("w2b")
    res = await _finish_withdraw("w2b", "failed", 1000, detail=NEVER_CONFIRMED)

    assert res["outcome"] == resolution.ASSUMED and res["assumed"]
    assert await _balances() == (0, 0), "debited as delivered, not refunded"
    assert await _status("w2b") == "done" and await _assumed("w2b")


@pytest.mark.asyncio
async def test_a_done_withdraw_the_vault_doesnt_show_is_assumed_delivered(test_db):  # noqa: F811
    """Refunding a 'done' trade could pay twice; debiting one that never left only
    shorts the player until an admin returns it."""
    await _withdraw_out("w3")
    res = await _finish_withdraw("w3", "done", 1000)

    assert res["outcome"] == resolution.ASSUMED
    assert await _balances() == (0, 0)
    assert await _status("w3") == "done" and await _assumed("w3")


@pytest.mark.asyncio
async def test_a_partial_change_is_assumed_delivered(test_db):  # noqa: F811
    await _withdraw_out("w4")
    res = await _finish_withdraw("w4", "failed", 950, detail=CLOSED)

    assert res["outcome"] == resolution.ASSUMED
    assert await _balances() == (0, 0)


@pytest.mark.asyncio
async def test_a_withdraw_the_serve_forgot_is_assumed_delivered(test_db):  # noqa: F811
    """A 404 is the serve restarting and losing its job list -- not the trade failing."""
    await _withdraw_out("w4b")
    res = await _finish_withdraw("w4b", "missing", None)

    assert res["outcome"] == resolution.ASSUMED
    assert await _balances() == (0, 0)


@pytest.mark.asyncio
async def test_an_unreadable_vault_leaves_the_job_for_later(test_db):  # noqa: F811
    """Not a verdict: the watchdog re-polls pending jobs and checks again. Reported as
    'confirming', not 'pending' -- the trade is over, there is nothing left to accept."""
    await _withdraw_out("w5")
    res = await _finish_withdraw("w5", "failed", None)

    assert res["outcome"] == resolution.CONFIRMING
    assert await _status("w5") == "pending"
    assert await _balances() == (0, 100)


@pytest.mark.asyncio
async def test_a_job_from_before_the_check_is_refunded_only_on_proof(test_db):  # noqa: F811
    await _withdraw_out("w6", baseline=None)
    res = await _finish_withdraw("w6", "failed", None, detail=CLOSED)

    assert res["outcome"] == "failed"
    assert await _balances() == (100, 0)


@pytest.mark.asyncio
async def test_a_job_from_before_the_check_without_proof_is_assumed_delivered(test_db):  # noqa: F811
    await _withdraw_out("w6b", baseline=None)
    res = await _finish_withdraw("w6b", "failed", None, detail=NEVER_CONFIRMED)

    assert res["outcome"] == resolution.ASSUMED
    assert await _balances() == (0, 0)


@pytest.mark.asyncio
async def test_a_deposit_is_credited_only_when_the_vault_rose(test_db):  # noqa: F811
    await resolution._record_job("d1", "deposit", GUILD, PLAYER, MTGO, 50, VaultReading(1000, NOW))
    res = await _finish_deposit("d1", "done", 1050)

    assert res["outcome"] == "done" and await _balances() == (50, 0)


@pytest.mark.asyncio
async def test_a_done_deposit_the_vault_doesnt_show_is_assumed_not_arrived(test_db):  # noqa: F811
    await resolution._record_job("d2", "deposit", GUILD, PLAYER, MTGO, 50, VaultReading(1000, NOW))
    res = await _finish_deposit("d2", "done", 1000)

    assert res["outcome"] == resolution.ASSUMED and await _balances() == (0, 0)
    assert await _status("d2") == "failed" and await _assumed("d2")


@pytest.mark.asyncio
async def test_a_deposit_the_serve_forgot_is_not_credited(test_db):  # noqa: F811
    await resolution._record_job("d2b", "deposit", GUILD, PLAYER, MTGO, 50, VaultReading(1000, NOW))
    res = await _finish_deposit("d2b", "missing", None)

    assert res["outcome"] == resolution.ASSUMED and await _balances() == (0, 0)


@pytest.mark.asyncio
async def test_a_deposit_reported_failed_that_arrived_is_credited(test_db):  # noqa: F811
    await resolution._record_job("d3", "deposit", GUILD, PLAYER, MTGO, 50, VaultReading(1000, NOW))
    res = await _finish_deposit("d3", "failed", 1050)

    assert res["outcome"] == "done" and await _balances() == (50, 0)


@pytest.mark.asyncio
async def test_a_deposit_proven_closed_is_failed(test_db):  # noqa: F811
    await resolution._record_job("d4", "deposit", GUILD, PLAYER, MTGO, 50, VaultReading(1000, NOW))
    res = await _finish_deposit("d4", "failed", 1000, detail=CLOSED)

    assert res["outcome"] == "failed" and await _balances() == (0, 0)
    assert await _assumed("d4") is None


@pytest.mark.asyncio
async def test_a_deposit_that_may_still_be_open_is_not_credited_and_flagged(test_db):  # noqa: F811
    """Its tix can still arrive once the player confirms; nothing is credited on a guess,
    and an admin is told to watch for them."""
    await resolution._record_job("d4b", "deposit", GUILD, PLAYER, MTGO, 50, VaultReading(1000, NOW))
    res = await _finish_deposit("d4b", "failed", 1000, detail=NEVER_CONFIRMED)

    assert res["outcome"] == resolution.ASSUMED and await _balances() == (0, 0)
    assert await _assumed("d4b")


# ---- starting -----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_baseline_means_no_trade_and_nothing_charged(test_db, monkeypatch):  # noqa: F811
    await _fund(100)
    monkeypatch.setattr(resolution.vault_check, "baseline", AsyncMock(return_value=None))
    client = AsyncMock(enabled=True)
    with patch("services.mtgo_resolution_service.get_client", return_value=client):
        res = await resolution.start_withdraw(GUILD, PLAYER, MTGO, 100)

    assert not res["ok"] and res["busy"]
    client.withdraw_tix.assert_not_awaited()
    assert await _balances() == (100, 0)


@pytest.mark.asyncio
async def test_a_started_trade_records_its_baseline(test_db, monkeypatch):  # noqa: F811
    await _fund(100)
    monkeypatch.setattr(resolution.vault_check, "baseline",
                        AsyncMock(return_value=VaultReading(1234, NOW)))
    client = AsyncMock(enabled=True)
    client.withdraw_tix = AsyncMock(return_value={"id": "w9"})
    with patch("services.mtgo_resolution_service.get_client", return_value=client):
        await resolution.start_withdraw(GUILD, PLAYER, MTGO, 100)

    async with db_session() as s:
        row = await s.get(MtgoJob, "w9")
    assert row.vault_before == 1234


@pytest.mark.asyncio
async def test_an_assumed_chunk_is_reported_as_assumed(test_db, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MTGO_MAX_CARDS_PER_TRADE", "300")

    async def start(g, p, u, n, **kw):
        return {"ok": True, "job_id": "only"}

    async def finish(job_id, g, p, n, u):
        return {"ok": False, "outcome": resolution.ASSUMED, "error": "assumed"}

    monkeypatch.setattr(resolution, "start_withdraw", start)
    monkeypatch.setattr(resolution, "finish_withdraw", finish)
    res = await resolution.run_withdraw_order(GUILD, PLAYER, MTGO, 200)

    assert res["assumed"] and not res["pending"] and res["open"] == 200


# ---- the reading's window -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_reading_after_the_next_trade_began_measures_both_and_is_refused(monkeypatch):
    """A 100-tix withdraw and a 100-tix deposit in one window net to zero, which would
    read as "nothing left" and refund the withdraw."""
    monkeypatch.setattr(vault_check.asyncio, "sleep", AsyncMock())
    finished = NOW
    client = _client([{"id": "next", "state": "running",
                       "startedAt": (finished + timedelta(seconds=20)).isoformat()}],
                     [(1000, finished + timedelta(seconds=40))])

    assert await vault_check.after(client, {"id": "ours", "finishedAt": finished.isoformat()}, BASE_AT) is None


@pytest.mark.asyncio
async def test_a_reading_inside_the_window_counts(monkeypatch):
    monkeypatch.setattr(vault_check.asyncio, "sleep", AsyncMock())
    finished = NOW
    client = _client([{"id": "next", "state": "running",
                       "startedAt": (finished + timedelta(minutes=5)).isoformat()}],
                     [(900, finished + timedelta(seconds=20)), (900, finished + timedelta(seconds=81))])

    assert (await vault_check.after(client, {"id": "ours", "finishedAt": finished.isoformat()}, BASE_AT)).tix == 900


@pytest.mark.asyncio
async def test_only_a_scan_that_began_inside_the_window_counts(monkeypatch):
    """`at` is when a scan FINISHED. A slow scan stamped just after the trade settled can
    still have counted the tix before it, so the first in-window reading is not used."""
    monkeypatch.setattr(vault_check, "_WAIT_FOR_FRESH_S", 0)
    finished = NOW
    client = _client([], [(1000, finished + timedelta(seconds=20))])

    assert await vault_check.after(client, {"id": "ours", "finishedAt": finished.isoformat()}, BASE_AT) is None


@pytest.mark.asyncio
async def test_no_baseline_while_another_trade_is_moving_the_vault():
    client = _client([{"state": "running"}], [(1000, NOW)])
    assert await vault_check.baseline(client) is None


@pytest.mark.asyncio
async def test_a_finished_job_without_a_finish_time_fails_the_baseline_closed():
    """Its trade could have moved the vault at any time, so no reading can be shown to
    come after it."""
    client = _client([{"state": "done", "finishedAt": None}], [(1000, NOW)])
    assert await vault_check.baseline(client) is None


# ---- one settler per job ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_two_settlers_reading_different_vaults_book_once(test_db):  # noqa: F811
    """The command poller and the watchdog can both finish one job. The first claim
    decides; the second books nothing and reports the first's answer."""
    await _withdraw_out("w7")
    first = await _finish_withdraw("w7", "failed", 900)     # vault dropped: delivered
    second = await _finish_withdraw("w7", "failed", 1000,   # a stale view: would refund
                                    detail=CLOSED)

    assert first["outcome"] == "done" and second["outcome"] == "done"
    assert await _balances() == (0, 0), "debited once, never refunded"


@pytest.mark.asyncio
async def test_an_interrupted_settlement_is_finished_by_the_watchdog(test_db):  # noqa: F811
    from sqlalchemy import update
    await _withdraw_out("w8")
    async with db_session() as s:
        await s.execute(update(MtgoJob).where(MtgoJob.job_id == "w8").values(
            status="settle-done", resolved_at=datetime.now() - timedelta(minutes=10)))

    assert await resolution._finish_settlements() == 1
    assert await _status("w8") == "done" and await _balances() == (0, 0)


# ---- a lost POST is assumed delivered, never refunded on a guess --------------------------------------

@pytest.mark.asyncio
async def test_an_ambiguous_withdraw_post_with_no_job_found_is_assumed_delivered(test_db, monkeypatch):  # noqa: F811
    await _fund(100)
    monkeypatch.setattr(resolution.vault_check, "baseline",
                        AsyncMock(return_value=VaultReading(1000, NOW)))
    client = AsyncMock(enabled=True)
    client.withdraw_tix = AsyncMock(return_value={"_ambiguous": True})
    client.find_recent_job = AsyncMock(return_value=None)
    with patch("services.mtgo_resolution_service.get_client", return_value=client), \
         patch("services.mtgo_resolution_service.asyncio.sleep", AsyncMock()):
        res = await resolution.start_withdraw(GUILD, PLAYER, MTGO, 100)

    assert not res["ok"] and res["assumed"]
    assert await _balances() == (0, 0), "debited as delivered, not refunded"
    async with db_session() as s:
        held = [r for r in (await s.execute(MtgoJob.__table__.select())).all()
                if r.assumed]
    assert len(held) == 1 and held[0].job_id.startswith("send-") and held[0].vault_before == 1000
    assert held[0].status == "done"


@pytest.mark.asyncio
async def test_a_definite_refusal_is_refunded(test_db, monkeypatch):  # noqa: F811
    await _fund(100)
    monkeypatch.setattr(resolution.vault_check, "baseline",
                        AsyncMock(return_value=VaultReading(1000, NOW)))
    client = AsyncMock(enabled=True)
    client.withdraw_tix = AsyncMock(return_value=None)
    with patch("services.mtgo_resolution_service.get_client", return_value=client):
        res = await resolution.start_withdraw(GUILD, PLAYER, MTGO, 100)

    assert not res["ok"] and not res.get("assumed")
    assert await _balances() == (100, 0)


def _post_reply(monkeypatch, client, code, text):
    class _Resp:
        status = code
        async def text(self): return text
        async def json(self):
            import json as _j
            return _j.loads(text)
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class _Session:
        def request(self, *a, **kw): return _Resp()

    monkeypatch.setattr(client, "_get_session", lambda: _Session())


@pytest.mark.asyncio
@pytest.mark.parametrize("code,text", [(500, "boom"), (202, ""), (202, "{}")])
async def test_a_reply_that_doesnt_name_a_job_is_ambiguous_not_a_refusal(monkeypatch, code, text):
    """A 5xx can come after the job was queued; a 2xx means it WAS accepted."""
    from services.mtgo_tradebot_client import MtgoTradeBotClient
    client = MtgoTradeBotClient(url="http://serve", token="t")
    _post_reply(monkeypatch, client, code, text)

    assert await client.withdraw_tix(MTGO, 100) == {"_ambiguous": True}


@pytest.mark.asyncio
async def test_a_refusal_stays_definite(monkeypatch):
    from services.mtgo_tradebot_client import MtgoTradeBotClient
    client = MtgoTradeBotClient(url="http://serve", token="t")
    _post_reply(monkeypatch, client, 409, '{"error": "busy"}')

    assert await client.withdraw_tix(MTGO, 100) is None


# ---- round 3: the window is rechecked, adoption is strict, a held start is reported held ---------

@pytest.mark.asyncio
async def test_a_trade_that_began_while_waiting_for_the_reading_voids_it(monkeypatch):
    """The job list is read before waiting out the vault cache. A job that started during
    that wait -- before the reading was taken -- is inside the reading."""
    monkeypatch.setattr(vault_check.asyncio, "sleep", AsyncMock())
    finished = NOW
    client = _client(None, [(1000, finished + timedelta(seconds=40)),
                            (1000, finished + timedelta(seconds=101))])
    client.list_jobs = AsyncMock(side_effect=[
        [],
        [{"id": "next", "state": "running", "startedAt": (finished + timedelta(seconds=20)).isoformat()}],
    ])

    assert await vault_check.after(client, {"id": "ours", "finishedAt": finished.isoformat()}, BASE_AT) is None


@pytest.mark.asyncio
async def test_a_trade_that_began_after_the_reading_leaves_it_standing(monkeypatch):
    monkeypatch.setattr(vault_check.asyncio, "sleep", AsyncMock())
    finished = NOW
    client = _client(None, [(900, finished + timedelta(seconds=20)),
                            (900, finished + timedelta(seconds=81))])
    client.list_jobs = AsyncMock(side_effect=[
        [],
        [{"id": "next", "state": "running", "startedAt": (finished + timedelta(seconds=90)).isoformat()}],
    ])

    assert (await vault_check.after(client, {"id": "ours", "finishedAt": finished.isoformat()}, BASE_AT)).tix == 900


@pytest.mark.asyncio
async def test_a_running_job_with_no_start_time_voids_the_reading(monkeypatch):
    monkeypatch.setattr(vault_check.asyncio, "sleep", AsyncMock())
    client = _client(None, [(900, NOW + timedelta(seconds=20)), (900, NOW + timedelta(seconds=81))])
    client.list_jobs = AsyncMock(side_effect=[[], [{"id": "next", "state": "running"}]])

    assert await vault_check.after(client, {"id": "ours", "finishedAt": NOW.isoformat()}, BASE_AT) is None


def _serve_listing(jobs):
    from services.mtgo_tradebot_client import MtgoTradeBotClient
    client = MtgoTradeBotClient.__new__(MtgoTradeBotClient)
    client._list_jobs = AsyncMock(return_value=jobs)
    return client


def _tix_job(job_id, created, state="done", user=MTGO, qty=100):
    return {"id": job_id, "type": "request", "state": state, "user": user,
            "createdAt": created.isoformat(),
            "give": [{"id": 1, "name": "Event Ticket", "qty": qty}]}


@pytest.mark.asyncio
async def test_adoption_never_takes_a_job_from_before_the_post():
    """The same player, the same amount, a minute apart: the earlier job matches on
    everything but when it was made."""
    sent = datetime.now(timezone.utc)
    client = _serve_listing([_tix_job("earlier", sent - timedelta(seconds=60))])
    assert await client.find_recent_job("request", MTGO, 100, not_before=sent) is None

    client = _serve_listing([_tix_job("ours", sent + timedelta(seconds=1))])
    assert (await client.find_recent_job("request", MTGO, 100, not_before=sent))["id"] == "ours"


@pytest.mark.asyncio
async def test_strict_adoption_refuses_a_job_it_cant_place_in_time():
    sent = datetime.now(timezone.utc)
    job = _tix_job("ours", sent)
    job["createdAt"] = "whenever"
    assert await _serve_listing([job]).find_recent_job("request", MTGO, 100, not_before=sent) is None


@pytest.mark.asyncio
async def test_a_failed_job_this_post_made_is_adopted_so_the_vault_can_settle_it():
    sent = datetime.now(timezone.utc)
    client = _serve_listing([_tix_job("ours", sent + timedelta(seconds=1), state="failed")])
    assert (await client.find_recent_job("request", MTGO, 100, not_before=sent))["id"] == "ours"


@pytest.mark.asyncio
async def test_a_lost_post_never_adopts_a_job_already_booked(test_db, monkeypatch):  # noqa: F811
    await resolution._record_job("booked", "withdraw", GUILD, PLAYER, MTGO.upper(), 100)
    await _fund(100)
    monkeypatch.setattr(resolution.vault_check, "baseline",
                        AsyncMock(return_value=VaultReading(1000, NOW)))
    client = AsyncMock(enabled=True)
    client.withdraw_tix = AsyncMock(return_value={"_ambiguous": True})
    client.find_recent_job = AsyncMock(return_value=None)
    with patch("services.mtgo_resolution_service.get_client", return_value=client), \
         patch("services.mtgo_resolution_service.asyncio.sleep", AsyncMock()):
        await resolution.start_withdraw(GUILD, PLAYER, MTGO, 100)

    kw = client.find_recent_job.await_args.kwargs
    assert "booked" in kw["exclude_ids"], "matched case-insensitively, like adoption"
    assert kw["not_before"] is not None and kw["not_before"] <= datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_an_assumed_start_is_reported_assumed_not_failed(test_db, monkeypatch):  # noqa: F811
    """A lost POST is booked as delivered. Reporting it as a plain failure would tell the
    player the tix are back in the wallet and to try again."""
    monkeypatch.setenv("MTGO_MAX_CARDS_PER_TRADE", "300")

    async def start(g, p, u, n, **kw):
        return {"ok": False, "assumed": True, "outcome": resolution.ASSUMED,
                "error": "assumed", "job_id": "lost-abc"}

    monkeypatch.setattr(resolution, "start_withdraw", start)
    res = await resolution.run_withdraw_order(GUILD, PLAYER, MTGO, 200)

    assert res["assumed"] and res["open"] == 200 and res["jobs"] == ["lost-abc"]


# ---- round 4: nothing else may trade between the baseline and the after-reading ----------------

@pytest.mark.asyncio
async def test_a_trade_that_ran_while_the_baseline_was_read_voids_it(monkeypatch):
    """The job list is read before waiting out the cache; a trade that started during
    the wait would sit between the baseline and the trade it is for."""
    monkeypatch.setattr(vault_check.asyncio, "sleep", AsyncMock())
    client = _client(None, [(1000, NOW), (1000, NOW + timedelta(seconds=61))])
    client.list_jobs = AsyncMock(side_effect=[
        [],
        [{"id": "x", "state": "running", "startedAt": (NOW - timedelta(seconds=10)).isoformat()}],
    ])
    assert await vault_check.baseline(client) is None


@pytest.mark.asyncio
async def test_a_trade_that_finished_too_close_to_the_baseline_voids_it(monkeypatch):
    monkeypatch.setattr(vault_check.asyncio, "sleep", AsyncMock())
    client = _client(None, [(1000, NOW), (1000, NOW + timedelta(seconds=61))])
    client.list_jobs = AsyncMock(side_effect=[
        [],
        # settled 12s after the counted scan can have begun: inside the settle lag
        [{"id": "x", "state": "done", "finishedAt": (NOW - timedelta(seconds=3)).isoformat()}],
    ])
    assert await vault_check.baseline(client) is None


@pytest.mark.asyncio
async def test_a_baseline_with_nothing_trading_stands(monkeypatch):
    monkeypatch.setattr(vault_check.asyncio, "sleep", AsyncMock())
    client = _client([{"id": "x", "state": "done",
                       "finishedAt": (NOW - timedelta(minutes=2)).isoformat()}],
                     [(1000, NOW), (1000, NOW + timedelta(seconds=61))])
    assert (await vault_check.baseline(client)).tix == 1000


@pytest.mark.asyncio
async def test_a_finished_job_with_no_start_inside_the_window_voids_the_reading(monkeypatch):
    """A done job that can't say when it started may have traded anywhere in the window --
    a 100-tix deposit there would cancel a 100-tix withdraw to zero."""
    monkeypatch.setattr(vault_check.asyncio, "sleep", AsyncMock())
    other = {"id": "x", "state": "done", "finishedAt": (NOW + timedelta(seconds=30)).isoformat()}
    client = _client([other], [(1000, NOW + timedelta(seconds=40))])
    assert await vault_check.after(client, {"id": "ours", "finishedAt": NOW.isoformat()}, BASE_AT) is None


@pytest.mark.asyncio
async def test_a_trade_between_the_baseline_and_ours_voids_the_reading(monkeypatch):
    monkeypatch.setattr(vault_check.asyncio, "sleep", AsyncMock())
    other = {"id": "x", "state": "done", "startedAt": (BASE_AT + timedelta(seconds=30)).isoformat(),
             "finishedAt": (BASE_AT + timedelta(seconds=60)).isoformat()}
    client = _client([other], [(900, NOW + timedelta(seconds=20))])
    assert await vault_check.after(client, {"id": "ours", "finishedAt": NOW.isoformat()}, BASE_AT) is None


@pytest.mark.asyncio
async def test_a_trade_over_before_the_baseline_does_not_void_the_reading(monkeypatch):
    monkeypatch.setattr(vault_check.asyncio, "sleep", AsyncMock())
    other = {"id": "x", "state": "done", "finishedAt": (BASE_AT - timedelta(minutes=1)).isoformat()}
    client = _client([other], [(900, NOW + timedelta(seconds=20)), (900, NOW + timedelta(seconds=81))])
    assert (await vault_check.after(client, {"id": "ours", "finishedAt": NOW.isoformat()}, BASE_AT)).tix == 900


@pytest.mark.asyncio
async def test_a_reading_that_never_comes_is_assumed_not_confirmed_forever(test_db):  # noqa: F811
    """A window another trade broke stays broken; the player must not wait on it forever."""
    await _withdraw_out("w20")
    client = AsyncMock(enabled=True)
    long_ago = datetime.now(timezone.utc) - timedelta(minutes=20)
    client.get_job = AsyncMock(return_value={"state": "failed", "detail": "x",
                                             "finishedAt": long_ago.isoformat()})
    with patch("services.mtgo_resolution_service.get_client", return_value=client), \
         patch.object(resolution.vault_check, "after", AsyncMock(return_value=None)):
        await resolution.finish_withdraw("w20", GUILD, PLAYER, 100, MTGO)
    assert await _status("w20") == "done" and await _assumed("w20")
    assert await _balances() == (0, 0), "assumed delivered, not refunded"


@pytest.mark.asyncio
async def test_a_job_with_no_finish_time_is_still_settled(test_db):  # noqa: F811
    """No finishedAt means no reading ever; it must not wait forever either."""
    await _withdraw_out("w21", baseline=None)
    async with db_session() as s:
        row = await s.get(MtgoJob, "w21")
        row.vault_before = 1000
        row.vault_before_at = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None)
        await s.commit()
    client = AsyncMock(enabled=True)
    client.get_job = AsyncMock(return_value={"state": "failed", "detail": "x"})
    with patch("services.mtgo_resolution_service.get_client", return_value=client):
        await resolution.finish_withdraw("w21", GUILD, PLAYER, 100, MTGO)
    assert await _status("w21") == "done" and await _assumed("w21")
    assert await _balances() == (0, 0)


# ---- what proves a trade closed -----------------------------------------------------------------

def test_only_a_failure_that_proves_the_trade_closed_counts():
    assert resolution.proves_closed(CLOSED)
    assert resolution.proves_closed("the trade closed as 'Cancelled' — treat as NOT completed")
    assert resolution.proves_closed("cancelled before it started")
    assert not resolution.proves_closed(NEVER_CONFIRMED), "can still be open on the confirm screen"
    assert not resolution.proves_closed("the trade wasn't completed in time")
    assert not resolution.proves_closed("x")
    assert not resolution.proves_closed(None)
    assert not resolution.proves_closed("")


# ---- an admin is told about every assumption, and a failed DM is retried ------------------------

async def _assumed_row(job_id):
    await _withdraw_out(job_id)
    await _finish_withdraw(job_id, "failed", 1000, detail=NEVER_CONFIRMED)


async def _alerted(job_id):
    async with db_session() as s:
        row = await s.get(MtgoJob, job_id)
        return row.alerted_at


@pytest.mark.asyncio
async def test_an_assumption_is_dmed_and_marked_only_once_it_went(test_db, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SERVE_ALERT_DISCORD_ID", "42")
    sent = AsyncMock(side_effect=[False, True])     # the first DM can't be delivered
    monkeypatch.setattr("notification_service.send_dm", sent)
    monkeypatch.setattr("bot_registry.get_bot", lambda: object())

    await _assumed_row("a1")                         # alerts at once: the DM fails
    assert await _alerted("a1") is None, "an alert that didn't go out isn't recorded as sent"

    assert await resolution.alert_assumed() == 1     # the watchdog's retry gets through
    assert await _alerted("a1") is not None
    assert sent.await_args.args[1] == "42" and "a1" in sent.await_args.args[2]

    assert await resolution.alert_assumed() == 0, "told once, not every pass"


@pytest.mark.asyncio
async def test_no_alert_target_means_nothing_is_marked_alerted(test_db, monkeypatch):  # noqa: F811
    monkeypatch.delenv("SERVE_ALERT_DISCORD_ID", raising=False)
    await _assumed_row("a2")

    assert await resolution.alert_assumed() == 0
    assert await _alerted("a2") is None, "kept for when a target is set"


# ---- a trade is recorded before anything moves ----------------------------------------------------

async def _rows():
    async with db_session() as s:
        return {r.job_id: r for r in (await s.execute(MtgoJob.__table__.select())).all()}


async def _age(job_id, seconds):
    async with db_session() as s:
        row = await s.get(MtgoJob, job_id)
        row.created_at = datetime.now() - timedelta(seconds=seconds)


@pytest.mark.asyncio
async def test_a_sent_trade_binds_its_intent_to_the_job(test_db, monkeypatch):  # noqa: F811
    await _fund(100)
    monkeypatch.setattr(resolution.vault_check, "baseline",
                        AsyncMock(return_value=VaultReading(1000, NOW)))
    client = AsyncMock(enabled=True)
    client.withdraw_tix = AsyncMock(return_value={"id": "w30"})
    with patch("services.mtgo_resolution_service.get_client", return_value=client):
        await resolution.start_withdraw(GUILD, PLAYER, MTGO, 100)

    rows = await _rows()
    assert list(rows) == ["w30"] and rows["w30"].status == "pending"


@pytest.mark.asyncio
async def test_a_refused_withdraw_closes_its_intent_and_refunds(test_db, monkeypatch):  # noqa: F811
    await _fund(100)
    monkeypatch.setattr(resolution.vault_check, "baseline",
                        AsyncMock(return_value=VaultReading(1000, NOW)))
    client = AsyncMock(enabled=True)
    client.withdraw_tix = AsyncMock(return_value=None)
    with patch("services.mtgo_resolution_service.get_client", return_value=client):
        await resolution.start_withdraw(GUILD, PLAYER, MTGO, 100)

    (row,) = (await _rows()).values()
    assert row.status == "failed" and row.assumed is None
    assert await _balances() == (100, 0)


@pytest.mark.asyncio
async def test_a_refused_withdraw_whose_refund_fails_is_refunded_by_the_watchdog(test_db, monkeypatch):  # noqa: F811
    """Closed as failed before the refund landed, nothing would ever give the tix back."""
    from sqlalchemy import update
    await _fund(100)
    monkeypatch.setattr(resolution.vault_check, "baseline",
                        AsyncMock(return_value=VaultReading(1000, NOW)))
    client = AsyncMock(enabled=True)
    client.withdraw_tix = AsyncMock(return_value=None)
    with patch("services.mtgo_resolution_service.get_client", return_value=client), \
            patch.object(resolution, "_return_in_flight", AsyncMock(side_effect=RuntimeError)):
        with pytest.raises(RuntimeError):
            await resolution.start_withdraw(GUILD, PLAYER, MTGO, 100)

    (row,) = (await _rows()).values()
    assert row.status == "settle-failed"
    assert await _balances() == (0, 100)
    async with db_session() as s:
        await s.execute(update(MtgoJob).where(MtgoJob.job_id == row.job_id).values(
            resolved_at=datetime.now() - timedelta(hours=1)))

    assert await resolution._finish_settlements() == 1
    assert await _status(row.job_id) == "failed"
    assert await _balances() == (100, 0)
    assert await resolution._finish_settlements() == 0
    assert await _balances() == (100, 0), "refunded once"


async def _crashed_withdraw(committed):
    """A withdraw whose process died after recording it -- before or after its commit."""
    await _fund(100)
    intent = await resolution._record_intent("withdraw", GUILD, PLAYER, MTGO, 100,
                                             VaultReading(1000, NOW))
    if committed:
        await wallet_service.pay(GUILD, PLAYER, wallet_service.SYSTEM_IN_FLIGHT, 100,
                                 source=f"wd:{intent}", notes="commit")
    await _age(intent, resolution._INTENT_ABANDONED_S + 60)
    return intent


@pytest.mark.asyncio
async def test_a_crash_after_the_commit_is_assumed_delivered(test_db, monkeypatch):  # noqa: F811
    """The POST may have gone out and the trade fired: refunding could pay twice."""
    monkeypatch.delenv("SERVE_ALERT_DISCORD_ID", raising=False)
    intent = await _crashed_withdraw(committed=True)

    assert await resolution.recover_unsent_intents() == 1
    assert await _status(intent) == "done" and await _assumed(intent)
    assert await _balances() == (0, 0)


@pytest.mark.asyncio
async def test_a_crash_before_the_commit_moved_nothing(test_db):  # noqa: F811
    """The commit precedes the POST, so with no commit there can be no trade."""
    intent = await _crashed_withdraw(committed=False)

    await resolution.recover_unsent_intents()
    assert await _status(intent) == "failed" and await _assumed(intent) is None
    assert await _balances() == (100, 0)


@pytest.mark.asyncio
async def test_a_crashed_deposit_is_not_credited(test_db, monkeypatch):  # noqa: F811
    monkeypatch.delenv("SERVE_ALERT_DISCORD_ID", raising=False)
    intent = await resolution._record_intent("deposit", GUILD, PLAYER, MTGO, 50,
                                             VaultReading(1000, NOW))
    await _age(intent, resolution._INTENT_ABANDONED_S + 60)

    await resolution.recover_unsent_intents()
    assert await _status(intent) == "failed" and await _assumed(intent)
    assert await _balances() == (0, 0)


@pytest.mark.asyncio
async def test_an_intent_still_inside_its_post_is_left_alone(test_db):  # noqa: F811
    """A slow POST is still being waited on by its sender."""
    intent = await resolution._record_intent("withdraw", GUILD, PLAYER, MTGO, 100,
                                             VaultReading(1000, NOW))

    assert await resolution.recover_unsent_intents() == 0
    assert await _status(intent) == "sending"


@pytest.mark.asyncio
async def test_a_booking_that_keeps_failing_is_reported_once(test_db, monkeypatch):  # noqa: F811
    """Retrying in silence for ever is how a stuck settlement went unnoticed."""
    from sqlalchemy import update
    monkeypatch.setenv("SERVE_ALERT_DISCORD_ID", "42")
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr("notification_service.send_dm", sent)
    monkeypatch.setattr("bot_registry.get_bot", lambda: object())
    await _withdraw_out("s1")
    await resolution._record_job("s2", "withdraw", GUILD, PLAYER, MTGO, 100, VaultReading(1000, NOW))
    async with db_session() as s:
        await s.execute(update(MtgoJob).where(MtgoJob.job_id == "s1").values(
            status="settle-done", resolved_at=datetime.now() - timedelta(hours=2)))
        await s.execute(update(MtgoJob).where(MtgoJob.job_id == "s2").values(
            status="settle-done", resolved_at=datetime.now() - timedelta(minutes=10)))

    assert await resolution.alert_assumed() == 1, "only the one stuck for over an hour"
    assert "s1" in sent.await_args.args[2]
    assert await resolution.alert_assumed() == 0


@pytest.mark.asyncio
async def test_a_stuck_assumed_settlement_still_says_it_was_assumed(test_db, monkeypatch):  # noqa: F811
    """The stuck alert is the only one such a row gets: once sent, the assumed alert
    skips it."""
    from sqlalchemy import update
    monkeypatch.setenv("SERVE_ALERT_DISCORD_ID", "42")
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr("notification_service.send_dm", sent)
    monkeypatch.setattr("bot_registry.get_bot", lambda: object())
    await _withdraw_out("s3")
    async with db_session() as s:
        await s.execute(update(MtgoJob).where(MtgoJob.job_id == "s3").values(
            status="settle-done", assumed="the serve forgot the job",
            resolved_at=datetime.now() - timedelta(hours=2)))

    assert await resolution.alert_assumed() == 1
    assert "the serve forgot the job" in sent.await_args.args[2]


def test_a_withdraw_the_vault_rose_for_is_not_a_delivery():
    assert classify(1000, 1100, -100) == "other"
    assert classify(1000, 900, +100) == "other", "nor a deposit the vault fell for"
