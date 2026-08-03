"""
Tournament escrow — holds a team's entry fee in the captain's wallet until the tournament
starts, bridging tournament_service (the participant row) and wallet_service (the reserve).

The entry fee is held as a **pending wallet reserve** on the captain (kind='escrow'), tagged
with a per-participant ``source`` so it's idempotent and refundable:
  * secure  — reserve the fee (wallet-first; the caller deposits any shortfall first) and
              flip the participant to 'paid'. Reusing an existing reserve makes it safe to
              retry after a crash between reserving and marking paid.
  * refund  — cancel the reserve (drop before start); the tix become spendable again.
  * on start — nothing moves: the reserve simply stays (now non-refundable). It is never
              *settled*, because settling would remove tix from the captain's wallet with no
              holder to receive them, breaking the vault == SUM(wallets) audit. The held
              reserves ARE the pot; a future payout phase settles + redistributes them.
"""
from datetime import datetime

from loguru import logger
from sqlalchemy import func, select

from database.db_session import db_session
from models.tournament import TournamentParticipant
from models.wallet_tx import WalletTx
from services import wallet_service


def escrow_source(tournament_id, participant_id) -> str:
    """Stable per-participant tag on the reserve (idempotency + refund handle + pot sum)."""
    return f"tourney:{tournament_id}:{participant_id}"


async def mark_paid(participant_id: int, reserve_tx_id: int) -> bool:
    """Flip a participant to 'paid', recording the reserve that backs its escrow."""
    async with db_session() as session:
        p = await session.get(TournamentParticipant, participant_id)
        if p is None:
            logger.warning(f"escrow mark_paid: participant {participant_id} not found")
            return False
        p.status = "paid"
        p.escrow_tx_id = reserve_tx_id
        p.paid_at = datetime.now()
        await session.flush()
        logger.info(f"escrow: participant {participant_id} paid (reserve tx {reserve_tx_id})")
        return True


async def secure_from_wallet(guild_id: str, captain_id: str, participant_id: int,
                             tournament_id: int, fee: int, team_name: str) -> dict:
    """Try to hold ``fee`` tix in the captain's wallet for this participant.

    Returns one of:
      {ok: True, done: True, reserved: n}    — escrow held, participant now paid.
      {ok: True, done: False, deficit: n}    — wallet short by ``deficit``; caller must
                                               deposit that much, then call again.
      {ok: False, error: str}                — the reserve failed (e.g. a race lost the funds).
    Idempotent: an existing reserve for this participant is reused, not stacked."""
    if fee <= 0:
        # Free / nothing to hold — treat as already complete.
        await mark_paid(participant_id, None)
        return {"ok": True, "done": True, "reserved": 0}

    source = escrow_source(tournament_id, participant_id)
    existing = await wallet_service.get_pending_reserve(guild_id, captain_id, source)
    if existing:
        await mark_paid(participant_id, existing.id)
        return {"ok": True, "done": True, "reserved": -existing.amount, "reused": True}

    available = await wallet_service.get_available(guild_id, captain_id)
    if available < fee:
        return {"ok": True, "done": False, "deficit": fee - available, "available": available}

    try:
        reserve = await wallet_service.reserve_debit(
            guild_id, captain_id, fee, kind="escrow", source=source,
            notes=f"tournament entry: {team_name}")
    except ValueError as e:  # lost a race for the funds since the check above
        return {"ok": False, "error": str(e)}

    await mark_paid(participant_id, reserve.id)
    return {"ok": True, "done": True, "reserved": fee}


async def refund_reserve(escrow_tx_id: int) -> bool:
    """Release a held escrow (drop before start). Idempotent — a non-pending reserve is a
    no-op. Returns False when there's nothing to refund."""
    if not escrow_tx_id:
        return False
    await wallet_service.cancel_reserve(escrow_tx_id)
    logger.info(f"escrow: refunded reserve tx {escrow_tx_id}")
    return True


async def total_escrowed(guild_id: str, tournament_id: int) -> int:
    """Total tix currently held across a tournament's participants (the pot so far)."""
    prefix = escrow_source(tournament_id, "")  # 'tourney:<id>:'
    async with db_session() as session:
        result = await session.execute(
            select(func.coalesce(func.sum(WalletTx.amount), 0)).where(
                WalletTx.guild_id == guild_id,
                WalletTx.status == "pending",
                WalletTx.source.like(prefix + "%"),
            )
        )
        return int(-(result.scalar() or 0))  # reserves are negative; report positive held
