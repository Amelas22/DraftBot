"""A withdraw is refunded only when the serve can prove nothing moved.

2026-10-03: a 160-tix withdraw left the vault, the serve reported it failed
(its trade events had silently stopped), and the refund paid the player twice.
The serve now says why a failure is safe to refund -- its final approve never
went out, or its vault showed no change once the trade was over -- and marks
the job `refundable`. Anything else stays committed to in-flight and is held
for an admin: a refund can still happen after a look at the vault, a double
payment can't be taken back.
"""
from unittest.mock import AsyncMock, patch

import pytest

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import db_session
from models.mtgo_job import MtgoJob
from services import mtgo_resolution_service as resolution
from services import wallet_service
from services.mtgo_tradebot_client import JobListUnavailable

GUILD, PLAYER, MTGO = "g1", "p1", "Someone"


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


async def _committed_withdraw(job_id, n=100):
    """A withdraw as start_withdraw leaves it: n committed to in-flight, job pending."""
    await _fund(n)
    await wallet_service.pay(GUILD, PLAYER, wallet_service.SYSTEM_IN_FLIGHT, n,
                             source=f"wd:{job_id}", notes="commit")
    await resolution._record_job(job_id, "withdraw", GUILD, PLAYER, MTGO, n)


def _serve(job):
    client = AsyncMock(enabled=True)
    client.get_job = AsyncMock(return_value=job)
    return patch("services.mtgo_resolution_service.get_client", return_value=client)


# ---- finish_withdraw ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_proven_failure_is_refunded(test_db):  # noqa: F811
    await _committed_withdraw("j1")
    with _serve({"state": "failed", "refundable": True, "vault": "none", "detail": "declined"}):
        res = await resolution.finish_withdraw("j1", GUILD, PLAYER, 100, MTGO, timeout_s=1)

    assert res["outcome"] == "failed"
    assert await _balances() == (100, 0), "back in the wallet"
    assert await _status("j1") == "failed"


@pytest.mark.asyncio
async def test_a_failure_without_proof_is_held_not_refunded(test_db):  # noqa: F811
    """The 2026-10-03 shape: failed, but nothing proves the tix didn't leave."""
    await _committed_withdraw("j2")
    with _serve({"state": "failed", "refundable": False, "vault": "unverified",
                 "detail": "the trade never confirmed as complete"}):
        res = await resolution.finish_withdraw("j2", GUILD, PLAYER, 100, MTGO, timeout_s=1)

    assert res["outcome"] == resolution.REVIEW
    assert await _balances() == (0, 100), "still committed, not refunded"
    assert await _status("j2") == resolution.REVIEW, "out of 'pending', so no poller acts on it"


@pytest.mark.asyncio
async def test_a_serve_that_says_nothing_about_proof_is_not_trusted(test_db):  # noqa: F811
    """An older serve never sends `refundable`. Its failures are exactly the
    unproven kind, so they are held too -- deploy the serve first."""
    await _committed_withdraw("j3")
    with _serve({"state": "failed", "detail": "trade failed"}):
        res = await resolution.finish_withdraw("j3", GUILD, PLAYER, 100, MTGO, timeout_s=1)

    assert res["outcome"] == resolution.REVIEW
    assert await _balances() == (0, 100)


@pytest.mark.asyncio
async def test_a_held_job_is_not_picked_up_again_by_the_watchdog(test_db):  # noqa: F811
    await _committed_withdraw("j4")
    with _serve({"state": "failed", "refundable": False}):
        await resolution.finish_withdraw("j4", GUILD, PLAYER, 100, MTGO, timeout_s=1)
    with _serve({"state": "failed", "refundable": True}):
        assert await resolution.resume_pending_jobs() == 0, "held jobs wait for a human"
    assert await _balances() == (0, 100)


# ---- finish_deposit -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_unverified_deposit_is_held_not_failed(test_db):  # noqa: F811
    """The serve couldn't confirm the tix arrived, so it didn't let us credit
    them -- but they may have. Held, so nobody is told it simply failed."""
    await resolution._record_job("d1", "deposit", GUILD, PLAYER, MTGO, 50)
    with _serve({"state": "failed", "refundable": False, "vault": "unverified"}):
        res = await resolution.finish_deposit("d1", GUILD, PLAYER, 50, MTGO, timeout_s=1)

    assert res["outcome"] == resolution.REVIEW
    assert await _balances() == (0, 0), "nothing credited"
    assert await _status("d1") == resolution.REVIEW


