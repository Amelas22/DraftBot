"""
Tix wallet service — the obligation/claim ledger over ``WalletTx``.

A player's wallet is a *view* over their transaction log (there is no stored balance):
    balance   = SUM(amount WHERE status='done')          # settled claim on the vault
    reserved  = -SUM(amount WHERE status='pending')      # withdraws in flight (debits)
    available = balance - reserved                        # what they can actually spend

Design mirrors ``services/debt_service.py``: single ``db_session()`` per op, idempotency
keyed on a stable id (``job_id`` for serve ops, ``source`` for internal pays), and
retry-with-backoff on transient SQLite "database is locked" errors.

Write rules (from the plan):
  * Credits (deposit / receive) are written ONLY as 'done', and only once their MTGO job
    has completed — never on enqueue. Idempotent by ``job_id``.
  * A withdraw is a two-step reserve→settle: ``reserve_debit`` immediately writes a
    'pending' debit (protecting the balance from double-spend), then the job poller flips
    it to 'done' (``settle_reserve``) or 'cancelled' (``cancel_reserve``, releasing it).
  * Internal ``pay`` moves tix between two players with no MTGO trade: two 'done' rows in
    one transaction, idempotent by ``source``.

Reconciliation: ``reconcile()`` asserts bot's physical vault tix == SUM of all 'done'
amounts (the audit that the claim ledger never invents value).
"""
import asyncio
import uuid
from dataclasses import dataclass
from loguru import logger
from sqlalchemy import select, func, case
from sqlalchemy.exc import IntegrityError

from database.db_session import db_session
from database.retry import with_db_retry
from models.wallet_tx import WalletTx

VALID_KINDS = {"deposit", "withdraw", "pay", "receive", "adjust", "escrow"}

# Serializes every DEBIT (check-available-then-spend) across the process. The bot is the
# database's only writer, so this lock is sufficient to prevent two concurrent debits from
# both reading the same balance and both spending it. Held only around short transactions;
# credits don't need it (they can't overdraw, and double-booking is blocked by the
# uq_wallet_tx_* unique indexes). NOT reentrant — acquire only at service entry points,
# never from code already running under it.
MONEY_LOCK = asyncio.Lock()


@dataclass
class Wallet:
    guild_id: str
    player_id: str
    balance: int      # settled
    reserved: int     # pending withdraws (>=0)
    available: int    # balance - reserved


async def _sum_amount(session, *conditions) -> int:
    q = select(func.coalesce(func.sum(WalletTx.amount), 0)).where(*conditions)
    result = await session.execute(q)
    return int(result.scalar() or 0)


async def _pending_reserve(session, guild_id: str, player_id: str, source: str) -> WalletTx | None:
    """The player's live ('pending') reserve carrying this exact ``source`` tag, or None —
    the one definition of 'a live reserve' (kept next to _balances so every WalletTx
    predicate lives here; the uq_wallet_tx_live_escrow index enforces the same shape)."""
    result = await session.execute(
        select(WalletTx).where(
            WalletTx.guild_id == guild_id,
            WalletTx.player_id == player_id,
            WalletTx.source == source,
            WalletTx.status == "pending",
        ).order_by(WalletTx.id).limit(1)
    )
    return result.scalars().first()


async def _balances(session, guild_id: str, player_id: str) -> tuple[int, int]:
    """(balance, reserved) inside an existing session/transaction — one grouped query."""
    q = select(
        func.coalesce(func.sum(case((WalletTx.status == "done", WalletTx.amount), else_=0)), 0),
        func.coalesce(func.sum(case((WalletTx.status == "pending", WalletTx.amount), else_=0)), 0),
    ).where(
        WalletTx.guild_id == guild_id,
        WalletTx.player_id == player_id,
        WalletTx.status.in_(("done", "pending")),
    )
    balance, pending = (await session.execute(q)).one()
    return int(balance), -int(pending)  # pending debits are negative -> reserved is positive


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------
async def get_balance(guild_id: str, player_id: str) -> int:
    async with db_session() as session:
        balance, _ = await _balances(session, guild_id, player_id)
        return balance


async def get_available(guild_id: str, player_id: str) -> int:
    async with db_session() as session:
        balance, reserved = await _balances(session, guild_id, player_id)
        return balance - reserved


async def get_wallet(guild_id: str, player_id: str) -> Wallet:
    async with db_session() as session:
        balance, reserved = await _balances(session, guild_id, player_id)
        return Wallet(guild_id, player_id, balance, reserved, balance - reserved)


async def total_wallets() -> int:
    """SUM of all settled balances — the claim side of the reconciliation invariant.
    Global on purpose: the physical vault (one MTGO custodian) is shared across guilds."""
    async with db_session() as session:
        return await _sum_amount(session, WalletTx.status == "done")


