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
from sqlalchemy import select, func
from sqlalchemy.exc import OperationalError

from database.db_session import db_session
from models.wallet_tx import WalletTx

VALID_KINDS = {"deposit", "withdraw", "pay", "receive", "adjust"}


@dataclass
class Wallet:
    guild_id: str
    player_id: str
    balance: int      # settled
    reserved: int     # pending withdraws (>=0)
    available: int    # balance - reserved


# ---------------------------------------------------------------------------
# retry helper (transient SQLite write-lock backoff, matching debt_service)
# ---------------------------------------------------------------------------
async def _with_retry(thunk):
    """Run an async thunk, retrying on transient 'database is locked' with backoff."""
    max_retries = 3
    retry_delay = 1.0
    for attempt in range(max_retries):
        try:
            return await thunk()
        except OperationalError as e:
            if "database is locked" in str(e) and attempt < max_retries - 1:
                logger.warning(
                    f"Wallet DB locked on attempt {attempt + 1}/{max_retries}, "
                    f"retrying in {retry_delay}s..."
                )
                await asyncio.sleep(retry_delay)
                retry_delay *= 2
            else:
                raise


async def _sum_amount(session, *conditions) -> int:
    q = select(func.coalesce(func.sum(WalletTx.amount), 0)).where(*conditions)
    result = await session.execute(q)
    return int(result.scalar() or 0)


async def _balances(session, guild_id: str, player_id: str) -> tuple[int, int]:
    """(balance, reserved) inside an existing session/transaction."""
    balance = await _sum_amount(
        session,
        WalletTx.guild_id == guild_id,
        WalletTx.player_id == player_id,
        WalletTx.status == "done",
    )
    pending = await _sum_amount(
        session,
        WalletTx.guild_id == guild_id,
        WalletTx.player_id == player_id,
        WalletTx.status == "pending",
    )
    return balance, -pending  # pending debits are negative -> reserved is positive


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


async def get_all_balances(guild_id: str) -> dict[str, int]:
    """Non-zero settled balances for every player in the guild (for the audit board)."""
    async with db_session() as session:
        query = (
            select(WalletTx.player_id, func.sum(WalletTx.amount).label("balance"))
            .where(WalletTx.guild_id == guild_id, WalletTx.status == "done")
            .group_by(WalletTx.player_id)
            .having(func.sum(WalletTx.amount) != 0)
        )
        result = await session.execute(query)
        return {row.player_id: int(row.balance) for row in result.all()}


async def total_wallets(guild_id: str = None) -> int:
    """SUM of all settled balances — the claim side of the reconciliation invariant.

    The physical vault (one MTGO custodian) is shared across guilds, so the audit total
    is global by default; pass ``guild_id`` only for a per-guild subtotal.
    """
    async with db_session() as session:
        conditions = [WalletTx.status == "done"]
        if guild_id is not None:
            conditions.append(WalletTx.guild_id == guild_id)
        return await _sum_amount(session, *conditions)


async def get_history(guild_id: str, player_id: str = None, limit: int = 25) -> list[WalletTx]:
    limit = min(limit, 100)
    async with db_session() as session:
        query = select(WalletTx).where(WalletTx.guild_id == guild_id)
        if player_id:
            query = query.where(WalletTx.player_id == player_id)
        query = query.order_by(WalletTx.created_at.desc(), WalletTx.id.desc()).limit(limit)
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

    return await _with_retry(_do)


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

    return await _with_retry(_do)


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

    return await _with_retry(_do)


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

    return await _with_retry(_do)


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

    return await _with_retry(_do)


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

    return await _with_retry(_do)


# ---------------------------------------------------------------------------
# reconciliation audit: physical vault tix == SUM(settled wallets)
# ---------------------------------------------------------------------------
async def reconcile(bot_tix: int, guild_id: str = None) -> dict:
    """Compare the physical vault tix (from the serve ``/vault``) to the claim total.

    ``bot_tix`` is global (one custodian), so by default the claim side is summed across
    all guilds. Returns {ok, wallet_total, bot_tix, diff}; ok when they match exactly."""
    wallet_total = await total_wallets(guild_id)
    diff = bot_tix - wallet_total
    ok = diff == 0
    if not ok:
        logger.warning(f"RECONCILE MISMATCH: bot_tix={bot_tix} wallets={wallet_total} diff={diff:+d}")
    return {"ok": ok, "wallet_total": wallet_total, "bot_tix": bot_tix, "diff": diff}