@pytest.mark.asyncio
async def test_a_plainly_failed_deposit_is_still_just_failed(test_db):  # noqa: F811
    await resolution._record_job("d2", "deposit", GUILD, PLAYER, MTGO, 50)
    with _serve({"state": "failed", "refundable": True, "vault": "not-approved"}):
        res = await resolution.finish_deposit("d2", GUILD, PLAYER, 50, MTGO, timeout_s=1)

    assert res["outcome"] == "failed"
    assert await _status("d2") == "failed"


# ---- start_withdraw: a lost POST ------------------------------------------------------

@pytest.mark.asyncio
async def test_an_unreadable_job_list_holds_the_withdraw(test_db):  # noqa: F811
    """The POST may have created a trade, and we couldn't look. Refunding here
    pays twice if it did."""
    await _fund(100)
    client = AsyncMock(enabled=True)
    client.withdraw_tix = AsyncMock(return_value={"_ambiguous": True})
    client.find_recent_job = AsyncMock(side_effect=JobListUnavailable())
    with patch("services.mtgo_resolution_service.get_client", return_value=client):
        res = await resolution.start_withdraw(GUILD, PLAYER, MTGO, 100)

    assert not res["ok"] and res.get("review")
    assert await _balances() == (0, 100), "committed, not refunded"
    async with db_session() as s:
        held = [r for r in (await s.execute(MtgoJob.__table__.select())).all()
                if r.status == resolution.REVIEW]
    assert len(held) == 1 and held[0].job_id.startswith("lost-"), "an admin can find it"


@pytest.mark.asyncio
async def test_a_definite_rejection_is_still_refunded(test_db):  # noqa: F811
    """No ambiguity, no job: the serve refused it, nothing can be trading."""
    await _fund(100)
    client = AsyncMock(enabled=True)
    client.withdraw_tix = AsyncMock(return_value=None)
    with patch("services.mtgo_resolution_service.get_client", return_value=client):
        res = await resolution.start_withdraw(GUILD, PLAYER, MTGO, 100)

    assert not res["ok"] and not res.get("review")
    assert await _balances() == (100, 0)


@pytest.mark.asyncio
async def test_an_ambiguous_post_with_no_job_found_is_still_held(test_db):  # noqa: F811
    """Not finding the job is not proof there isn't one: a POST still in flight can
    create it after we looked. Only a definite rejection is its own proof."""
    await _fund(100)
    client = AsyncMock(enabled=True)
    client.withdraw_tix = AsyncMock(return_value={"_ambiguous": True})
    client.find_recent_job = AsyncMock(return_value=None)
    with patch("services.mtgo_resolution_service.get_client", return_value=client), \
         patch("services.mtgo_resolution_service.asyncio.sleep", AsyncMock()):
        res = await resolution.start_withdraw(GUILD, PLAYER, MTGO, 100)

    assert not res["ok"] and res.get("review")
    assert await _balances() == (0, 100), "committed, not refunded"


# ---- two pollers, one job ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_poller_that_loses_the_claim_moves_no_money(test_db):  # noqa: F811
    """A command poller and the watchdog can both finish one job. If they read
    different replies, only the one that claimed the job may book -- the other
    reports what the winner decided."""
    await _committed_withdraw("j5")
    with _serve({"state": "done"}):
        first = await resolution.finish_withdraw("j5", GUILD, PLAYER, 100, MTGO, timeout_s=1)
    with _serve({"state": "failed", "refundable": True}):
        second = await resolution.finish_withdraw("j5", GUILD, PLAYER, 100, MTGO, timeout_s=1)

    assert first["outcome"] == "done" and second["outcome"] == "done"
    assert await _balances() == (0, 0), "debited once, never refunded"


