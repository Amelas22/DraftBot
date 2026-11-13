"""Escrow -- tix a player locks for a named counterparty until a moderator decides.

This is player-to-player escrow decided by a moderator; the escrow behind tournament
entry fees lives in services/tournament_escrow_service.py.

The tix sit with the synthetic holder ``escrow:hold:<id>`` (see
wallet_service.escrow_holder_id), so they are ordinary wallet rows: counted by the
reconciliation audit, and the system total never moves. An ``escrow_holds`` row records
who, why and where it stands.

  * open   -- transfer the funder's tix into the holder and insert the record, in one
              transaction, so a record never exists without its tix or the reverse.
  * decide -- a moderator releases the tix to the counterparty or returns them to the
              funder: the holder's whole balance moves and the record is closed, in one
              transaction. Only the first decision on a hold lands.

Every ledger read and write goes through wallet_service. MONEY_LOCK is taken here, at the
entry point, and released before anyone is notified: wallet_service's lock is not
reentrant, and settling the recipient's debts (on_inflow) takes it again.
"""
import asyncio
from collections.abc import Awaitable
from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy import or_, select

from database.db_session import db_session
from database.retry import with_db_retry
from models.escrow_hold import OPEN, RELEASED, RETURNED, EscrowHold
from services import wallet_service
from services.mtgo_resolution_service import on_inflow
from services.wallet_service import InsufficientFunds


class EscrowNotFound(LookupError):
    """No such escrow in this server."""


class EscrowAlreadyDecided(ValueError):
    """The escrow was released or returned already. Carries the row as it now stands so
    the refusal can say what happened and who decided."""

    def __init__(self, escrow: EscrowHold):
        super().__init__(f"Escrow #{escrow.id} is already {escrow.status}")
        self.escrow = escrow


def recipient_of(escrow: EscrowHold, outcome: str) -> str:
    """Who the tix go to for this outcome."""
    return escrow.counterparty_id if outcome == RELEASED else escrow.funder_id


# The size of EscrowHold.note's column. SQLite would not enforce it, so open_escrow does.
# It also keeps the ledger's copy ("escrow #<id>: <note>") inside WalletTx.notes' 256.
NOTE_MAX = 200


async def _tell(escrow_id: int, notice: Awaitable[Any]) -> None:
    """Await a notification or inflow settlement. The money has already moved, so a
    failure here is logged and never allowed to look like a failed escrow."""
    try:
        await notice
    except Exception as e:  # noqa: BLE001
        logger.warning(f"escrow #{escrow_id}: follow-up failed: {e}")


async def open_escrow(guild_id: str, funder_id: str, counterparty_id: str,
                      amount: int, note: str) -> EscrowHold:
    """Lock ``amount`` tix from the funder's wallet for the counterparty."""
    note = note.strip()
    if amount < 1:
        raise ValueError("An escrow must hold at least 1 tix")
    if funder_id == counterparty_id:
        raise ValueError("An escrow needs two different players")
    if not note:
        raise ValueError("An escrow needs a note saying what it is for")
    if len(note) > NOTE_MAX:
        raise ValueError(f"An escrow note is at most {NOTE_MAX} characters")

    async def _do() -> EscrowHold:
        async with db_session() as session:
            balance = await wallet_service.balance_in(session, guild_id, funder_id)
            if amount > balance:
                raise InsufficientFunds(funder_id, amount, balance)
            hold = EscrowHold(guild_id=guild_id, funder_id=funder_id,
                              counterparty_id=counterparty_id, amount=amount,
                              note=note, status=OPEN)
            session.add(hold)
            await session.flush()  # the id names the holder
            await wallet_service.transfer_in(
                session, guild_id, funder_id, wallet_service.escrow_holder_id(hold.id),
                amount, f"escrow:{hold.id}:fund", f"escrow #{hold.id}: {note}")
            return hold

    async with wallet_service.MONEY_LOCK:
        hold = await with_db_retry(_do)

    # Lazy, like every money service: notification_service pulls in discord, which the
    # tests and alembic's env importing this module must not need.
    from notification_service import notify_escrow_opened, notify_wallet
    logger.info(f"escrow #{hold.id} opened: {funder_id} -> {counterparty_id} {amount} tix")
    await asyncio.gather(*(
        _tell(hold.id, notify_wallet(
            notify_escrow_opened, guild_id, party, escrow_id=hold.id, funder_id=funder_id,
            counterparty_id=counterparty_id, amount=amount, note=note))
        for party in (funder_id, counterparty_id)))
    return hold