async def get_history(guild_id: str, player_id: str, limit: int = 25) -> list[WalletTx]:
    limit = min(limit, 100)
    async with db_session() as session:
        query = (
            select(WalletTx)
            .where(WalletTx.guild_id == guild_id, WalletTx.player_id == player_id)
            .order_by(WalletTx.created_at.desc(), WalletTx.id.desc())
            .limit(limit)
        )
        result = await session.execute(query)
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# credits (deposit / receive) — written only as 'done', idempotent by job_id
# ---------------------------------------------------------------------------
async def credit_done(
    guild_id: str,
    player_id: str,
    amount: int,
    kind: str,
    *,
    job_id: str = None,
    counterparty_id: str = None,
    source: str = None,
    notes: str = None,
) -> WalletTx:
    """Book a settled credit (+amount). Idempotent by ``job_id`` when provided —
    a replayed deposit-job poll returns the existing row instead of double-crediting."""
    if amount <= 0:
        raise ValueError("Credit amount must be positive")
    if kind not in VALID_KINDS:
        raise ValueError(f"Invalid kind: {kind}")

    async def _do():
        async with db_session() as session:
            if job_id:
                existing = await session.execute(
                    select(WalletTx).where(
                        WalletTx.job_id == job_id,
                        WalletTx.kind == kind,
                        WalletTx.status == "done",
                    )
                )
                row = existing.scalars().first()
                if row:
                    logger.info(f"credit_done: job {job_id} already booked (idempotent), returning existing")
                    return row
            tx = WalletTx(
                guild_id=guild_id, player_id=player_id, kind=kind, amount=amount,
                status="done", counterparty_id=counterparty_id, job_id=job_id,
                source=source, notes=notes,
            )
            session.add(tx)
            await session.flush()
            await session.refresh(tx)
            logger.info(f"credit_done: {player_id} +{amount} ({kind}) job={job_id} -> tx {tx.id}")
            return tx

    try:
        return await with_db_retry(_do)
    except IntegrityError:
        # uq_wallet_tx_job_kind: a concurrent handler booked this job between our check
        # and insert — re-run; _do's own idempotency branch returns the existing row.
        logger.info(f"credit_done: job {job_id} booked concurrently, refetching")
        return await _do()


# ---------------------------------------------------------------------------
# withdraw = reserve (pending debit) -> settle | cancel
# ---------------------------------------------------------------------------
async def reserve_debit(
    guild_id: str,
    player_id: str,
    amount: int,
    *,
    kind: str = "withdraw",
    counterparty_id: str = None,
    source: str = "serve",
    notes: str = None,
) -> WalletTx:
    """Atomically check ``available >= amount`` and write a 'pending' debit (-amount)
    that reserves the tix. Caller then POSTs the MTGO job and calls ``attach_job``.
    Raises ValueError if funds are insufficient."""
    if amount <= 0:
        raise ValueError("Withdraw amount must be positive")

    async def _do():
        async with db_session() as session:
            balance, reserved = await _balances(session, guild_id, player_id)
            available = balance - reserved
            if amount > available:
                raise ValueError(
                    f"Insufficient funds: need {amount}, available {available} "
                    f"(balance {balance}, reserved {reserved})"
                )
            tx = WalletTx(
                guild_id=guild_id, player_id=player_id, kind=kind, amount=-amount,
                status="pending", counterparty_id=counterparty_id, source=source, notes=notes,
            )
            session.add(tx)
            await session.flush()
            await session.refresh(tx)
            logger.info(f"reserve_debit: {player_id} reserved {amount} ({kind}) -> tx {tx.id}")
            return tx

    async with MONEY_LOCK:
        return await with_db_retry(_do)


async def attach_job(tx_id: int, job_id: str) -> WalletTx:
    """Attach the serve job id to a reservation once the POST returns."""
    async def _do():
        async with db_session() as session:
            tx = await session.get(WalletTx, tx_id)
            if tx is None:
                logger.warning(f"attach_job: tx {tx_id} not found")
                return None
            tx.job_id = job_id
            await session.flush()
            await session.refresh(tx)
            return tx

    return await with_db_retry(_do)


async def _resolve_reserve(tx_id: int, new_status: str) -> WalletTx:
    async def _do():
        async with db_session() as session:
            tx = await session.get(WalletTx, tx_id)
            if tx is None:
                logger.warning(f"resolve reserve: tx {tx_id} not found")
                return None
            if tx.status == "pending":
                tx.status = new_status
                await session.flush()
                await session.refresh(tx)
                logger.info(f"reserve tx {tx_id} -> {new_status}")
            else:
                logger.info(f"reserve tx {tx_id} already {tx.status} (idempotent no-op)")
            return tx

    return await with_db_retry(_do)


