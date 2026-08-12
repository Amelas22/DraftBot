"""
Tix wallet service — the obligation/claim ledger over ``WalletTx``.

Strict double-entry over an append-only log. A wallet is a *view* over its rows:

    balance = SUM(amount)

That's the whole rule. There is no stored balance, no row status, and nothing is ever
mutated after it's written — every correction is a new compensating row, so the balance
at any past moment is reconstructible from the log alone.

Two kinds of movement:

  * **Transfers** (2 rows, net 0) move a claim between holders: a player paying another,
    an entry fee going into a tournament's prize wallet, a refund coming back out.
    SUM over all wallets is unchanged, so the reconciliation invariant holds by
    construction. Idempotent by ``source`` — the ``uq_wallet_tx_transfer_legs`` index
    makes a double-booking impossible, not merely unlikely.

  * **Boundary crossings** (1 row) are the only entries that change the system total,
    and they exist precisely because value entered or left the physical vault: a
    completed deposit credits (+n), a completed withdraw debits (-n). Idempotent by
    ``job_id`` — booked ONLY once the MTGO job reports 'done', never on enqueue.

An in-flight withdraw is not a status; it's a transfer into the ``system:in-flight``
holder (see SYSTEM_IN_FLIGHT). While the trade is open those tix belong to that holder,
so they're unspendable by the player without any special-casing in the balance query.
Success books the boundary debit against in-flight; failure transfers them back.

Reconciliation: ``reconcile()`` asserts vault tix == SUM(all wallet rows).
"""
import asyncio
import uuid
from dataclasses import dataclass
from loguru import logger
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError

from database.db_session import db_session
from database.retry import with_db_retry
from models.wallet_tx import WalletTx

VALID_KINDS = {"deposit", "withdraw", "pay", "receive", "adjust"}

# Synthetic holders. Not people: they own claims the same way a player does, which is
# what keeps every movement a plain transfer. is_system_account() exists so anything
# iterating "players with wallets" can exclude them in one place.
SYSTEM_IN_FLIGHT = "system:in-flight"   # tix committed to an open MTGO withdraw trade
SYSTEM_PREFIXES = ("system:", "prize:")


def is_system_account(player_id: str) -> bool:
    """True for synthetic wallet holders (in-flight, tournament prize pools)."""
    return bool(player_id) and player_id.startswith(SYSTEM_PREFIXES)


# Serializes every DEBIT (check-balance-then-spend) across the process. The bot is the
# database's only writer, so this lock is sufficient to prevent two concurrent debits
# from both reading the same balance and both spending it. Held only around short
# transactions; boundary credits don't need it (they can't overdraw, and double-booking
# is blocked by uq_wallet_tx_job_kind). NOT reentrant — acquire only at service entry
# points, never from code already running under it.
MONEY_LOCK = asyncio.Lock()


@dataclass
class Wallet:
    guild_id: str
    player_id: str
    balance: int


async def _sum_amount(session, *conditions) -> int:
    q = select(func.coalesce(func.sum(WalletTx.amount), 0)).where(*conditions)
    result = await session.execute(q)
    return int(result.scalar() or 0)


async def balance_in(session, guild_id: str, player_id: str) -> int:
    """A holder's balance inside an existing session/transaction — the single definition,
    reused by every caller that needs to check funds within its own transaction."""
    return await _sum_amount(
        session, WalletTx.guild_id == guild_id, WalletTx.player_id == player_id)


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------
async def get_balance(guild_id: str, player_id: str) -> int:
    async with db_session() as session:
        return await balance_in(session, guild_id, player_id)


async def get_wallet(guild_id: str, player_id: str) -> Wallet:
    async with db_session() as session:
        return Wallet(guild_id, player_id, await balance_in(session, guild_id, player_id))