@pytest.mark.asyncio
async def test_a_booking_that_raises_leaves_the_job_settleable(test_db, monkeypatch):  # noqa: F811
    await _committed_withdraw("j6")

    async def boom(*a, **k):
        raise RuntimeError("db hiccup")
    real_debit = resolution.wallet_service.debit_done
    monkeypatch.setattr(resolution.wallet_service, "debit_done", boom)
    with _serve({"state": "done"}), pytest.raises(RuntimeError):
        await resolution.finish_withdraw("j6", GUILD, PLAYER, 100, MTGO, timeout_s=1)

    # Still claimed for 'done' -- never back to pending, where a later "failed"
    # could refund on top of a debit that may have landed.
    assert await _status("j6") == "settle-done"
    with _serve({"state": "failed", "refundable": True}):
        res = await resolution.finish_withdraw("j6", GUILD, PLAYER, 100, MTGO, timeout_s=1)
    assert res["outcome"] == "pending" and await _balances() == (0, 100), "no refund"

    # Recovery finishes it, once the settlement is stale.
    from datetime import datetime, timedelta
    from sqlalchemy import update
    monkeypatch.setattr(resolution.wallet_service, "debit_done", real_debit)
    async with db_session() as s:
        await s.execute(update(MtgoJob).where(MtgoJob.job_id == "j6").values(
            resolved_at=datetime.now() - timedelta(minutes=10)))
    assert await resolution._finish_settlements() == 1
    assert await _status("j6") == "done" and await _balances() == (0, 0)


@pytest.mark.asyncio
async def test_known_jobs_are_excluded_whatever_the_case_of_the_name(test_db):  # noqa: F811
    """Adoption matches the user case-insensitively; so must the exclusion."""
    await resolution._record_job("old-job", "withdraw", GUILD, PLAYER, "SomeOne", 100)
    assert "old-job" in await resolution._known_job_ids("someone")


@pytest.mark.asyncio
async def test_an_unreadable_job_list_is_busy_not_free():
    from helpers.money_gate import serve_busy_reason
    client = AsyncMock()
    client.url = "http://serve"
    client.health = AsyncMock(return_value={"ok": True})
    client.active_jobs = AsyncMock(return_value=None)

    assert await serve_busy_reason(client) is not None


# ---- the order runner ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_held_chunk_is_reported_as_held_not_failed(test_db, monkeypatch):  # noqa: F811
    """So the command doesn't tell the player those tix are back in their
    wallet (they aren't) or to send a deposit again (they may have)."""
    monkeypatch.setenv("MTGO_MAX_CARDS_PER_TRADE", "300")

    async def fake_start(g, p, u, n, **kw):
        return {"ok": True, "job_id": "only"}

    async def fake_finish(job_id, g, p, n, u):
        return {"ok": False, "outcome": resolution.REVIEW, "error": "held"}

    monkeypatch.setattr(resolution, "start_withdraw", fake_start)
    monkeypatch.setattr(resolution, "finish_withdraw", fake_finish)
    res = await resolution.run_withdraw_order(GUILD, PLAYER, MTGO, 200)

    assert res["review"] and not res["pending"]
    assert res["open"] == 200 and res["moved"] == 0


# ---- recovery must not mistake "can't tell" for "no job" ---------------------------------

@pytest.mark.asyncio
async def test_a_reply_that_isnt_a_listing_is_unreadable_not_empty():
    """A 200 without a job list once read as "the serve has no jobs", which
    adoption took as "no trade exists" and refunded."""
    from services.mtgo_tradebot_client import MtgoTradeBotClient
    client = MtgoTradeBotClient.__new__(MtgoTradeBotClient)
    for reply in ({"raw": "<html>"}, {"jobs": "nope"}, None, []):
        client._call = AsyncMock(return_value=reply)
        assert await client._list_jobs() is None, reply
        with pytest.raises(JobListUnavailable):
            await client.find_recent_job("request", MTGO, 100, strict=True)


@pytest.mark.asyncio
async def test_a_strict_lookup_adopts_a_failed_job():
    """"It failed" is not "it never existed": only the failed job's own proof
    can say nothing moved, so recovery hands it over instead of hiding it."""
    from datetime import datetime, timezone
    from services.mtgo_tradebot_client import MtgoTradeBotClient
    client = MtgoTradeBotClient.__new__(MtgoTradeBotClient)
    failed = {"id": "f1", "type": "request", "state": "failed", "user": MTGO,
              "give": [{"name": "Event Ticket", "qty": 100}],
              "createdAt": datetime.now(timezone.utc).isoformat()}
    client._call = AsyncMock(return_value={"jobs": [failed]})

    assert await client.find_recent_job("request", MTGO, 100) is None, "lenient: skipped as before"
    assert (await client.find_recent_job("request", MTGO, 100, strict=True))["id"] == "f1"


