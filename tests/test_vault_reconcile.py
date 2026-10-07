"""A tix trade is booked by what the vault shows, not only what the serve says.

2026-10-03: a 160-tix withdraw left the vault, the serve reported it failed
(its trade events had stopped), and the refund paid the player twice. Every
tix trade now records the vault's count before it goes out, and settling
compares the vault afterwards:

  withdraw  dropped by n  -> delivered, never refunded -- whatever the serve said
            unchanged     -> refunded only if the serve also said failed
  deposit   rose by n     -> credited -- whatever the serve said
            unchanged     -> failed only if the serve also said failed
  anything else           -> held for an admin; an unreadable vault waits
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
                      (840, last_done + timedelta(seconds=20))])  # late enough

    assert (await vault_check.baseline(client)).tix == 840


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


async def _withdraw_out(job_id, n=100, baseline=1000):
    """A withdraw as start_withdraw leaves it: n committed to in-flight, job pending."""
    await _fund(n)
    await wallet_service.pay(GUILD, PLAYER, wallet_service.SYSTEM_IN_FLIGHT, n,
                             source=f"wd:{job_id}", notes="commit")
    reading = VaultReading(baseline, NOW) if baseline is not None else None
    await resolution._record_job(job_id, "withdraw", GUILD, PLAYER, MTGO, n, reading)


def _settle_with(serve_state, vault_after):
    """The serve reports `serve_state`; the vault then reads `vault_after` (None: unreadable)."""
    client = AsyncMock(enabled=True)
    client.get_job = AsyncMock(return_value={"state": serve_state, "detail": "x",
                                             "finishedAt": datetime.now(timezone.utc).isoformat()})
    reading = VaultReading(vault_after, NOW + timedelta(minutes=1)) if vault_after is not None else None
    return (patch("services.mtgo_resolution_service.get_client", return_value=client),
            patch.object(resolution.vault_check, "after", AsyncMock(return_value=reading)))


async def _finish_withdraw(job_id, serve_state, vault_after, n=100):
    a, b = _settle_with(serve_state, vault_after)
    with a, b:
        return await resolution.finish_withdraw(job_id, GUILD, PLAYER, n, MTGO, timeout_s=1)


async def _finish_deposit(job_id, serve_state, vault_after, n=50):
    a, b = _settle_with(serve_state, vault_after)
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
async def test_a_failed_withdraw_the_vault_confirms_is_refunded(test_db):  # noqa: F811
    await _withdraw_out("w2")
    res = await _finish_withdraw("w2", "failed", 1000)

    assert res["outcome"] == "failed"
    assert await _balances() == (100, 0)


@pytest.mark.asyncio
async def test_a_done_withdraw_the_vault_doesnt_show_is_held(test_db):  # noqa: F811
    """Debiting tix that never left would short the player; refunding a 'done' trade
    could pay twice. Neither is safe to do alone."""
    await _withdraw_out("w3")
    res = await _finish_withdraw("w3", "done", 1000)

    assert res["outcome"] == resolution.REVIEW
    assert await _balances() == (0, 100), "still committed"
    assert await _status("w3") == resolution.REVIEW


@pytest.mark.asyncio
async def test_a_partial_change_is_held(test_db):  # noqa: F811
    await _withdraw_out("w4")
    res = await _finish_withdraw("w4", "failed", 950)

    assert res["outcome"] == resolution.REVIEW
    assert await _balances() == (0, 100)


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
async def test_a_job_from_before_the_check_settles_as_reported(test_db):  # noqa: F811
    await _withdraw_out("w6", baseline=None)
    res = await _finish_withdraw("w6", "failed", None)

    assert res["outcome"] == "failed"
    assert await _balances() == (100, 0)


@pytest.mark.asyncio
async def test_a_deposit_is_credited_only_when_the_vault_rose(test_db):  # noqa: F811
    await resolution._record_job("d1", "deposit", GUILD, PLAYER, MTGO, 50, VaultReading(1000, NOW))
    res = await _finish_deposit("d1", "done", 1050)

    assert res["outcome"] == "done" and await _balances() == (50, 0)


@pytest.mark.asyncio
async def test_a_done_deposit_the_vault_doesnt_show_is_held_not_credited(test_db):  # noqa: F811
    await resolution._record_job("d2", "deposit", GUILD, PLAYER, MTGO, 50, VaultReading(1000, NOW))
    res = await _finish_deposit("d2", "done", 1000)

    assert res["outcome"] == resolution.REVIEW and await _balances() == (0, 0)


@pytest.mark.asyncio
async def test_a_deposit_reported_failed_that_arrived_is_credited(test_db):  # noqa: F811
    await resolution._record_job("d3", "deposit", GUILD, PLAYER, MTGO, 50, VaultReading(1000, NOW))
    res = await _finish_deposit("d3", "failed", 1050)

    assert res["outcome"] == "done" and await _balances() == (50, 0)


@pytest.mark.asyncio
async def test_a_failed_deposit_the_vault_confirms_is_failed(test_db):  # noqa: F811
    await resolution._record_job("d4", "deposit", GUILD, PLAYER, MTGO, 50, VaultReading(1000, NOW))
    res = await _finish_deposit("d4", "failed", 1000)

    assert res["outcome"] == "failed" and await _balances() == (0, 0)


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
async def test_a_held_chunk_is_reported_as_held(test_db, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MTGO_MAX_CARDS_PER_TRADE", "300")

    async def start(g, p, u, n, **kw):
        return {"ok": True, "job_id": "only"}

    async def finish(job_id, g, p, n, u):
        return {"ok": False, "outcome": resolution.REVIEW, "error": "held"}

    monkeypatch.setattr(resolution, "start_withdraw", start)
    monkeypatch.setattr(resolution, "finish_withdraw", finish)
    res = await resolution.run_withdraw_order(GUILD, PLAYER, MTGO, 200)

    assert res["review"] and not res["pending"] and res["open"] == 200


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
                     [(900, finished + timedelta(seconds=20))])

    assert (await vault_check.after(client, {"id": "ours", "finishedAt": finished.isoformat()}, BASE_AT)).tix == 900


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
    second = await _finish_withdraw("w7", "failed", 1000)   # a stale view: would refund

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


# ---- a lost POST is held, never refunded on a guess ---------------------------------------------------

@pytest.mark.asyncio
async def test_an_ambiguous_withdraw_post_with_no_job_found_is_held(test_db, monkeypatch):  # noqa: F811
    await _fund(100)
    monkeypatch.setattr(resolution.vault_check, "baseline",
                        AsyncMock(return_value=VaultReading(1000, NOW)))
    client = AsyncMock(enabled=True)
    client.withdraw_tix = AsyncMock(return_value={"_ambiguous": True})
    client.find_recent_job = AsyncMock(return_value=None)
    with patch("services.mtgo_resolution_service.get_client", return_value=client), \
         patch("services.mtgo_resolution_service.asyncio.sleep", AsyncMock()):
        res = await resolution.start_withdraw(GUILD, PLAYER, MTGO, 100)

    assert not res["ok"] and res["review"]
    assert await _balances() == (0, 100), "committed, not refunded"
    async with db_session() as s:
        held = [r for r in (await s.execute(MtgoJob.__table__.select())).all()
                if r.status == resolution.REVIEW]
    assert len(held) == 1 and held[0].job_id.startswith("lost-") and held[0].vault_before == 1000


@pytest.mark.asyncio
async def test_a_definite_refusal_is_refunded(test_db, monkeypatch):  # noqa: F811
    await _fund(100)
    monkeypatch.setattr(resolution.vault_check, "baseline",
                        AsyncMock(return_value=VaultReading(1000, NOW)))
    client = AsyncMock(enabled=True)
    client.withdraw_tix = AsyncMock(return_value=None)
    with patch("services.mtgo_resolution_service.get_client", return_value=client):
        res = await resolution.start_withdraw(GUILD, PLAYER, MTGO, 100)

    assert not res["ok"] and not res.get("review")
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
    client = _client(None, [(1000, finished + timedelta(seconds=40))])
    client.list_jobs = AsyncMock(side_effect=[
        [],
        [{"id": "next", "state": "running", "startedAt": (finished + timedelta(seconds=20)).isoformat()}],
    ])

    assert await vault_check.after(client, {"id": "ours", "finishedAt": finished.isoformat()}, BASE_AT) is None


@pytest.mark.asyncio
async def test_a_trade_that_began_after_the_reading_leaves_it_standing(monkeypatch):
    monkeypatch.setattr(vault_check.asyncio, "sleep", AsyncMock())
    finished = NOW
    client = _client(None, [(900, finished + timedelta(seconds=20))])
    client.list_jobs = AsyncMock(side_effect=[
        [],
        [{"id": "next", "state": "running", "startedAt": (finished + timedelta(seconds=30)).isoformat()}],
    ])

    assert (await vault_check.after(client, {"id": "ours", "finishedAt": finished.isoformat()}, BASE_AT)).tix == 900


@pytest.mark.asyncio
async def test_a_running_job_with_no_start_time_voids_the_reading(monkeypatch):
    monkeypatch.setattr(vault_check.asyncio, "sleep", AsyncMock())
    client = _client(None, [(900, NOW + timedelta(seconds=20))])
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
async def test_a_start_held_for_review_is_reported_held_not_failed(test_db, monkeypatch):  # noqa: F811
    """A lost POST leaves its chunk committed. Reporting it as a plain failure would tell
    the player the tix are back in the wallet and to try again."""
    monkeypatch.setenv("MTGO_MAX_CARDS_PER_TRADE", "300")

    async def start(g, p, u, n, **kw):
        return {"ok": False, "review": True, "outcome": resolution.REVIEW,
                "error": "held", "job_id": "lost-abc"}

    monkeypatch.setattr(resolution, "start_withdraw", start)
    res = await resolution.run_withdraw_order(GUILD, PLAYER, MTGO, 200)

    assert res["review"] and res["open"] == 200 and res["jobs"] == ["lost-abc"]


# ---- round 4: nothing else may trade between the baseline and the after-reading ----------------

@pytest.mark.asyncio
async def test_a_trade_that_ran_while_the_baseline_was_read_voids_it(monkeypatch):
    """The job list is read before waiting out the cache; a trade that started during
    the wait would sit between the baseline and the trade it is for."""
    monkeypatch.setattr(vault_check.asyncio, "sleep", AsyncMock())
    client = _client(None, [(1000, NOW)])
    client.list_jobs = AsyncMock(side_effect=[
        [],
        [{"id": "x", "state": "running", "startedAt": (NOW - timedelta(seconds=10)).isoformat()}],
    ])
    assert await vault_check.baseline(client) is None


@pytest.mark.asyncio
async def test_a_trade_that_finished_too_close_to_the_baseline_voids_it(monkeypatch):
    monkeypatch.setattr(vault_check.asyncio, "sleep", AsyncMock())
    client = _client(None, [(1000, NOW)])
    client.list_jobs = AsyncMock(side_effect=[
        [],
        [{"id": "x", "state": "done", "finishedAt": (NOW - timedelta(seconds=3)).isoformat()}],
    ])
    assert await vault_check.baseline(client) is None


@pytest.mark.asyncio
async def test_a_baseline_with_nothing_trading_stands(monkeypatch):
    monkeypatch.setattr(vault_check.asyncio, "sleep", AsyncMock())
    client = _client([{"id": "x", "state": "done",
                       "finishedAt": (NOW - timedelta(minutes=2)).isoformat()}], [(1000, NOW)])
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
    client = _client([other], [(900, NOW + timedelta(seconds=20))])
    assert (await vault_check.after(client, {"id": "ours", "finishedAt": NOW.isoformat()}, BASE_AT)).tix == 900


@pytest.mark.asyncio
async def test_a_reading_that_never_comes_is_held_for_review_not_confirmed_forever(test_db):  # noqa: F811
    """A window another trade broke stays broken; the player must not wait on it forever."""
    await _withdraw_out("w20")
    client = AsyncMock(enabled=True)
    long_ago = datetime.now(timezone.utc) - timedelta(minutes=20)
    client.get_job = AsyncMock(return_value={"state": "failed", "detail": "x",
                                             "finishedAt": long_ago.isoformat()})
    with patch("services.mtgo_resolution_service.get_client", return_value=client), \
         patch.object(resolution.vault_check, "after", AsyncMock(return_value=None)):
        await resolution.finish_withdraw("w20", GUILD, PLAYER, 100, MTGO)
    assert await _status("w20") == resolution.REVIEW
    assert await _balances() == (0, 100), "neither refunded nor debited"


@pytest.mark.asyncio
async def test_a_job_with_no_finish_time_still_reaches_review(test_db):  # noqa: F811
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
    assert await _status("w21") == resolution.REVIEW
    assert await _balances() == (0, 100)
