"""
Resolution engine — the bridge between the two ledgers.

  * Physical ledger = the MTGO TradeBot serve (real vault contents; trades).
  * Claim ledger    = wallet_service / WalletTx (who is owed what).

A trade fires ONLY when value crosses the vault boundary:
  - deposit  (bot receives tix)  -> credit the player once the job is 'done'.
  - withdraw (bot gives tix)     -> transfer the tix to ``system:in-flight`` up front,
                                    then on 'done' book the boundary debit against that
                                    holder, or on 'failed' transfer them back.
Everything internal (pay, entry fees, debt settlement, auto-draw) is a claim transfer
between holders with NO trade and no change to the system total.

Job discipline (matches the plan): each serve op is an async job with a lowercase
``state`` ∈ queued|running|done|failed. The ``start_*`` calls enqueue and return fast
(the in-flight commit is the only immediate ledger effect, for withdraws); ``finish_*``
poll to a terminal state and write the ledger — credits are booked ONLY on 'done', never
on enqueue, and idempotently by ``job_id`` so a re-poll can't double-book. Split this way
so a cog can enqueue synchronously, defer the interaction, and run ``finish_*`` as a
background task within Discord's interaction window.
"""
import asyncio
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select, func, update

from database.db_session import db_session
from database.retry import with_db_retry
from models.mtgo_job import MtgoJob
from models.wallet_tx import WalletTx
from models.debt_ledger import DebtLedger
from helpers.money_gate import serve_busy_reason, spawn_followup
from services.mtgo_tradebot_client import (JobListUnavailable, ORDER_TIMEOUT_S, get_client,
                                           max_cards_per_trade)
from services import wallet_service
from services import debt_service
# Module scope, unlike the notifier below: preference_service reaches only config,
# session and models, so it closes no cycle back into this module.
from preference_service import get_players_dm_notification_preferences

# Poll fast at first (a serve rejection surfaces in seconds), then back off — the human
# step (accepting an MTGO trade) takes minutes, so terminal-detection latency is cheap.
_POLL_INTERVAL_S = 3.0
_POLL_INTERVAL_MAX_S = 15.0
_POLL_BACKOFF = 1.5
# Keep the inline poll inside Discord's 15-minute interaction/followup window.
_DEFAULT_POLL_TIMEOUT_S = 14 * 60


# ---------------------------------------------------------------------------
# job polling
# ---------------------------------------------------------------------------
async def _poll_job(job_id: str, timeout_s: float):
    """Poll GET /jobs/{id} until terminal. Returns (outcome, job) where outcome is
    'done' | 'failed' | 'pending' ('pending' = still running when the timeout hit)."""
    client = get_client()
    waited = 0.0
    interval = _POLL_INTERVAL_S
    last = None
    while waited < timeout_s:
        job = await client.get_job(job_id)
        if job is not None:
            last = job
            state = (job.get("state") or "").lower()
            if state == "done":
                return "done", job
            if state == "failed":
                return "failed", job
        await asyncio.sleep(interval)
        waited += interval
        interval = min(interval * _POLL_BACKOFF, _POLL_INTERVAL_MAX_S)
    return "pending", (last or {"id": job_id, "state": "pending"})


# ---------------------------------------------------------------------------
# durable job records — every started serve job is persisted so a startup
# resumer can finish booking trades that outlive their in-memory poller
# ---------------------------------------------------------------------------
async def _record_job(job_id: str, kind: str, guild_id: str, player_id: str, mtgo_user: str,
                      amount: int):
    async def _do():
        async with db_session() as session:
            if await session.get(MtgoJob, job_id):
                return
            session.add(MtgoJob(
                job_id=job_id, kind=kind, guild_id=guild_id, player_id=player_id,
                mtgo_user=mtgo_user, amount=amount, status="pending"))
    await with_db_retry(_do)


# The recoverable middle of a settlement: claimed for this outcome, money not yet
# confirmed booked. Fits the 16-char status column.
_SETTLING = "settle-"


async def _move_status(job_id: str, frm: str, to: str) -> bool:
    """Atomically move a job from one status to another. True only for the caller that did it."""
    async def _do():
        async with db_session() as session:
            res = await session.execute(
                update(MtgoJob)
                .where(MtgoJob.job_id == job_id, MtgoJob.status == frm)
                .values(status=to, resolved_at=datetime.now()))
            return res.rowcount == 1
    return await with_db_retry(_do)


async def _settle(job_id: str, status: str, book: "Callable[[], Any] | None" = None) -> bool:
    """Claim the job, book, then mark it final. False if another poller already claimed it.

    Settlement claims FIRST and moves money only if it won: a command's poller and the
    watchdog's resumed poller can both reach a terminal state for one job, and if they
    read different replies (failed, then done) each used to book its own -- a refund AND
    a debit, or money moved under a row still marked held.

    The claim is to 'settle-<status>', not straight to the final status, so a process that
    dies between claim and booking leaves a row that says so: _finish_settlements re-runs
    the booking (every booking is idempotent by its key) instead of the job reading as
    settled with nothing booked."""
    middle = f"{_SETTLING}{status}" if book is not None else status
    if not await _move_status(job_id, "pending", middle):
        return False
    if book is not None:
        # No rollback if the booking raises: it may already have committed (a refund's
        # transfer lands before its inflow sweep runs), and putting the job back to
        # pending would let a later "done" book a debit on top. It stays claimed for
        # this outcome, and _finish_settlements completes it -- every booking is
        # idempotent, so whatever already landed is not repeated.
        await book()
        await _move_status(job_id, middle, status)
    return True


async def _settled_elsewhere(job_id: str) -> dict[str, Any]:
    """What a poller that lost the claim reports: whatever the winner decided (or is
    finishing booking -- a settle-* row is that outcome, already claimed)."""
    async with db_session() as session:
        row = await session.get(MtgoJob, job_id)
    status = row.status if row else None
    if status and status.startswith(_SETTLING):
        # Claimed, booking not yet confirmed -- and a booking that raises puts the job
        # back to pending. Report it as still open until the final status lands.
        return {"ok": False, "outcome": "pending"}
    if status == "done":
        return {"ok": True, "outcome": "done",
                "credited": row.amount if row and row.kind == "deposit" else 0}
    if status == REVIEW:
        return {"ok": False, "outcome": REVIEW, "review": True,
                "error": "the trade didn't confirm cleanly -- an admin is checking it"}
    if status == "failed":
        return {"ok": False, "outcome": "failed", "error": "trade failed"}
    # pending (the winner's booking raised and put it back) or unknown: not an answer yet.
    return {"ok": False, "outcome": "pending"}