@pytest.mark.asyncio
async def test_recovery_never_adopts_a_job_already_booked(test_db):  # noqa: F811
    """Adoption matches type, user and amount over minutes; an earlier withdraw
    of the same size is a perfect match and already booked."""
    await resolution._record_job("old-job", "withdraw", GUILD, PLAYER, MTGO, 100)
    client = AsyncMock(enabled=True)
    client.find_recent_job = AsyncMock(return_value=None)
    with patch("services.mtgo_resolution_service.get_client", return_value=client), \
         patch("services.mtgo_resolution_service.asyncio.sleep", AsyncMock()):
        await resolution._recover_lost_job({"_ambiguous": True}, "request", MTGO, 100)

    excluded = client.find_recent_job.await_args.kwargs["exclude_ids"]
    assert "old-job" in excluded


@pytest.mark.asyncio
async def test_a_deposit_whose_post_is_unknown_is_held_and_findable(test_db):  # noqa: F811
    """A trade may still open and move tix with no job to credit them."""
    client = AsyncMock(enabled=True)
    client.deposit_tix = AsyncMock(return_value={"_ambiguous": True})
    client.find_recent_job = AsyncMock(side_effect=JobListUnavailable())
    with patch("services.mtgo_resolution_service.get_client", return_value=client):
        res = await resolution.start_deposit(GUILD, PLAYER, MTGO, 50)

    assert not res["ok"] and res.get("review")
    async with db_session() as s:
        held = [r for r in (await s.execute(MtgoJob.__table__.select())).all()
                if r.status == resolution.REVIEW]
    assert len(held) == 1 and held[0].kind == "deposit"


# ---- a job-creating POST: only a refusal proves no job exists ----------------------------

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
@pytest.mark.parametrize("code,text", [(500, "boom"), (202, ""), (202, "not json"), (202, "{}")])
async def test_a_reply_that_doesnt_name_a_job_is_ambiguous_not_a_refusal(monkeypatch, code, text):
    """A 5xx can come after the job was queued; a 2xx means it WAS accepted. Read
    as a definite failure, either one refunded a withdraw whose trade went ahead."""
    from services.mtgo_tradebot_client import MtgoTradeBotClient
    client = MtgoTradeBotClient(url="http://serve", token="t")
    _post_reply(monkeypatch, client, code, text)

    assert await client.withdraw_tix(MTGO, 100) == {"_ambiguous": True}


@pytest.mark.asyncio
async def test_a_refusal_is_definite(monkeypatch):
    from services.mtgo_tradebot_client import MtgoTradeBotClient
    client = MtgoTradeBotClient(url="http://serve", token="t")
    _post_reply(monkeypatch, client, 409, '{"error": "busy"}')

    assert await client.withdraw_tix(MTGO, 100) is None


@pytest.mark.asyncio
async def test_an_accepted_job_comes_back_as_itself(monkeypatch):
    from services.mtgo_tradebot_client import MtgoTradeBotClient
    client = MtgoTradeBotClient(url="http://serve", token="t")
    _post_reply(monkeypatch, client, 202, '{"id": "abc"}')

    assert (await client.withdraw_tix(MTGO, 100))["id"] == "abc"


# ---- a settlement interrupted between claim and booking ---------------------------------

@pytest.mark.asyncio
async def test_an_interrupted_settlement_is_finished_not_forgotten(test_db):  # noqa: F811
    """Claimed for 'done', process died before the debit: the watchdog books it
    and marks it done, instead of the row reading as settled with nothing booked."""
    from datetime import datetime, timedelta
    from sqlalchemy import update
    await _committed_withdraw("j7")
    async with db_session() as s:
        await s.execute(update(MtgoJob).where(MtgoJob.job_id == "j7").values(
            status="settle-done", resolved_at=datetime.now() - timedelta(minutes=10)))

    assert await resolution._finish_settlements() == 1
    assert await _status("j7") == "done"
    assert await _balances() == (0, 0), "the debit was booked"
    assert await resolution._finish_settlements() == 0, "and only once"


