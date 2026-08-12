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
from datetime import datetime

from loguru import logger
from sqlalchemy import select, func

from database.db_session import db_session
from database.retry import with_db_retry
from models.mtgo_job import MtgoJob
from models.wallet_tx import WalletTx
from models.debt_ledger import DebtLedger
from helpers.money_gate import serve_busy_reason, spawn_followup
from services.mtgo_tradebot_client import get_client
from services import wallet_service
from services import debt_service

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


async def _resolve_job(job_id: str, status: str):
    async def _do():
        async with db_session() as session:
            job = await session.get(MtgoJob, job_id)
            if job is not None and job.status == "pending":
                job.status = status
                job.resolved_at = datetime.now()
    await with_db_retry(_do)


async def _recover_lost_job(resp, job_type: str, mtgo_user: str, n: int):
    """Recovery for a failed job POST. Runs the /jobs adoption scan ONLY when the client
    flagged the failure as ambiguous (delivered-but-response-lost is possible); a definite
    rejection or never-connected error returns None immediately — no job can exist, and
    the caller may fail fast. One retry covers a brief flap."""
    if not (resp and resp.get("_ambiguous")):
        return None
    client = get_client()
    job = await client.find_recent_job(job_type, mtgo_user, n)
    if job is None:
        await asyncio.sleep(2)
        job = await client.find_recent_job(job_type, mtgo_user, n)
    return job


# ---------------------------------------------------------------------------
# deposit (bot receives tix) — credit only on 'done'
# ---------------------------------------------------------------------------
async def start_deposit(guild_id: str, player_id: str, mtgo_user: str, n: int, *,
                        commit: bool = True, wait_minutes: int = 0) -> dict:
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
    resp = await client.deposit_tix(mtgo_user, n, commit=commit, wait_minutes=wait_minutes)
    if not resp or not resp.get("id"):
        # If the POST may have reached the serve with only the response lost, the trade
        # can still fire — adopt the job rather than orphan it. Definite failures skip
        # the scan and fail fast.
        resp = await _recover_lost_job(resp, "deposit", mtgo_user, n)
        if not resp or not resp.get("id"):
            return {"ok": False, "error": "serve did not accept the deposit (unreachable or rejected)"}
        logger.warning(f"start_deposit: adopted job {resp['id']} after lost POST response")
    await _record_job(resp["id"], "deposit", guild_id, player_id, mtgo_user, n)
    return {"ok": True, "job_id": resp["id"]}


async def finish_deposit(job_id: str, guild_id: str, player_id: str, n: int, mtgo_user: str,
                         timeout_s: float = _DEFAULT_POLL_TIMEOUT_S) -> dict:
    """Poll the deposit job; on 'done' credit the wallet (idempotent by job_id)."""
    outcome, job = await _poll_job(job_id, timeout_s)
    if outcome == "done":
        await wallet_service.credit_done(
            guild_id, player_id, n,
            job_id=job_id, counterparty_id=mtgo_user, source="serve", notes=f"deposit {n} tix")
        await _resolve_job(job_id, "done")
        return {"ok": True, "outcome": "done", "credited": n}
    if outcome == "failed":
        await _resolve_job(job_id, "failed")
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


async def start_withdraw(guild_id: str, player_id: str, mtgo_user: str, n: int, *,
                         commit: bool = True, wait_minutes: int = 0) -> dict:
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

    resp = await client.withdraw_tix(mtgo_user, n, commit=commit, wait_minutes=wait_minutes)
    if not resp or not resp.get("id"):
        resp = await _recover_lost_job(resp, "request", mtgo_user, n)
        if not resp or not resp.get("id"):
            # Definite rejection, or an ambiguous failure whose job-list scan shows no
            # job — either way no trade can have been opened; give the tix back.
            await _return_in_flight(guild_id, player_id, n, f"wd:{commit_key}")
            return {"ok": False, "error": "serve did not accept the withdraw (unreachable or rejected)"}
        logger.warning(f"start_withdraw: adopted job {resp['id']} after lost POST response")
    await _record_job(resp["id"], "withdraw", guild_id, player_id, mtgo_user, n)
    return {"ok": True, "job_id": resp["id"]}


async def finish_withdraw(job_id: str, guild_id: str, player_id: str, n: int,
                          mtgo_user: str = None,
                          timeout_s: float = _DEFAULT_POLL_TIMEOUT_S) -> dict:
    """Poll the withdraw job. On 'done' the tix physically left the vault, so book the
    boundary debit against in-flight (idempotent by job_id). On 'failed' transfer them
    back to the player (idempotent by job_id too). On timeout they stay committed to
    in-flight — the watchdog resolves it later."""
    outcome, job = await _poll_job(job_id, timeout_s)
    if outcome == "done":
        await wallet_service.debit_done(
            guild_id, wallet_service.SYSTEM_IN_FLIGHT, n, job_id=job_id,
            counterparty_id=mtgo_user, notes=f"withdraw {n} tix delivered to {mtgo_user}")
        await _resolve_job(job_id, "done")
        return {"ok": True, "outcome": "done"}
    if outcome == "failed":
        await _return_in_flight(guild_id, player_id, n, job_id)
        await _resolve_job(job_id, "failed")
        return {"ok": False, "outcome": "failed", "error": job.get("detail") or "trade failed"}
    return {"ok": False, "outcome": "pending"}