async def settle_reserve(tx_id: int) -> WalletTx:
    """Withdraw job completed: confirm the reservation (pending -> done). Idempotent."""
    return await _resolve_reserve(tx_id, "done")


async def cancel_reserve(tx_id: int) -> WalletTx:
    """Withdraw job failed/aborted: release the reservation (pending -> cancelled). Idempotent."""
    return await _resolve_reserve(tx_id, "cancelled")


# ---------------------------------------------------------------------------
# internal pay (no MTGO trade) — two 'done' rows, idempotent by source
# ---------------------------------------------------------------------------
async def pay(
    guild_id: str,
    from_player: str,
    to_player: str,
    amount: int,
    *,
    source: str = None,
    notes: str = None,
) -> tuple[WalletTx, WalletTx]:
    """Move ``amount`` tix from one wallet to another with no trade. Checks the payer's
    available funds, writes a -amount 'pay' row and a +amount 'receive' row in one
    transaction. Idempotent by ``source`` (pass a debt/settlement id to make a
    debt-settlement pay replay-safe; a uuid is generated otherwise)."""
    if amount <= 0:
        raise ValueError("Payment amount must be positive")
    if from_player == to_player:
        raise ValueError("Cannot pay yourself")
    if source is None:
        source = str(uuid.uuid4())

    async def _do():
        async with db_session() as session:
            # idempotency: both legs already written for this source?
            existing = await session.execute(
                select(WalletTx).where(WalletTx.source == source, WalletTx.kind.in_(["pay", "receive"]))
                .order_by(WalletTx.id)
            )
            rows = existing.scalars().all()
            if len(rows) >= 2:
                logger.info(f"pay: source {source} already settled (idempotent), returning existing")
                return rows[0], rows[1]

            balance, reserved = await _balances(session, guild_id, from_player)
            available = balance - reserved
            if amount > available:
                raise ValueError(
                    f"Insufficient funds: {from_player} needs {amount}, available {available}"
                )

            debit = WalletTx(
                guild_id=guild_id, player_id=from_player, kind="pay", amount=-amount,
                status="done", counterparty_id=to_player, source=source, notes=notes,
            )
            credit = WalletTx(
                guild_id=guild_id, player_id=to_player, kind="receive", amount=amount,
                status="done", counterparty_id=from_player, source=source, notes=notes,
            )
            session.add(debit)
            session.add(credit)
            await session.flush()
            await session.refresh(debit)
            await session.refresh(credit)
            logger.info(f"pay: {from_player} -> {to_player} {amount} tix (source {source})")
            return debit, credit

    try:
        async with MONEY_LOCK:
            return await with_db_retry(_do)
    except IntegrityError:
        # uq_wallet_tx_transfer_legs: this source was settled concurrently — re-run;
        # _do's own idempotency branch returns the existing legs.
        logger.info(f"pay: source {source} settled concurrently, refetching")
        return await _do()


async def adjust(guild_id: str, player_id: str, amount: int, notes: str, created_by: str) -> WalletTx:
    """Admin credit/debit (audit correction). ``amount`` signed. Bypasses the funds
    check (an admin may drive a balance negative to record a real-world discrepancy)."""
    if amount == 0:
        raise ValueError("Adjustment cannot be zero")

    async def _do():
        async with db_session() as session:
            tx = WalletTx(
                guild_id=guild_id, player_id=player_id, kind="adjust", amount=amount,
                status="done", source="admin", notes=f"{notes} (by {created_by})",
            )
            session.add(tx)
            await session.flush()
            await session.refresh(tx)
            logger.info(f"adjust: {player_id} {amount:+d} by {created_by}")
            return tx

    return await with_db_retry(_do)


# ---------------------------------------------------------------------------
# reconciliation audit: physical vault tix == SUM(settled wallets)
# ---------------------------------------------------------------------------
async def reconcile(bot_tix: int) -> dict:
    """Compare the physical vault tix (from the serve ``/vault``) to the claim total.

    Both sides are global (one custodian, one claim ledger). Returns
    {ok, wallet_total, bot_tix, diff}; ok when they match exactly."""
    wallet_total = await total_wallets()
    diff = bot_tix - wallet_total
    ok = diff == 0
    if not ok:
        logger.warning(f"RECONCILE MISMATCH: bot_tix={bot_tix} wallets={wallet_total} diff={diff:+d}")
    return {"ok": ok, "wallet_total": wallet_total, "bot_tix": bot_tix, "diff": diff}