# A job the house can't settle safely on its own: the money stays where it is (a
# withdraw's tix stay committed to in-flight, a deposit stays uncredited) and the row
# leaves 'pending' so no poller acts on it again. An admin resolves it against the
# serve's ledger and vault.
REVIEW = "review"


async def _hold_for_review(job_id: str, why: str, player_msg: str) -> dict[str, Any]:
    """Hold a job and return the result its caller reports."""
    if not await _settle(job_id, REVIEW):
        return await _settled_elsewhere(job_id)
    logger.error(f"MTGO job {job_id} HELD FOR REVIEW -- {why}")
    return {"ok": False, "outcome": REVIEW, "review": True, "error": player_msg}


async def _hold_lost_post(kind: str, guild_id: str, player_id: str, mtgo_user: str,
                          n: int, why: str, player_msg: str) -> dict[str, Any]:
    """A POST whose outcome is unknown and whose job we couldn't look for: a trade may
    exist. A placeholder row keeps it findable, since no real job id ever came back."""
    placeholder = f"lost-{uuid.uuid4().hex}"[:64]
    await _record_job(placeholder, kind, guild_id, player_id, mtgo_user, n)
    return await _hold_for_review(placeholder, why, player_msg)


async def _known_job_ids(mtgo_user: str) -> "set[str]":
    """Every job already booked for this MTGO user. Adoption matches on type, user and
    amount over minutes, so without this an ambiguous POST that created nothing could
    adopt an EARLIER, already-booked trade -- and idempotent booking would then quietly
    book nothing for the new one."""
    # Case-insensitive, as adoption's own user match is.
    async with db_session() as session:
        rows = await session.execute(select(MtgoJob.job_id).where(
            func.lower(MtgoJob.mtgo_user) == mtgo_user.lower()))
    return {r[0] for r in rows.all()}


# How long after a lost POST a job it created can still be adopted. The POST itself may
# take ORDER_TIMEOUT_S before it gives up, so a job created at its start is already that
# old when we look; a shorter window would refund a trade that is still live.
_ADOPT_WINDOW_S = ORDER_TIMEOUT_S + 120


def _post_stamp() -> datetime:
    """When a job-creating POST is about to go out, in the serve's terms (UTC), less a
    few seconds of slack for the two clocks."""
    from datetime import timezone
    return datetime.now(timezone.utc) - timedelta(seconds=5)


def _ambiguous(resp: dict[str, Any] | None) -> bool:
    """The client's flag for a POST that may have reached the serve with only the
    reply lost -- the one case where "no job" can't be taken as "no trade"."""
    return bool(resp and resp.get("_ambiguous"))


async def _adopt_or_none(resp: dict[str, Any] | None, job_type: str, mtgo_user: str, n: int,
                         exclude_ids: "Iterable[str]", sent_at: datetime) -> dict[str, Any] | None:
    """The lost POST's job if it can be found, else None. Whether None is then safe to
    act on is the caller's question: only a definite rejection (not ambiguous) is.

    ``sent_at`` is when THIS post went out: a job created before it is an earlier trade,
    however well it matches. A serve clock running behind ours can hide the right job,
    which then holds for review -- never adopts the wrong one."""
    try:
        job = await _recover_lost_job(resp, job_type, mtgo_user, n, exclude_ids=exclude_ids,
                                      not_before=sent_at)
    except JobListUnavailable:
        return None
    if job:
        logger.warning(f"adopted {job_type} job {job.get('id')} after a lost POST response")
    return job if job and job.get("id") else None


async def _recover_lost_job(resp: dict[str, Any] | None, job_type: str,
                            mtgo_user: str, n: int,
                            exclude_ids: "Iterable[str]" = (),
                            not_before: "datetime | None" = None) -> dict[str, Any] | None:
    """Recovery for a failed job POST. Runs the /jobs adoption scan ONLY when the client
    flagged the failure as ambiguous (delivered-but-response-lost is possible); a definite
    rejection or never-connected error returns None immediately — no job can exist, and
    the caller may fail fast. One retry covers a brief flap.

    Raises JobListUnavailable when it could not LOOK -- a read of the serve's job
    list that failed is not a list without the job, and treating it as one refunded
    withdraws whose trade was still live."""
    if not (resp and resp.get("_ambiguous")):
        return None
    client = get_client()
    excluded = set(exclude_ids or ()) | await _known_job_ids(mtgo_user)
    for attempt in range(2):
        if attempt:
            await asyncio.sleep(2)
        job = await client.find_recent_job(job_type, mtgo_user, n, max_age_s=_ADOPT_WINDOW_S,
                                           exclude_ids=excluded, strict=True,
                                           not_before=not_before)
        if job is not None:
            return job
    return None


# ---------------------------------------------------------------------------
# deposit (bot receives tix) — credit only on 'done'

# How many trades one command will run. The custodian works one at a time and
# each needs a human to accept it, so an order past this is not a long wait --
# it is somebody who should run the command twice.
#
# It also bounds the arithmetic: `chunk_amounts` materialises one element per
# trade, and Discord will hand us any integer up to 2**53. Without a ceiling,
# `/wallet deposit 1000000000000` allocates its way through the process inside
# a background task, which is an OOM kill rather than a caught error.
MAX_TRADES_PER_ORDER = 10


def chunk_amounts(n: int, limit: int) -> "list[int]":
    """``n`` split into trade-sized pieces, full ones first.

    The serve refuses an order above its per-trade limit, so anything larger
    goes as several trades.

    Full chunks first because the run stops at the first trade that does not
    complete: the earlier a piece sits, the likelier it lands, so leading with
    the big ones maximises what a stopped run actually handed over. 500 as
    [300, 200] delivers 300 when the second trade fails; [200, 300] delivers
    200.
    """
    whole, rest = divmod(n, limit)
    return [limit] * whole + ([rest] if rest else [])