async def total_wallets() -> int:
    """SUM of every row — the claim side of the reconciliation invariant. Global on
    purpose: the physical vault (one MTGO custodian) is shared across guilds."""
    async with db_session() as session:
        return await _sum_amount(session)


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
# boundary crossings — the only entries that change the system total
# ---------------------------------------------------------------------------
async def credit_done(
    guild_id: str,
    player_id: str,
    amount: int,
    kind: str = "deposit",
    *,
    job_id: str = None,
    counterparty_id: str = None,
    source: str = None,
    notes: str = None,
) -> WalletTx:
    """Book a completed deposit (+amount): value entered the vault. Idempotent by
    ``job_id`` when provided — a replayed job poll returns the existing row."""
    if amount <= 0:
        raise ValueError("Credit amount must be positive")
    if kind not in VALID_KINDS:
        raise ValueError(f"Invalid kind: {kind}")

    async def _do():
        async with db_session() as session:
            if job_id:
                row = (await session.execute(
                    select(WalletTx).where(WalletTx.job_id == job_id, WalletTx.kind == kind)
                )).scalars().first()
                if row:
                    logger.info(f"credit_done: job {job_id} already booked (idempotent)")
                    return row
            tx = WalletTx(
                guild_id=guild_id, player_id=player_id, kind=kind, amount=amount,
                counterparty_id=counterparty_id, job_id=job_id, source=source, notes=notes,
            )
            session.add(tx)
            await session.flush()
            await session.refresh(tx)
            logger.info(f"credit_done: {player_id} +{amount} ({kind}) job={job_id} -> tx {tx.id}")
            return tx

    try:
        return await with_db_retry(_do)
    except IntegrityError:
        logger.info(f"credit_done: job {job_id} booked concurrently, refetching")
        return await _do()


async def debit_done(
    guild_id: str,
    player_id: str,
    amount: int,
    *,
    job_id: str = None,
    counterparty_id: str = None,
    source: str = None,
    notes: str = None,
) -> WalletTx:
    """Book a completed withdraw (-amount): value left the vault. The holder is normally
    SYSTEM_IN_FLIGHT (the tix were transferred there when the trade opened), so this
    consumes that committed claim rather than a player's balance. Idempotent by ``job_id``."""
    if amount <= 0:
        raise ValueError("Debit amount must be positive")

    async def _do():
        async with db_session() as session:
            if job_id:
                row = (await session.execute(
                    select(WalletTx).where(
                        WalletTx.job_id == job_id, WalletTx.kind == "withdraw")
                )).scalars().first()
                if row:
                    logger.info(f"debit_done: job {job_id} already booked (idempotent)")
                    return row
            tx = WalletTx(
                guild_id=guild_id, player_id=player_id, kind="withdraw", amount=-amount,
                counterparty_id=counterparty_id, job_id=job_id, source=source, notes=notes,
            )
            session.add(tx)
            await session.flush()
            await session.refresh(tx)
            logger.info(f"debit_done: {player_id} -{amount} (withdraw) job={job_id} -> tx {tx.id}")
            return tx

    try:
        return await with_db_retry(_do)
    except IntegrityError:
        logger.info(f"debit_done: job {job_id} booked concurrently, refetching")
        return await _do()


# ---------------------------------------------------------------------------
# transfers — 2 rows, net 0, idempotent by source
# ---------------------------------------------------------------------------
def transfer_rows(guild_id: str, from_player: str, to_player: str, amount: int,
                  source: str, notes: str = None) -> tuple[WalletTx, WalletTx]:
    """The (debit, credit) pair for one transfer — unsaved, so a caller can add them to
    its own transaction (escrow, payout, debt settlement all compose this way)."""
    return (
        WalletTx(guild_id=guild_id, player_id=from_player, kind="pay", amount=-amount,
                 counterparty_id=to_player, source=source, notes=notes),
        WalletTx(guild_id=guild_id, player_id=to_player, kind="receive", amount=amount,
                 counterparty_id=from_player, source=source, notes=notes),
    )