async def decide(guild_id: str, escrow_id: int, outcome: str, decided_by: str) -> EscrowHold:
    """Release the escrow to the counterparty or return it to the funder.

    Moderators may decide an escrow they are a party to; that is the caller's policy
    to restrict, not this ledger's."""
    if outcome not in (RELEASED, RETURNED):
        raise ValueError(f"Unknown escrow outcome: {outcome!r}")

    async def _do() -> tuple[EscrowHold, bool]:
        # (row, decided_now). An already-decided hold is reported by return rather than
        # by raising inside the session: a rollback would expire the row, and the
        # caller reads it after the session has closed.
        async with db_session() as session:
            hold = await session.get(EscrowHold, escrow_id)
            if hold is None or hold.guild_id != guild_id:
                raise EscrowNotFound(f"No escrow #{escrow_id} in this server")
            if hold.status != OPEN:
                return hold, False
            holder = wallet_service.escrow_holder_id(hold.id)
            held = await wallet_service.balance_in(session, guild_id, holder)
            await wallet_service.transfer_in(
                session, guild_id, holder, recipient_of(hold, outcome), held,
                f"escrow:{hold.id}:decide", f"escrow #{hold.id} {outcome}")
            hold.status = outcome
            hold.decided_by = decided_by
            hold.decided_at = datetime.now()
            return hold, True

    async with wallet_service.MONEY_LOCK:
        hold, decided_now = await with_db_retry(_do)

    if not decided_now:
        raise EscrowAlreadyDecided(hold)

    from notification_service import notify_escrow_decided, notify_wallet
    logger.info(f"escrow #{hold.id} {outcome} by {decided_by}")
    recipient = recipient_of(hold, outcome)
    other = hold.funder_id if recipient == hold.counterparty_id else hold.counterparty_id
    details = dict(escrow_id=hold.id, funder_id=hold.funder_id,
                   counterparty_id=hold.counterparty_id, amount=hold.amount,
                   outcome=outcome, decided_by=decided_by, note=hold.note)
    # The moderator's panel is waiting on this: the two parties are told side by side.
    await asyncio.gather(
        _tell(hold.id, on_inflow(guild_id, recipient, notify_escrow_decided, **details)),
        _tell(hold.id, notify_wallet(notify_escrow_decided, guild_id, other, **details)))
    return hold


async def get_escrow(guild_id: str, escrow_id: int) -> EscrowHold | None:
    """The escrow with this id if it exists in this server, whatever its status."""
    async with db_session() as session:
        hold = await session.get(EscrowHold, escrow_id)
        return hold if hold is not None and hold.guild_id == guild_id else None


async def open_escrows(guild_id: str) -> list[EscrowHold]:
    """Every escrow awaiting a decision in this server, oldest first."""
    async with db_session() as session:
        rows = await session.execute(
            select(EscrowHold)
            .where(EscrowHold.guild_id == guild_id, EscrowHold.status == OPEN)
            .order_by(EscrowHold.created_at, EscrowHold.id))
        return list(rows.scalars().all())


async def escrows_for(guild_id: str, player_id: str, *,
                      decided_limit: int = 5) -> list[EscrowHold]:
    """A player's escrows in either role: all the open ones (oldest first), then their
    ``decided_limit`` most recently decided."""
    mine = or_(EscrowHold.funder_id == player_id, EscrowHold.counterparty_id == player_id)
    async with db_session() as session:
        open_rows = await session.execute(
            select(EscrowHold)
            .where(EscrowHold.guild_id == guild_id, mine, EscrowHold.status == OPEN)
            .order_by(EscrowHold.created_at, EscrowHold.id))
        decided_rows = await session.execute(
            select(EscrowHold)
            .where(EscrowHold.guild_id == guild_id, mine, EscrowHold.status != OPEN)
            .order_by(EscrowHold.decided_at.desc(), EscrowHold.id.desc())
            .limit(decided_limit))
        return list(open_rows.scalars().all()) + list(decided_rows.scalars().all())