@pytest.mark.asyncio
async def test_a_live_settlement_is_left_to_its_settler(test_db):  # noqa: F811
    from datetime import datetime
    from sqlalchemy import update
    await _committed_withdraw("j8")
    async with db_session() as s:
        await s.execute(update(MtgoJob).where(MtgoJob.job_id == "j8").values(
            status="settle-done", resolved_at=datetime.now()))

    assert await resolution._finish_settlements() == 0
    assert await _status("j8") == "settle-done"


@pytest.mark.asyncio
async def test_a_failed_deposit_without_proof_is_held(test_db):  # noqa: F811
    """A serve that doesn't say nothing arrived may have received the tix:
    telling the player it failed has them send the tix again."""
    await resolution._record_job("d3", "deposit", GUILD, PLAYER, MTGO, 50)
    with _serve({"state": "failed", "detail": "trade failed"}):
        res = await resolution.finish_deposit("d3", GUILD, PLAYER, 50, MTGO, timeout_s=1)

    assert res["outcome"] == resolution.REVIEW


@pytest.mark.asyncio
async def test_a_claim_still_booking_reads_as_pending_to_the_loser(test_db):  # noqa: F811
    """The booking can still raise and put the job back; only a final status is an answer."""
    from sqlalchemy import update
    await _committed_withdraw("j9")
    async with db_session() as s:
        await s.execute(update(MtgoJob).where(MtgoJob.job_id == "j9").values(status="settle-done"))

    assert (await resolution._settled_elsewhere("j9"))["outcome"] == "pending"


@pytest.mark.asyncio
async def test_an_interrupted_deposit_is_applied_like_any_other(test_db, monkeypatch):  # noqa: F811
    from datetime import datetime, timedelta
    from sqlalchemy import update
    applied = []

    async def inflow(g, p):
        applied.append(p)
        return [], []
    monkeypatch.setattr(resolution, "settle_deposit_inflow", inflow)
    await resolution._record_job("d4", "deposit", GUILD, PLAYER, MTGO, 50)
    async with db_session() as s:
        await s.execute(update(MtgoJob).where(MtgoJob.job_id == "d4").values(
            status="settle-done", resolved_at=datetime.now() - timedelta(minutes=10)))

    assert await resolution._finish_settlements() == 1
    assert await _balances() == (50, 0) and applied == [PLAYER]


@pytest.mark.asyncio
async def test_a_job_older_than_the_post_is_never_adopted():
    """Type, user and amount match an EARLIER trade perfectly; when it was created
    is what says it isn't this post's."""
    from datetime import datetime, timedelta, timezone
    from services.mtgo_tradebot_client import MtgoTradeBotClient
    client = MtgoTradeBotClient.__new__(MtgoTradeBotClient)
    now = datetime.now(timezone.utc)
    earlier = {"id": "old", "type": "request", "state": "done", "user": MTGO,
               "give": [{"name": "Event Ticket", "qty": 100}],
               "createdAt": (now - timedelta(minutes=3)).isoformat()}
    client._call = AsyncMock(return_value={"jobs": [earlier]})

    assert await client.find_recent_job("request", MTGO, 100, max_age_s=720, strict=True,
                                        not_before=now - timedelta(seconds=5)) is None
    assert (await client.find_recent_job("request", MTGO, 100, max_age_s=720, strict=True,
                                         not_before=now - timedelta(minutes=5)))["id"] == "old"


@pytest.mark.asyncio
async def test_a_job_put_back_to_pending_reads_as_pending_to_the_loser(test_db):  # noqa: F811
    await _committed_withdraw("j10")
    assert (await resolution._settled_elsewhere("j10"))["outcome"] == "pending"


@pytest.mark.asyncio
async def test_a_timezone_less_timestamp_is_skipped_not_raised():
    """Comparing it with an aware "now" raises TypeError -- which, escaping
    recovery after a withdraw was committed, would leave no job and no hold."""
    from services.mtgo_tradebot_client import MtgoTradeBotClient
    client = MtgoTradeBotClient.__new__(MtgoTradeBotClient)
    naive = {"id": "n1", "type": "request", "state": "done", "user": MTGO,
             "give": [{"name": "Event Ticket", "qty": 100}], "createdAt": "2026-10-04T12:00:00"}
    client._call = AsyncMock(return_value={"jobs": [naive]})

    assert await client.find_recent_job("request", MTGO, 100, strict=True) is None