async def transfer_in(session, guild_id: str, from_player: str, to_player: str,
                      amount: int, source: str, notes: str = None,
                      *, check_funds: bool = True):
    """Write a transfer inside the CALLER's session/transaction. Raises ValueError if the
    payer can't cover it (pass check_funds=False for a refund out of a system holder that
    is being unwound). Caller must hold MONEY_LOCK when check_funds is True."""
    if amount <= 0:
        raise ValueError("Transfer amount must be positive")
    if from_player == to_player:
        raise ValueError("Cannot transfer to the same holder")
    if check_funds:
        available = await balance_in(session, guild_id, from_player)
        if amount > available:
            raise ValueError(f"Insufficient funds: {from_player} needs {amount}, has {available}")
    debit, credit = transfer_rows(guild_id, from_player, to_player, amount, source, notes)
    session.add(debit)
    session.add(credit)
    await session.flush()
    return debit, credit


async def pay(
    guild_id: str,
    from_player: str,
    to_player: str,
    amount: int,
    *,
    source: str = None,
    notes: str = None,
) -> tuple[WalletTx, WalletTx]:
    """Move ``amount`` tix between two holders — no MTGO trade, no change to the system
    total. Idempotent by ``source`` (a uuid is generated when none is given)."""
    if source is None:
        source = str(uuid.uuid4())

    async def _do():
        async with db_session() as session:
            existing = (await session.execute(
                select(WalletTx).where(
                    WalletTx.source == source, WalletTx.kind.in_(["pay", "receive"])
                ).order_by(WalletTx.id))).scalars().all()
            if len(existing) >= 2:
                logger.info(f"pay: source {source} already settled (idempotent)")
                return existing[0], existing[1]
            rows = await transfer_in(session, guild_id, from_player, to_player,
                                     amount, source, notes)
            logger.info(f"pay: {from_player} -> {to_player} {amount} tix (source {source})")
            return rows

    try:
        async with MONEY_LOCK:
            return await with_db_retry(_do)
    except IntegrityError:
        logger.info(f"pay: source {source} settled concurrently, refetching")
        return await _do()


async def adjust(guild_id: str, player_id: str, amount: int, notes: str, created_by: str) -> WalletTx:
    """Admin correction (signed, one-sided — it deliberately changes the system total, so
    it will show up in reconciliation). Bypasses the funds check: an admin may drive a
    balance negative to record a real-world discrepancy."""
    if amount == 0:
        raise ValueError("Adjustment cannot be zero")

    async def _do():
        async with db_session() as session:
            tx = WalletTx(
                guild_id=guild_id, player_id=player_id, kind="adjust", amount=amount,
                source="admin", notes=f"{notes} (by {created_by})",
            )
            session.add(tx)
            await session.flush()
            await session.refresh(tx)
            logger.info(f"adjust: {player_id} {amount:+d} by {created_by}")
            return tx

    return await with_db_retry(_do)


# ---------------------------------------------------------------------------
# reconciliation audit: physical vault tix == SUM(all wallet rows)
# ---------------------------------------------------------------------------
async def reconcile(bot_tix: int) -> dict:
    """Compare the physical vault tix (from the serve ``/vault``) to the claim total.

    Both sides are global (one custodian, one claim ledger). In-flight withdraws are
    included: those tix are still physically in the vault until the trade completes, and
    the in-flight holder still owns the matching claim, so both sides move together.
    Returns {ok, wallet_total, bot_tix, diff}; ok when they match exactly."""
    wallet_total = await total_wallets()
    diff = bot_tix - wallet_total
    ok = diff == 0
    if not ok:
        logger.warning(f"RECONCILE MISMATCH: bot_tix={bot_tix} wallets={wallet_total} diff={diff:+d}")
    return {"ok": ok, "wallet_total": wallet_total, "bot_tix": bot_tix, "diff": diff}
