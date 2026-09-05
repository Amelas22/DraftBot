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

# Synthetic holders. Not people: they own claims the same way a player does, which is
# what keeps every movement a plain transfer.
# Per-guild in practice: every row carries guild_id and both the commit and its
# resolution take the guild from the same MtgoJob, so a cross-guild withdraw can't arise.
SYSTEM_IN_FLIGHT = "system:in-flight"   # tix committed to an open MTGO withdraw trade


def prize_wallet_id(tournament_id) -> str:
    """The holder that owns a tournament's pot."""
    return f"prize:tourney:{tournament_id}"


def is_system_account(player_id: str) -> bool:
    """True for synthetic holders. Real holders are Discord snowflakes, so anything
    non-numeric is ours (in-flight, prize pools) — used wherever holders are rendered
    or iterated as people."""
    return not (player_id or "").isdigit()


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


# The one failure a caller answers differently: "you have no money" is fixed by
# depositing, "that amount is nonsense" is not. Named once here so the layer that
# tags it and the layer that switches on it cannot drift apart on a typo.
INSUFFICIENT_FUNDS = "insufficient_funds"


class InsufficientFunds(ValueError):
    """Not enough available tix. A ValueError subclass so every existing
    ``except ValueError`` keeps catching it, but a distinct type so a caller can tell
    this apart from the other reasons a transfer is refused.

    Carries ``available`` only — the shortfall is the caller's own amount minus that,
    which every caller already has. The message names the holder for the log; it is
    deliberately not what a player is shown, since it reads back their Discord id.
    """

    def __init__(self, player_id: str, needed: int, available: int):
        super().__init__(
            f"Insufficient funds: {player_id} needs {needed}, has {available}")
        self.available = available


async def balance_in(session, guild_id: str, player_id: str) -> int:
    """A holder's balance inside an existing session/transaction — the single definition,
    reused by every caller that needs to check funds within its own transaction."""
    return await _sum_amount(
        session, WalletTx.guild_id == guild_id, WalletTx.player_id == player_id)


async def balances_for(guild_id: str, player_ids) -> dict[str, int]:
    """Balance per holder in one grouped query — rendering N pending teams costs one
    read, not N. Holders with no rows come back as 0 so callers needn't re-check."""
    ids = [str(p) for p in (player_ids or [])]
    if not ids:
        return {}
    async with db_session() as session:
        rows = (await session.execute(
            select(WalletTx.player_id, func.coalesce(func.sum(WalletTx.amount), 0))
            .where(WalletTx.guild_id == guild_id, WalletTx.player_id.in_(ids))
            .group_by(WalletTx.player_id)
        )).all()
    found = {pid: int(total) for pid, total in rows}
    return {pid: found.get(pid, 0) for pid in ids}


async def movements_in(session, guild_id: str, holder: str, party: str) -> int:
    """How many transfers `holder` has exchanged with `party`, in the caller's
    own session.

    Keys a source string that must differ per movement rather than per state: a
    player who joins, leaves and rejoins arrives back at the same balance, so a
    key built from the balances alone would collide with the original join and
    be swallowed as a retry.
    """
    return int((await session.execute(
        select(func.count()).select_from(WalletTx)
        .where(WalletTx.guild_id == guild_id,
               WalletTx.player_id == holder,
               WalletTx.counterparty_id == party)
    )).scalar() or 0)


async def contributions_to(guild_id: str, holder: str) -> dict[str, int]:
    """Net tix each counterparty currently has sitting in `holder`.

    A grouped SUM, deliberately -- not a page of get_history, which clamps to
    100 rows and would silently under-report a holder that has seen more
    movement than that. Zero-net counterparties are dropped, so a party fully
    refunded out of a pool does not show up as holding nothing.
    """
    async with db_session() as session:
        rows = (await session.execute(
            select(WalletTx.counterparty_id, func.coalesce(func.sum(WalletTx.amount), 0))
            .where(WalletTx.guild_id == guild_id,
                   WalletTx.player_id == holder,
                   WalletTx.counterparty_id.isnot(None))
            .group_by(WalletTx.counterparty_id)
            .having(func.coalesce(func.sum(WalletTx.amount), 0) != 0)
        )).all()
    return {str(party): int(total) for party, total in rows}