# ---------------------------------------------------------------------------
# pending-jobs watchdog — finish booking any job whose poller died
# (poll timeout / bot restart / gateway reconnect)
# ---------------------------------------------------------------------------
_RESCAN_INTERVAL_S = 10 * 60
_watchdog_running = False   # on_ready refires on gateway reconnects; start one loop only
_polling_jobs: set = set()  # job_ids with a live resumed poller — rescans skip them


async def resume_pending_jobs() -> int:
    """Spawn a poller for every 'pending' MtgoJob that doesn't already have one, booking
    its ledger side when the job reaches a terminal state. Booking is idempotent
    (job_id unique index), so racing a still-live command poller is safe. Returns the
    number of jobs picked up."""
    async with db_session() as session:
        pending = (await session.execute(
            select(MtgoJob).where(MtgoJob.status == "pending"))).scalars().all()
    pending = [j for j in pending if j.job_id not in _polling_jobs]
    if not pending:
        return 0

    async def _resume(job: MtgoJob):
        try:
            if job.kind == "deposit":
                # Booking the credit is all that's needed: anything waiting on those
                # funds (a pending tournament entry) is completed by the escrow sweep
                # on the same watchdog tick.
                await finish_deposit(job.job_id, job.guild_id, job.player_id,
                                     job.amount, job.mtgo_user)
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


async def pending_jobs_watchdog():
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
        except Exception as e:
            logger.warning(f"pending_jobs_watchdog: rescan failed: {e}")
        await asyncio.sleep(_RESCAN_INTERVAL_S)


# ---------------------------------------------------------------------------
# internal pay (no trade)
# ---------------------------------------------------------------------------
async def pay(guild_id: str, from_player: str, to_player: str, amount: int, *, notes: str = None) -> dict:
    """Move tix between two wallets with no MTGO trade (a plain claim transfer)."""
    try:
        debit, credit = await wallet_service.pay(guild_id, from_player, to_player, amount, notes=notes)
        return {"ok": True, "amount": amount, "tx_ids": [debit.id, credit.id]}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# debt settlement from wallet — wallet move + debt clear in ONE transaction
# ---------------------------------------------------------------------------
async def settle_debt_from_wallet(guild_id: str, payer_id: str, creditor_id: str, amount: int,
                                  *, link_id: str = None) -> dict:
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
                return {"ok": False, "error": f"insufficient wallet funds (available {available})"}

            # payer must actually owe the creditor at least this much
            debt_balance = int((await session.execute(
                select(func.coalesce(func.sum(DebtLedger.amount), 0)).where(
                    DebtLedger.guild_id == guild_id,
                    DebtLedger.player_id == payer_id,
                    DebtLedger.counterparty_id == creditor_id,
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
                                   notes=note, created_by=payer_id))
            session.add(DebtLedger(guild_id=guild_id, player_id=creditor_id, counterparty_id=payer_id,
                                   amount=-amount, source_type="settlement", source_id=link_id,
                                   notes=note, created_by=payer_id))
            # single commit on exit -> wallet + debt move together or not at all
            logger.info(f"settle_debt_from_wallet: {payer_id} -> {creditor_id} {amount} tix (link {link_id})")
            return {"ok": True, "amount": amount, "payer": payer_id, "creditor": creditor_id, "id": link_id}

    async with wallet_service.MONEY_LOCK:
        return await with_db_retry(_do)


async def auto_draw(guild_id: str, player_id: str) -> list[dict]:
    """Apply a player's wallet balance to their outstanding debts, oldest debt first,
    until the wallet is exhausted or the debts are cleared. Returns the settlements made.

    Called after a deposit (fresh funds) or after a debt is booked (funds already there).
    Each settlement is atomic and re-checks the live debt, so a concurrent or repeated run
    simply draws against whatever remains — it converges, never over-pays."""
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
    ordered = [
        (entries[-1].created_at if entries else None, cp)  # entries are newest-first
        for cp, entries in zip(creditors, entry_lists)
    ]
    ordered.sort(key=lambda t: (t[0] is None, t[0]))

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
        else:
            logger.warning(f"auto_draw: settle {player_id}->{cp} {pay_amt} skipped: {res.get('error')}")
    if settlements:
        logger.info(f"auto_draw: {player_id} settled {len(settlements)} debt(s) from wallet")
    return settlements


# ---------------------------------------------------------------------------
# reconciliation passthrough (physical vault tix == SUM settled wallets)
# ---------------------------------------------------------------------------
async def reconcile() -> dict:
    """Pull the vault's live tix from the serve and compare to the wallet claim total."""
    client = get_client()
    bot_tix = await client.bot_tix()
    if bot_tix is None:
        return {"ok": False, "error": "could not read vault tix from serve"}
    return await wallet_service.reconcile(bot_tix)