def trade_count(n: int) -> int:
    """How many trades an order of ``n`` will take.

    So a caller can say so up front without importing the serve's limit.

    Counted rather than planned: this runs in the command body on a number
    Discord will accept up to 2**53, and building one list element per trade
    would let `/wallet withdraw 1000000000000` allocate its way through the
    process before any balance check ran.
    """
    limit = max_cards_per_trade()
    return -(-n // limit) if n > 0 else 0


async def _run_order(start: "Callable[..., Any]", finish: "Callable[..., Any]",
                     guild_id: str, player_id: str, mtgo_user: str,
                     n: int, *, commit: bool, wait_minutes: int) -> "dict[str, Any]":
    """One order, as many trades as it takes, run one at a time.

    Shared by deposit and withdraw because the loop is the same for both: what
    genuinely differs between them -- committing tix to in-flight, returning
    them -- lives inside their own start/finish pair, not here.

    ``moved`` counts only chunks that finished. ``pending`` says the run
    stopped on a trade that is still OPEN rather than one that failed, which
    the caller must keep apart: the watchdog will settle a pending trade, so
    telling the player to send that part again would have them send it twice.
    ``review`` says it stopped on a chunk held for an admin, and ``open`` is then
    that chunk's size: neither moved nor back in the wallet, so the caller must
    not describe it as either.
    """
    if trade_count(n) > MAX_TRADES_PER_ORDER:
        return {"moved": 0, "jobs": [],
                "error": (f"That would take {trade_count(n)} separate MTGO trades. "
                          f"The most one command runs is {MAX_TRADES_PER_ORDER} "
                          f"-- split it across a few commands."),
                "busy": False, "pending": False, "review": False, "open": 0}
    moved, error, busy, pending, review, open_part = 0, None, False, False, False, 0
    jobs: "list[str]" = []
    for part in chunk_amounts(n, max_cards_per_trade()):
        started = await start(guild_id, player_id, mtgo_user, part,
                              commit=commit, wait_minutes=wait_minutes,
                              exclude_ids=jobs)
        if not started.get("ok"):
            error, busy = started.get("error"), bool(started.get("busy"))
            review = bool(started.get("review"))
            open_part = part if review else 0
            break
        jobs.append(started["job_id"])
        res = await finish(started["job_id"], guild_id, player_id, part, mtgo_user)
        if not res.get("ok"):
            pending = res.get("outcome") == "pending"
            review = res.get("outcome") == REVIEW
            # The chunk that is still open, so the caller can say how much is
            # really in that trade. Only ONE chunk is ever open -- the run
            # stops here -- and saying "the rest" counts undispatched chunks
            # the player still has to ask for.
            open_part = part if (pending or review) else 0
            error = res.get("error") or res.get("outcome")
            break
        moved += part
    return {"moved": moved, "jobs": jobs, "error": error,
            "busy": busy, "pending": pending, "review": review, "open": open_part}


async def run_deposit_order(guild_id: str, player_id: str, mtgo_user: str, n: int, *,
                            commit: bool = True,
                            wait_minutes: int = 0) -> "dict[str, Any]":
    """Deposit ``n`` tix in as many trades as the serve's limit requires.

    A convenience over running /wallet deposit several times, and it behaves
    like it: each chunk is an ordinary job that credits on its own, so a run
    that stops part-way leaves the player credited for what completed and owing
    nothing for what did not. There is no order-level state to recover and
    nothing to unwind -- the pieces were never a single thing.
    """
    return await _run_order(start_deposit, finish_deposit, guild_id, player_id,
                            mtgo_user, n, commit=commit, wait_minutes=wait_minutes)


async def run_withdraw_order(guild_id: str, player_id: str, mtgo_user: str, n: int, *,
                             commit: bool = True,
                             wait_minutes: int = 0) -> "dict[str, Any]":
    """Withdraw ``n`` tix in as many trades as the serve's limit requires.

    Safe for the same reason as the deposit side, plus one more: each chunk
    commits only its OWN tix to in-flight, at its own dispatch. A run that
    stops part-way therefore leaves the remainder in the player's balance,
    where they can ask for it again -- never committed to a trade that was
    never opened.

    The caller should check the balance covers ``n`` first. Each chunk checks
    only its own share, so an unaffordable order would really trade the first
    chunk out of the vault before the second one refused.
    """
    return await _run_order(start_withdraw, finish_withdraw, guild_id, player_id,
                            mtgo_user, n, commit=commit, wait_minutes=wait_minutes)


# ---------------------------------------------------------------------------
async def start_deposit(guild_id: str, player_id: str, mtgo_user: str, n: int, *,
                        commit: bool = True, wait_minutes: int = 0,
                        exclude_ids: "Iterable[str]" = ()) -> dict[str, Any]:
    """Enqueue a deposit (bot receives ``n`` tix from ``mtgo_user``). No wallet effect yet,
    but the job is durably recorded so it can't be stranded by a restart."""
    if n <= 0:
        return {"ok": False, "error": "amount must be positive"}
    client = get_client()
    if not client.enabled:
        return {"ok": False, "error": "MTGO TradeBot integration is disabled"}
    busy = await serve_busy_reason()   # one trade at a time; don't queue behind another
    if busy:
        return {"ok": False, "error": busy, "busy": True}
    sent_at = _post_stamp()
    resp = await client.deposit_tix(mtgo_user, n, commit=commit, wait_minutes=wait_minutes)
    if not resp or not resp.get("id"):
        # If the POST may have reached the serve with only the response lost, the trade
        # can still fire — adopt the job rather than orphan it. Definite failures skip
        # the scan and fail fast.
        adopted = await _adopt_or_none(resp, "deposit", mtgo_user, n, exclude_ids, sent_at)
        if adopted is None and _ambiguous(resp):
            # Nothing is committed for a deposit, but the POST may still create a trade
            # that moves tix with no job to credit them. Held, so it can be reconciled.
            return await _hold_lost_post(
                "deposit", guild_id, player_id, mtgo_user, n,
                f"deposit POST outcome unknown and no job found for it ({mtgo_user}, {n} tix)",
                "couldn't confirm whether the deposit was queued -- if a trade invite arrives, "
                "decline it and ask an admin")
        if adopted is None:
            return {"ok": False, "error": "serve did not accept the deposit (unreachable or rejected)"}
        resp = adopted
    await _record_job(resp["id"], "deposit", guild_id, player_id, mtgo_user, n)
    return {"ok": True, "job_id": resp["id"]}


async def finish_deposit(job_id: str, guild_id: str, player_id: str, n: int, mtgo_user: str,
                         timeout_s: float = _DEFAULT_POLL_TIMEOUT_S) -> dict[str, Any]:
    """Poll the deposit job; on 'done' credit the wallet (idempotent by job_id)."""
    outcome, job = await _poll_job(job_id, timeout_s)
    if outcome == "done":
        async def credit():
            await wallet_service.credit_done(
                guild_id, player_id, n,
                job_id=job_id, counterparty_id=mtgo_user, source="serve", notes=f"deposit {n} tix")
        if not await _settle(job_id, "done", credit):
            return await _settled_elsewhere(job_id)
        return {"ok": True, "outcome": "done", "credited": n}
    if outcome == "failed":
        if job.get("refundable") is not True:
            # Final only with the serve's proof that nothing arrived (the same `refundable`
            # proof a withdraw needs). Without it -- an unverified outcome, or a serve that
            # doesn't say -- the tix may have come: hold it for an admin rather than tell
            # the player it simply failed, and have them send the tix again.
            return await _hold_for_review(
                job_id, f"deposit of {n} tix could not be verified against the vault: "
                        f"{job.get('detail')}",
                "the trade didn't confirm cleanly -- an admin will check whether your tix "
                "arrived and credit them if they did")
        if not await _settle(job_id, "failed"):
            return await _settled_elsewhere(job_id)
        return {"ok": False, "outcome": "failed", "error": job.get("detail") or "trade failed"}
    return {"ok": False, "outcome": "pending"}


# ---------------------------------------------------------------------------
# withdraw (bot gives tix) — commit to in-flight, then cross the boundary or return
# ---------------------------------------------------------------------------
async def _return_in_flight(guild_id: str, player_id: str, n: int, key: str):
    """Give a committed-but-undelivered withdraw back to the player. Idempotent by
    ``key`` — the one place that names the reversal, so the abort path and the
    failed-job path can't drift into two different (and therefore replayable) keys."""
    await wallet_service.pay(
        guild_id, wallet_service.SYSTEM_IN_FLIGHT, player_id, n,
        source=f"return:{key}", notes=f"withdraw {n} tix returned")
    # An inflow like any other; no DM, the withdrawing player is watching their own
    # command's reply.
    await on_inflow(guild_id, player_id)


async def on_inflow(guild_id: str, player_id: str,
                    notifier: Callable[..., Any] | None = None,
                    *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    """Call this from every path that puts tix into somebody's wallet. The only thing
    such a path should have to know.

    It owns the whole concept: tell the recipient, then apply the money to what they
    owe. That order is not incidental -- it is what makes their two DMs read in the
    order the money actually moved ("you received 5", then "3 went to your creditor").
    Skipping system holders and never raising come with it, from settle_inflow.

    ``notifier`` is a notification_service notify_* function, called as
    ``notifier(bot, guild_id, player_id, *args, **kwargs)`` -- the shape every wallet
    notifier already has. Omit it where the recipient already sees the outcome
    synchronously (a returned withdraw, a deposit's own confirmation message).

    WHY THIS IS A CALL AND NOT A HOOK on wallet_service's writers, which would remove
    the need to remember it entirely: a debt settlement is itself a credit to the
    creditor, written by transfer_in from INSIDE the caller's MONEY_LOCK. Firing this
    there would re-enter a non-reentrant lock, and would turn every settlement into a
    cascade down the creditor chain that auto_draw deliberately refuses to do. So the
    inflow paths call it, and this docstring is the reason a new one should too.
    """
    if notifier is not None:
        from notification_service import notify_wallet
        await notify_wallet(notifier, guild_id, player_id, *args, **kwargs)
    return await settle_inflow(guild_id, player_id)


async def settle_deposit_inflow(guild_id: str, player_id: str):
    """Apply a landed deposit, in the order the player intended.

    Their own pending tournament entry has first claim: they committed to it, so the fee
    shouldn't be siphoned to a creditor on the way in. Whatever is left then settles
    against what they owe. Returns (completed_entries, settlements).

    Both deposit completion paths go through here — the live /wallet deposit followup and
    the watchdog's resume of a job that finished late. The resume path used to book the
    credit and stop, so a deposit that completed slowly was never drawn against debts at
    all; keeping the order in one function is what stops the two paths disagreeing again.
    """
    from services import tournament_escrow_service as escrow
    completed = await escrow.sweep_pending_entries(player_id)
    drawn = await on_inflow(guild_id, player_id)
    return completed, drawn


async def start_withdraw(guild_id: str, player_id: str, mtgo_user: str, n: int, *,
                         commit: bool = True, wait_minutes: int = 0,
                         exclude_ids: "Iterable[str]" = ()) -> dict[str, Any]:
    """Transfer ``n`` tix from the player to ``system:in-flight`` (atomic funds check),
    then enqueue the give. While the trade is open those tix belong to in-flight, so
    they're unspendable — no status, no special-casing in any balance query.

    The commitment is returned to the player ONLY when we're sure the serve never created
    the job — an ambiguous POST failure keeps it committed (the trade may still fire) and
    adopts the job from the serve's list when it can."""
    if n <= 0:
        return {"ok": False, "error": "amount must be positive"}
    client = get_client()
    if not client.enabled:
        return {"ok": False, "error": "MTGO TradeBot integration is disabled"}
    busy = await serve_busy_reason()   # matters more here: this path commits tix first
    if busy:
        return {"ok": False, "error": busy, "busy": True}

    # unique per attempt so a player can open a second withdraw later
    commit_key = uuid.uuid4().hex
    try:
        await wallet_service.pay(
            guild_id, player_id, wallet_service.SYSTEM_IN_FLIGHT, n,
            source=f"wd:{commit_key}", notes=f"withdraw {n} tix to {mtgo_user}")
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    sent_at = _post_stamp()
    resp = await client.withdraw_tix(mtgo_user, n, commit=commit, wait_minutes=wait_minutes)
    if not resp or not resp.get("id"):
        adopted = await _adopt_or_none(resp, "request", mtgo_user, n, exclude_ids, sent_at)
        if adopted is None and _ambiguous(resp):
            # The POST may have created a trade -- possibly after we looked -- and nothing
            # proves it didn't. A refund now pays twice if it did, so the tix stay
            # committed, held for an admin.
            return await _hold_lost_post(
                "withdraw", guild_id, player_id, mtgo_user, n,
                f"withdraw POST outcome unknown and no job found for it "
                f"({mtgo_user}, {n} tix committed, commit wd:{commit_key})",
                "couldn't confirm whether the withdraw was queued -- your tix are held "
                "safely until an admin checks it")
        if adopted is None:
            # A DEFINITE rejection: the serve refused it or was never reached, so no trade
            # can exist -- the one failure that is its own proof. Give the tix back.
            await _return_in_flight(guild_id, player_id, n, f"wd:{commit_key}")
            return {"ok": False, "error": "serve did not accept the withdraw (unreachable or rejected)"}
        resp = adopted
    await _record_job(resp["id"], "withdraw", guild_id, player_id, mtgo_user, n)
    return {"ok": True, "job_id": resp["id"]}


async def finish_withdraw(job_id: str, guild_id: str, player_id: str, n: int,
                          mtgo_user: str | None = None,
                          timeout_s: float = _DEFAULT_POLL_TIMEOUT_S) -> dict[str, Any]:
    """Poll the withdraw job. On 'done' the tix physically left the vault, so book the
    boundary debit against in-flight (idempotent by job_id). On 'failed' transfer them
    back to the player (idempotent by job_id too). On timeout they stay committed to
    in-flight — the watchdog resolves it later."""
    outcome, job = await _poll_job(job_id, timeout_s)
    if outcome == "done":
        async def debit():
            await wallet_service.debit_done(
                guild_id, wallet_service.SYSTEM_IN_FLIGHT, n, job_id=job_id,
                counterparty_id=mtgo_user, notes=f"withdraw {n} tix delivered to {mtgo_user}")
        if not await _settle(job_id, "done", debit):
            return await _settled_elsewhere(job_id)
        return {"ok": True, "outcome": "done"}
    if outcome == "failed":
        # REFUND ONLY WITH PROOF. The serve marks a failure refundable only when nothing can
        # have moved: its final approve never went out, or its vault showed no change once
        # the trade was over. 2026-10-03: a withdraw that had left the vault came back
        # "failed", the refund paid the player twice. Without the proof the tix stay in
        # in-flight -- where a refund can still happen once an admin checks the vault, but a
        # double payment can't.
        if job.get("refundable") is True:
            if not await _settle(job_id, "failed",
                                 lambda: _return_in_flight(guild_id, player_id, n, job_id)):
                return await _settled_elsewhere(job_id)
            return {"ok": False, "outcome": "failed", "error": job.get("detail") or "trade failed"}
        return await _hold_for_review(
            job_id, f"withdraw of {n} tix reported failed without proof nothing moved "
                    f"(vault={job.get('vault')!r}): {job.get('detail')}",
            "the trade didn't confirm cleanly -- your tix are held safely until an admin "
            "checks whether they were delivered")
    return {"ok": False, "outcome": "pending"}


# ---------------------------------------------------------------------------
# pending-jobs watchdog — finish booking any job whose poller died
# (poll timeout / bot restart / gateway reconnect)
# ---------------------------------------------------------------------------
_RESCAN_INTERVAL_S = 10 * 60
_watchdog_running = False   # on_ready refires on gateway reconnects; start one loop only
_polling_jobs: set[str] = set()  # job_ids with a live resumed poller — rescans skip them


async def resume_pending_jobs() -> int:
    """Spawn a poller for every 'pending' MtgoJob that doesn't already have one, booking
    its ledger side when the job reaches a terminal state. Booking is idempotent
    (job_id unique index), so racing a still-live command poller is safe. Returns the
    number of jobs picked up."""
    await _finish_settlements()
    async with db_session() as session:
        pending = (await session.execute(
            select(MtgoJob).where(MtgoJob.status == "pending"))).scalars().all()
    pending = [j for j in pending if j.job_id not in _polling_jobs]
    if not pending:
        return 0

    async def _resume(job: MtgoJob):
        try:
            if job.kind == "deposit":
                res = await finish_deposit(job.job_id, job.guild_id, job.player_id,
                                           job.amount, job.mtgo_user)
                if res.get("ok"):
                    # A job that finished late lands here instead of in the command's
                    # own followup, so it has to apply the deposit the same way — the
                    # watchdog's own escrow sweep runs before these pollers finish, so
                    # it cannot be relied on to catch this player's entry.
                    await settle_deposit_inflow(job.guild_id, job.player_id)
            elif job.kind == "withdraw":
                await finish_withdraw(job.job_id, job.guild_id, job.player_id,
                                      job.amount, job.mtgo_user)
        finally:
            _polling_jobs.discard(job.job_id)

    for job in pending:
        _polling_jobs.add(job.job_id)
        spawn_followup(f"resume {job.kind} {job.job_id}", _resume(job))
    logger.info(f"resume_pending_jobs: picked up {len(pending)} unresolved MTGO job(s)")
    return len(pending)


# Long enough that a live settler has finished; a booking is a couple of DB writes.
_STALE_SETTLEMENT_S = 5 * 60


async def _finish_settlements() -> int:
    """Complete tix settlements a process died in the middle of: claimed for an outcome,
    booking not confirmed. Re-running a booking is safe -- each is idempotent by its key
    (job_id for the boundary rows, return:<job_id> for a refund) -- so a booking that DID
    land is not repeated, and one that didn't is made."""
    cutoff = datetime.now() - timedelta(seconds=_STALE_SETTLEMENT_S)
    async with db_session() as session:
        rows = (await session.execute(select(MtgoJob).where(
            MtgoJob.status.like(f"{_SETTLING}%"),
            MtgoJob.kind.in_(("deposit", "withdraw")),
            MtgoJob.resolved_at < cutoff))).scalars().all()
    done = 0
    for job in rows:
        target = job.status[len(_SETTLING):]
        try:
            if job.kind == "deposit" and target == "done":
                await wallet_service.credit_done(
                    job.guild_id, job.player_id, job.amount, job_id=job.job_id,
                    counterparty_id=job.mtgo_user, source="serve", notes=f"deposit {job.amount} tix")
            elif job.kind == "withdraw" and target == "done":
                await wallet_service.debit_done(
                    job.guild_id, wallet_service.SYSTEM_IN_FLIGHT, job.amount, job_id=job.job_id,
                    counterparty_id=job.mtgo_user, notes=f"withdraw {job.amount} tix delivered to {job.mtgo_user}")
            elif job.kind == "withdraw" and target == "failed":
                await _return_in_flight(job.guild_id, job.player_id, job.amount, job.job_id)
            if job.kind == "deposit" and target == "done":
                # Entry before debts, as the live and resumed deposit paths do -- and
                # BEFORE the row goes final, so a crash in between re-runs both (the
                # credit is idempotent and the sweep draws only what is there).
                await settle_deposit_inflow(job.guild_id, job.player_id)
            await _move_status(job.job_id, job.status, target)
            logger.warning(f"finished an interrupted settlement: {job.kind} {job.job_id} -> {target}")
            done += 1
        except Exception as e:
            logger.error(f"could not finish settlement {job.job_id} ({job.status}): {e}")
    return done


async def pending_jobs_watchdog(bot: Any = None) -> None:
    """Run resume_pending_jobs forever, every ``_RESCAN_INTERVAL_S``.

    A single startup pass isn't enough: each poll gives up after ~14 minutes
    ('pending' outcome), but a serve job can complete later than that (a stalled
    serve that recovers, live case: 28 minutes). The rescan re-polls every
    still-pending job — skipping ones whose poller is still live — until it
    reaches a terminal state. Guarded so on_ready refiring on gateway reconnects
    can't stack duplicate loops (same pattern as run_log_reconciler)."""
    global _watchdog_running
    if _watchdog_running:
        return
    _watchdog_running = True
    while True:
        try:
            await resume_pending_jobs()
            # Funds can also land outside a tracked job (a plain /wallet deposit, a
            # teammate's /wallet pay), so finish any entry the wallet can now cover.
            from services import tournament_escrow_service as escrow
            await escrow.sweep_pending_entries()
            if bot is not None:
                # Re-render every open board, not just ones whose entries completed:
                # /wallet pay, a withdraw and a debt settlement all move a captain's
                # balance — and so the "needs N more tix" figure — by routes no
                # refresh call site covers. This tick is the backstop for all of them.
                from services.tournament_formatter import refresh_boards
                await refresh_boards(bot, await escrow.open_registration_boards())
        except Exception as e:
            logger.warning(f"pending_jobs_watchdog: rescan failed: {e}")
        await asyncio.sleep(_RESCAN_INTERVAL_S)


# ---------------------------------------------------------------------------
# internal pay (no trade)
# ---------------------------------------------------------------------------
async def _settle_owed_portion(guild_id: str, payer_id: str, creditor_id: str,
                               amount: int) -> dict[str, Any] | None:
    """Settle up to ``amount`` of what ``payer_id`` owes ``creditor_id``, or None if they
    owe nothing (or the settlement could not be applied).

    Capped at the outstanding debt because settle_debt_from_wallet refuses an amount that
    exceeds it. A failure here is deliberately soft: the caller falls through to a plain
    transfer, which runs its own funds check and surfaces the real error rather than
    turning a payment into a settlement-shaped one.
    """
    balance = await debt_service.get_balance_with(
        guild_id=guild_id, player_id=payer_id, counterparty_id=creditor_id)
    if balance >= 0:
        return None                       # owes them nothing: an ordinary payment
    res = await settle_debt_from_wallet(
        guild_id, payer_id, creditor_id, min(amount, -balance))
    if not res.get("ok"):
        logger.warning(
            f"pay: debt settle {payer_id}->{creditor_id} skipped: {res.get('error')}")
        return None
    return res


async def pay(guild_id: str, from_player: str, to_player: str, amount: int, *, notes: str | None = None) -> dict[str, Any]:
    """Move tix between two wallets with no MTGO trade (a plain claim transfer).

    Paying someone you OWE settles that debt (up to the amount owed) rather than landing
    as a plain transfer that leaves it standing. `/wallet pay` is how a debtor actually
    pays their creditor, so the ledger has to follow the money — otherwise the tix move,
    the debt does not, and the pair have to settle it a second time by hand. Anything
    above the debt moves on as an ordinary payment.

    Note the asymmetry this closes: ``on_inflow`` settles what the RECIPIENT owes, so
    before this a debtor paying their creditor directly was the one route that skipped
    settlement entirely — while merely receiving tix would have auto-settled the same debt.
    """
    cleared = await _settle_owed_portion(guild_id, from_player, to_player, amount)
    settled_amount = cleared["amount"] if cleared else 0
    rest = amount - settled_amount

    if settled_amount:
        # Tell the creditor, but do NOT run their on_inflow: a settlement must not cascade
        # down the creditor chain (the rule auto_draw states and keeps).
        from notification_service import notify_wallet, notify_payment_received
        await notify_wallet(notify_payment_received, guild_id, to_player, from_player,
                            settled_amount, note=f"settled {settled_amount} tix of debt")

    if rest <= 0:
        return {"ok": True, "amount": amount, "debt_settled": cleared, "settled": []}

    try:
        debit, credit = await wallet_service.pay(guild_id, from_player, to_player, rest, notes=notes)
    except wallet_service.InsufficientFunds as e:
        # Tagged rather than left as prose: the cog answers this one refusal with "go
        # and deposit", which would be nonsense advice for any of the others.
        return {"ok": False, "error": str(e), "debt_settled": cleared,
                "code": wallet_service.INSUFFICIENT_FUNDS, "available": e.available}
    except ValueError as e:
        return {"ok": False, "error": str(e), "debt_settled": cleared}
    # Receiving money is the case you are least likely to be watching for.
    from notification_service import notify_payment_received
    drawn = await on_inflow(guild_id, to_player, notify_payment_received,
                            from_player, rest, note=notes)
    return {"ok": True, "amount": amount, "tx_ids": [debit.id, credit.id],
            "debt_settled": cleared, "settled": drawn}


# ---------------------------------------------------------------------------
# debt settlement from wallet — wallet move + debt clear in ONE transaction
# ---------------------------------------------------------------------------
async def settle_debt_from_wallet(guild_id: str, payer_id: str, creditor_id: str, amount: int,
                                  *, link_id: str | None = None) -> dict[str, Any]:
    """Settle a tix debt from the payer's wallet: move the claim (payer −N, creditor +N)
    AND clear the debt ledger, atomically in one transaction. No MTGO trade — the tix
    never leave the vault; only the claim on them changes hands.

    Validates funds AND the outstanding debt inside the transaction, and is idempotent by
    ``link_id`` (which tags both the WalletTx pair and the DebtLedger settlement pair)."""
    if amount <= 0:
        return {"ok": False, "error": "amount must be positive"}
    if payer_id == creditor_id:
        return {"ok": False, "error": "cannot settle a debt with yourself"}
    if link_id is None:
        link_id = str(uuid.uuid4())
    wallet_source = f"debt:{link_id}"

    async def _do():
        async with db_session() as session:
            # idempotency: this settlement already applied?
            seen = await session.execute(
                select(WalletTx.id).where(WalletTx.source == wallet_source).limit(1))
            if seen.scalar():
                return {"ok": True, "amount": amount, "id": link_id, "idempotent": True}

            # payer must have the funds (settled minus reserved) — the same availability
            # formula every wallet op uses
            available = await wallet_service.balance_in(session, guild_id, payer_id)
            if amount > available:
                # Same refusal as pay()'s, tagged the same way so any caller that
                # wants to answer it with deposit advice can, without matching prose.
                return {"ok": False,
                        "error": f"insufficient wallet funds (available {available})",
                        "code": wallet_service.INSUFFICIENT_FUNDS, "available": available}

            # payer must actually owe the creditor at least this much, IN TIX. The
            # card-entity filter is not optional: on a card row ``amount`` is a count of
            # COPIES, so without it a lent card nets against money owed — a payer owing 5
            # tix who has lent 3 cards to the same person reads as owing 2, and their
            # settlement is refused as exceeding the debt, leaving real tix uncollected.
            # Every other balance read in debt_service applies the same filter.
            debt_balance = int((await session.execute(
                select(func.coalesce(func.sum(DebtLedger.amount), 0)).where(
                    DebtLedger.guild_id == guild_id,
                    DebtLedger.player_id == payer_id,
                    DebtLedger.counterparty_id == creditor_id,
                    debt_service.TIX_ONLY,
                ))).scalar() or 0)
            if debt_balance >= 0:
                return {"ok": False, "error": "no outstanding debt to this creditor"}
            owed = -debt_balance
            if amount > owed:
                return {"ok": False, "error": f"amount ({amount}) exceeds debt ({owed})"}

            note = f"Wallet debt settlement: {amount} tix"
            # 1) wallet claim move (payer -> creditor); funds checked above
            await wallet_service.transfer_in(
                session, guild_id, payer_id, creditor_id, amount, wallet_source,
                notes=note)
            # 2) debt ledger settlement (payer +amount reduces debt; creditor -amount reduces credit)
            session.add(DebtLedger(guild_id=guild_id, player_id=payer_id, counterparty_id=creditor_id,
                                   amount=amount, source_type="settlement", source_id=link_id,
                                   notes=note, created_by=payer_id, settlement_method="wallet"))
            session.add(DebtLedger(guild_id=guild_id, player_id=creditor_id, counterparty_id=payer_id,
                                   amount=-amount, source_type="settlement", source_id=link_id,
                                   notes=note, created_by=payer_id, settlement_method="wallet"))
            # single commit on exit -> wallet + debt move together or not at all
            logger.info(f"settle_debt_from_wallet: {payer_id} -> {creditor_id} {amount} tix (link {link_id})")
            return {"ok": True, "amount": amount, "payer": payer_id, "creditor": creditor_id, "id": link_id}

    async with wallet_service.MONEY_LOCK:
        return await with_db_retry(_do)


async def settle_inflow(guild_id: str, player_id: str,
                        occasion: str = "an inflow") -> list[dict[str, Any]]:
    """Run ``auto_draw`` for a holder, guarded. Call this from every inflow path, not just
    deposits — and from ``settle_new_debts`` for the mirror case, a debt arriving at
    someone who already holds tix (``occasion`` only names the trigger in the error log).

    Deposits alone are not enough: a payout, a refund, a player-to-player payment or a
    returned withdraw all credit a wallet without going near the deposit path, so a debtor
    could be paid a tournament prize and withdraw it straight back out while their creditor
    was still owed. Settlement on the way IN is what makes the debt collectable.

    TWO RULES, both load-bearing:

    * **Never call this while holding ``MONEY_LOCK``.** It reaches ``settle_debt_from_wallet``,
      which takes that lock, and the lock is not reentrant — see the note on its definition.
      So call it AFTER the ``async with wallet_service.MONEY_LOCK:`` block that moved the
      money, never inside one.
    * **Never let it break the inflow.** The transfer has already committed by the time we
      get here; failing to settle afterwards must not turn a successful payout into an
      error. Failures are logged and swallowed.

    Synthetic holders (prize wallets, in-flight) are skipped — they hold claims but have no
    debts, and drawing against them is meaningless."""
    if wallet_service.is_system_account(player_id):
        return []
    try:
        return await auto_draw(guild_id, player_id)
    except Exception as e:  # noqa: BLE001 - settlement must never fail its trigger
        logger.error(f"settle_inflow: auto_draw failed for {player_id} after {occasion}: {e}")
        return []


async def settle_new_debts(guild_id: str, debtor_ids: Iterable[str]) -> list[dict[str, Any]]:
    """Draw brand-new debts against what their debtors ALREADY hold.

    The mirror of ``settle_inflow``: that fires when money reaches someone who owes, this
    when a debt reaches someone who has money. Without it, losing a staked draft while
    holding tix left paying up to the debtor's discretion — and the whole point of a
    wallet-backed ledger is that it isn't discretionary.

    De-duplicated because ``auto_draw`` settles a debtor's WHOLE book in one pass, so a
    loser who owes three people needs one pass, not three.
    """
    ids = list(dict.fromkeys(debtor_ids))
    if not ids:
        return []
    # One batched balance read, then only the funded debtors do any work. Most draft
    # losers have an empty wallet and nothing to draw against, and auto_draw would spend
    # two sessions apiece just to discover that.
    balances = await wallet_service.balances_for(guild_id, ids)
    funded = [pid for pid in ids if balances.get(pid, 0) > 0]
    if not funded:
        return []
    # Concurrent on purpose: MONEY_LOCK covers only each settlement's own transaction, so
    # the balance reads and the two settlement DMs per creditor run lock-free and would
    # otherwise just add their round-trips together. Safe because each debtor's failures
    # are swallowed independently and settle_debt_from_wallet re-checks the live debt and
    # balance INSIDE the lock, so overlapping work converges instead of over-paying.
    drawn = await asyncio.gather(*(
        settle_inflow(guild_id, pid, "a new debt") for pid in funded))
    return [settlement for per_debtor in drawn for settlement in per_debtor]


async def settle_draft_winnings(guild_id: str, paid: dict[str, int],
                                draft_name: str) -> None:
    """Land a draft's payouts on their winners: settle, and tell those who want telling.

    The inflow-side twin of ``settle_new_debts``: that one draws brand-new debts against
    money a player already holds, this one draws already-held debts against money that
    just arrived. Both run from ``settle_decided_draft``, one per branch.

    A prize is an INFLOW, so it goes through ``on_inflow`` rather than dispatching a DM
    directly -- that is what stops a debtor collecting their winnings and withdrawing
    them while their creditor is still owed, the case ``settle_inflow`` names as its
    reason for existing, and what ``tournament_escrow_service.execute_payout`` already
    does for the tournament prize.

    Settlement runs for EVERY winner; only the DM honours the dm_notifications opt-out.
    One loop does both, and what keeps them independent is the notifier ARGUMENT: an
    opted-out winner is passed None, so ``on_inflow`` settles their debts and sends
    nothing.

    Call AFTER the caller's ``MONEY_LOCK`` block, never inside it: ``on_inflow`` reaches
    ``settle_debt_from_wallet``, which takes that lock, and it is not reentrant.
    """
    if not paid:
        return
    # Deferred to keep the import cycle broken from both ends, the way on_inflow defers
    # its notify_wallet import and execute_payout defers this same notifier.
    from notification_service import notify_draft_winnings

    # Both reads feed the DM ONLY, so neither may cost anyone their settlement. Losing
    # prefs means we cannot tell who opted out, and the safe reading of an unknown
    # preference is to stay quiet: the money is what must not be skipped. Without this
    # the failure is unrecoverable -- settle_decided_draft skips the pool branch for
    # good once the holder is empty, so nothing ever retries the sweep.
    # Empty is the safe starting value, so a read that fails -- or returns nothing,
    # which the shared execute_db_operation wrapper does by handing back its default --
    # leaves the loop below sending no DM rather than raising outside this guard.
    prefs: dict[str, bool] = {}
    balances: dict[str, int] = {}
    try:
        prefs = await get_players_dm_notification_preferences(list(paid), guild_id) or {}
        balances = await wallet_service.balances_for(guild_id, list(paid)) or {}
    except Exception as e:  # noqa: BLE001 - a DM must never break the money path
        logger.error(f"draft winnings: DM prep failed for {draft_name}, "
                     f"settling {len(paid)} winner(s) silently: {e}")

    # All winners at once, the same shape settle_new_debts uses on this very path.
    # return_exceptions so one winner's settlement failing cannot abandon the rest --
    # they are independent debts owed to different creditors.
    results = await asyncio.gather(*(
        on_inflow(guild_id, player_id,
                  notify_draft_winnings if prefs.get(player_id) else None,
                  credited, draft_name, balances.get(player_id, 0))
        for player_id, credited in paid.items()), return_exceptions=True)
    for player_id, result in zip(paid, results):
        if isinstance(result, BaseException):
            logger.error(f"draft winnings: settling {player_id} for {draft_name} "
                         f"failed: {result}")


async def auto_draw(guild_id: str, player_id: str) -> list[dict[str, Any]]:
    """Apply a player's wallet balance to their outstanding debts, oldest debt first,
    until the wallet is exhausted or the debts are cleared. Returns the settlements made.

    Prefer ``settle_inflow`` from inflow paths — it adds the system-account guard and the
    never-break-the-caller contract. Call this directly only where those do not apply.

    Each settlement is atomic and re-checks the live debt, so a concurrent or repeated run
    simply draws against whatever remains — it converges, never over-pays.

    NOTE it deliberately does NOT cascade: paying a creditor may leave THEM able to settle
    their own debts, but chaining that here would recurse through an unbounded number of
    parties inside one command. Each creditor settles on their own next inflow instead."""
    available = await wallet_service.get_balance(guild_id, player_id)
    if available <= 0:
        return []
    balances = await debt_service.get_all_balances_for(guild_id, player_id)
    owed = {cp: -bal for cp, bal in balances.items() if bal < 0}  # positive amounts owed
    if not owed:
        return []

    # order creditors by the age of the oldest still-active debt entry (oldest first);
    # the per-creditor lookups are independent reads, so run them concurrently
    creditors = list(owed)
    entry_lists = await asyncio.gather(*(
        debt_service.get_active_debt_entries(guild_id, player_id, cp) for cp in creditors))
    ordered: list[tuple[datetime | None, str]] = [
        (entries[-1].created_at if entries else None, cp)  # entries are newest-first
        for cp, entries in zip(creditors, entry_lists)
    ]
    def _oldest_first(entry: tuple[datetime | None, str]):
        # A creditor with no active entry has no age, and sorts last.
        return (entry[0] is None, entry[0])

    ordered.sort(key=_oldest_first)

    settlements = []
    for _, cp in ordered:
        if available <= 0:
            break
        pay_amt = min(available, owed[cp])
        if pay_amt <= 0:
            continue
        res = await settle_debt_from_wallet(guild_id, player_id, cp, pay_amt)
        if res.get("ok"):
            available -= pay_amt
            settlements.append(res)
            # Neither party asked for this — it fired off a deposit — so tell both.
            # Reports what is STILL owed after this leg, not the original debt.
            # Not on an idempotent replay: that moved no money, and announcing it would
            # tell two people they were paid again. Mirrors execute_payout's
            # already_paid guard. Unreachable while auto_draw passes no link_id, which
            # is exactly when a guard is cheap to add.
            if not res.get("idempotent"):
                from notification_service import notify_wallet, notify_auto_settlement
                await notify_wallet(
                    notify_auto_settlement, guild_id, player_id, cp, pay_amt,
                    remaining=max(0, owed[cp] - pay_amt))
        else:
            logger.warning(f"auto_draw: settle {player_id}->{cp} {pay_amt} skipped: {res.get('error')}")
    if settlements:
        logger.info(f"auto_draw: {player_id} settled {len(settlements)} debt(s) from wallet")
    return settlements


# ---------------------------------------------------------------------------
# reconciliation passthrough (physical vault tix == SUM settled wallets)
# ---------------------------------------------------------------------------
async def reconcile() -> dict[str, Any]:
    """Pull the vault's live tix from the serve and compare to the wallet claim total."""
    client = get_client()
    bot_tix = await client.bot_tix()
    if bot_tix is None:
        return {"ok": False, "error": "could not read vault tix from serve"}
    return await wallet_service.reconcile(bot_tix)