async def net_between(session, guild_id: str, holder: str, party: str) -> int:
    """What one counterparty currently has sitting in `holder`, inside an
    existing session/transaction -- contributions_to for a single party, read
    from the snapshot the caller is about to write into."""
    return int((await session.execute(
        select(func.coalesce(func.sum(WalletTx.amount), 0))
        .where(WalletTx.guild_id == guild_id,
               WalletTx.player_id == holder,
               WalletTx.counterparty_id == party)
    )).scalar() or 0)


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
async def _book_boundary(guild_id: str, player_id: str, amount: int, kind: str,
                         *, job_id: str, counterparty_id: str | None = None,
                         source: str | None = None, notes: str | None = None) -> WalletTx:
    """Write the lone row for a completed MTGO trade. Idempotent by ``job_id``: a replayed
    poll returns the existing row, and uq_wallet_tx_job_kind makes a concurrent duplicate
    impossible (these run lock-free, unlike transfers)."""
    async def _do():
        async with db_session() as session:
            row = (await session.execute(
                select(WalletTx).where(WalletTx.job_id == job_id, WalletTx.kind == kind)
            )).scalars().first()
            if row:
                logger.info(f"{kind}: job {job_id} already booked (idempotent)")
                return row
            tx = WalletTx(
                guild_id=guild_id, player_id=player_id, kind=kind, amount=amount,
                counterparty_id=counterparty_id, job_id=job_id, source=source, notes=notes,
            )
            session.add(tx)
            await session.flush()
            await session.refresh(tx)
            logger.info(f"{kind}: {player_id} {amount:+d} job={job_id} -> tx {tx.id}")
            return tx

    try:
        return await with_db_retry(_do)
    except IntegrityError:
        logger.info(f"{kind}: job {job_id} booked concurrently, refetching")
        return await _do()


async def credit_done(guild_id: str, player_id: str, amount: int, *, job_id: str,
                      counterparty_id: str | None = None, source: str | None = None,
                      notes: str | None = None) -> WalletTx:
    """Book a completed deposit (+amount): value entered the vault."""
    if amount <= 0:
        raise ValueError("Credit amount must be positive")
    return await _book_boundary(guild_id, player_id, amount, "deposit", job_id=job_id,
                                counterparty_id=counterparty_id, source=source, notes=notes)


async def debit_done(guild_id: str, player_id: str, amount: int, *, job_id: str,
                     counterparty_id: str | None = None, source: str | None = None,
                     notes: str | None = None) -> WalletTx:
    """Book a completed withdraw (-amount): value left the vault. The holder is normally
    SYSTEM_IN_FLIGHT — the tix were transferred there when the trade opened, so this
    consumes that committed claim rather than a player's balance."""
    if amount <= 0:
        raise ValueError("Debit amount must be positive")
    return await _book_boundary(guild_id, player_id, -amount, "withdraw", job_id=job_id,
                                counterparty_id=counterparty_id, source=source, notes=notes)


# ---------------------------------------------------------------------------
# transfers — 2 rows, net 0, idempotent by source
# ---------------------------------------------------------------------------
async def transfer_legs(session, source: str) -> list[WalletTx]:
    """The rows of the transfer booked under ``source`` (empty if never booked) — the one
    'has this already happened?' probe for transfers. Constrains ``kind`` so the partial
    uq_wallet_tx_transfer_legs index serves it."""
    return list((await session.execute(
        select(WalletTx).where(
            WalletTx.source == source, WalletTx.kind.in_(("pay", "receive"))
        ).order_by(WalletTx.id))).scalars().all())


async def transfer_credit(session, source: str) -> WalletTx | None:
    """The credit ('receive') leg of a booked transfer, or None — what a reversal needs
    to know the amount that moved."""
    return (await session.execute(
        select(WalletTx).where(
            WalletTx.source == source, WalletTx.kind == "receive").limit(1)
    )).scalars().first()


async def transfer_in(session, guild_id: str, from_player: str, to_player: str,
                      amount: int, source: str, notes: str | None = None):
    """Write a transfer inside the CALLER's session/transaction — a pure writer: the
    caller owns the funds check (under MONEY_LOCK) because only it knows whether this
    is a spend or the unwinding of a holder it just funded."""
    if amount <= 0:
        raise ValueError("Transfer amount must be positive")
    if from_player == to_player:
        raise ValueError("Cannot transfer to the same holder")
    debit = WalletTx(guild_id=guild_id, player_id=from_player, kind="pay", amount=-amount,
                     counterparty_id=to_player, source=source, notes=notes)
    credit = WalletTx(guild_id=guild_id, player_id=to_player, kind="receive", amount=amount,
                      counterparty_id=from_player, source=source, notes=notes)
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
    source: str | None = None,
    notes: str | None = None,
) -> tuple[WalletTx, WalletTx]:
    """Move ``amount`` tix between two holders — no MTGO trade, no change to the system
    total. Checks the payer's funds. Idempotent by ``source`` (a uuid is generated when
    none is given)."""
    if source is None:
        source = str(uuid.uuid4())

    async def _do():
        async with db_session() as session:
            existing = await transfer_legs(session, source)
            if existing:
                logger.info(f"pay: source {source} already settled (idempotent)")
                return existing[0], existing[1]
            balance = await balance_in(session, guild_id, from_player)
            if amount > balance:
                raise InsufficientFunds(from_player, amount, balance)
            rows = await transfer_in(session, guild_id, from_player, to_player,
                                     amount, source, notes)
            logger.info(f"pay: {from_player} -> {to_player} {amount} tix (source {source})")
            return rows

    async with MONEY_LOCK:
        return await with_db_retry(_do)


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
