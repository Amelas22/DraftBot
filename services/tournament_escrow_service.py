"""
Tournament escrow — holds a team's entry fee in the captain's wallet until the tournament
starts, bridging tournament_service (the participant row) and wallet_service (the reserve).

The entry fee is held as a **pending wallet reserve** on the captain (kind='escrow'), tagged
with a per-participant ``source`` so it's idempotent and refundable:
  * secure  — reserve the fee (wallet-first; the caller deposits any shortfall first) and
              flip the participant to 'paid'. Reusing an existing reserve makes it safe to
              retry after a crash between reserving and marking paid.
  * refund  — cancel the reserve (drop before start); the tix become spendable again.
  * on start — reallocate: each held reserve moves into the tournament's PRIZE WALLET
              (settle the captain's reserve to 'done' AND credit the prize wallet the same
              amount, in one transaction). That's an internal transfer, so vault ==
              SUM(wallets) is preserved; the prize wallet now holds the pot, and a future
              payout phase pays it out to the winners.

The prize wallet is an ordinary wallet holder with a synthetic id (``prize:tourney:<id>``), so
it's counted by the reconciliation audit and can later ``pay`` out to winners like any wallet.
"""
from datetime import datetime

from loguru import logger
from sqlalchemy import func, select

from database.db_session import db_session
from models.tournament import Tournament, TournamentParticipant
from models.wallet_tx import WalletTx
from services import wallet_service


def escrow_source(tournament_id, participant_id) -> str:
    """Stable per-participant tag on the reserve (idempotency + refund handle + pot sum)."""
    return f"tourney:{tournament_id}:{participant_id}"


def prize_wallet_id(tournament_id) -> str:
    """Synthetic wallet holder for a tournament's prize pool (an ordinary WalletTx player_id)."""
    return f"prize:tourney:{tournament_id}"


# Prize-pool split presets (top-heavy, per MTG convention). Ratios need not sum to 100 —
# compute_allocations normalizes by the sum of the places actually paid.
PAYOUT_STRUCTURES = {
    "winner_take_all": [100],
    "top2": [65, 35],
    "top3": [50, 30, 20],
    "top4": [40, 30, 20, 10],
}


def describe_structure(name: str) -> str:
    ratios = PAYOUT_STRUCTURES.get(name)
    if not ratios:
        return name
    if name == "winner_take_all":
        return "winner-take-all"
    return f"top {len(ratios)} ({'/'.join(str(r) for r in ratios)})"


def compute_allocations(pool: int, structure: str, ranked: list) -> list:
    """Split ``pool`` tix over the ranked teams by the named structure.

    ``ranked`` is [(captain_id, team_name)] in finish order (1st first). Returns
    [(place, captain_id, team_name, amount)] for amounts > 0. Integer tix; the whole pool is
    always distributed — normalized to the places actually paid (so fewer teams than the
    structure names just concentrates the split), with the rounding remainder going to 1st."""
    ratios = PAYOUT_STRUCTURES.get(structure) or PAYOUT_STRUCTURES["winner_take_all"]
    n = min(len(ratios), len(ranked))
    if n == 0 or pool <= 0:
        return []
    ratios = ratios[:n]
    total_ratio = sum(ratios)
    amounts = [pool * r // total_ratio for r in ratios]  # floor
    amounts[0] += pool - sum(amounts)  # remainder (and any truncated tail) -> 1st
    out = []
    for i in range(n):
        if amounts[i] > 0:
            captain_id, team_name = ranked[i]
            out.append((i + 1, captain_id, team_name, amounts[i]))
    return out


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


async def drop_with_refund(tournament_id: int, team_name: str) -> dict:
    """Remove a team AND release its escrow hold, atomically in one transaction (so the
    participant delete and the reserve cancel can never half-apply — a team can't end up
    seeded-but-unpaid, nor deleted-but-still-holding tix). Registration phase only.

    Returns {team_name, refunded}. Raises ValueError like tournament_service.remove_team."""
    async with db_session() as session:
        tournament = await session.get(Tournament, tournament_id)
        if tournament is None:
            raise ValueError("Tournament not found.")
        if tournament.status != "registration":
            raise ValueError(f"Teams cannot be removed once '{tournament.name}' has started.")
        stmt = select(TournamentParticipant).where(
            TournamentParticipant.tournament_id == tournament_id,
            func.lower(TournamentParticipant.team_name) == team_name.strip().lower(),
        )
        p = (await session.execute(stmt)).scalars().first()
        if p is None:
            raise ValueError(f"'{team_name}' is not registered for this tournament.")
        refunded = 0
        if p.escrow_tx_id:
            tx = await session.get(WalletTx, p.escrow_tx_id)
            if tx is not None and tx.status == "pending":
                tx.status = "cancelled"
                refunded = -tx.amount
        name = p.team_name
        await session.delete(p)
        await session.flush()
        logger.info(f"escrow: dropped '{name}' from tournament {tournament_id} (refunded {refunded})")
        return {"team_name": name, "refunded": refunded}


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


async def reallocate_to_prize(session, guild_id: str, tournament_id: int) -> dict:
    """Move every held escrow reserve into the tournament's prize wallet — run INSIDE the
    caller's session so it commits atomically with ``start_tournament`` (seed + reallocate
    together, or neither).

    For each paid participant still holding a pending 'escrow' reserve: settle that reserve to
    'done' (the captain's tix leave their wallet) AND credit the prize wallet the same amount
    ('done'). Because it's an internal transfer, SUM(done) — and thus the vault==wallets audit —
    is unchanged. Idempotent: a reserve that's already settled/cancelled is skipped, so a
    re-run moves nothing. Returns {moved, count, prize_id}."""
    prize_id = prize_wallet_id(tournament_id)
    parts = (await session.execute(
        select(TournamentParticipant).where(
            TournamentParticipant.tournament_id == tournament_id,
            TournamentParticipant.status == "paid",
            TournamentParticipant.escrow_tx_id.isnot(None),
        ))).scalars().all()

    moved = 0
    count = 0
    for p in parts:
        tx = await session.get(WalletTx, p.escrow_tx_id)
        if tx is None or tx.status != "pending":
            continue  # comped (no reserve), already reallocated, or refunded
        fee = -tx.amount  # the reserve is a negative debit
        tx.status = "done"  # settle the captain's debit
        session.add(WalletTx(
            guild_id=guild_id, player_id=prize_id, kind="receive", amount=fee, status="done",
            counterparty_id=p.captain_user_id, source=f"prize:{tournament_id}:{p.id}",
            notes=f"entry fee to prize pool: {p.team_name}"))
        moved += fee
        count += 1
    await session.flush()
    logger.info(f"prize reallocation: tournament {tournament_id} moved {moved} tix "
                f"from {count} team(s) into {prize_id}")
    return {"moved": moved, "count": count, "prize_id": prize_id}


async def prize_pool(guild_id: str, tournament_id: int) -> int:
    """The tournament's prize wallet balance (settled tix available to pay out)."""
    async with db_session() as session:
        result = await session.execute(
            select(func.coalesce(func.sum(WalletTx.amount), 0)).where(
                WalletTx.guild_id == guild_id,
                WalletTx.player_id == prize_wallet_id(tournament_id),
                WalletTx.status == "done",
            ))
        return int(result.scalar() or 0)


def _payout_like(tournament_id) -> str:
    return f"payout:{tournament_id}:%"


async def is_paid_out(tournament_id: int) -> bool:
    """True once any payout has been booked for this tournament (idempotency guard)."""
    async with db_session() as session:
        r = await session.execute(
            select(WalletTx.id).where(WalletTx.source.like(_payout_like(tournament_id))).limit(1))
        return r.scalar() is not None


async def execute_payout(guild_id: str, tournament_id: int, allocations: list) -> dict:
    """Disburse the prize pool to the winners' captains, atomically in one transaction.

    ``allocations`` is [(place, captain_id, team_name, amount)]. For each, debits the prize
    wallet and credits the captain (an internal transfer, so vault==SUM(wallets) holds).
    Idempotent by source ``payout:<tid>:<place>`` — a re-run books nothing. Refuses to pay
    more than the pool holds. Returns {ok, already_paid?, paid, total, pool} or {ok:False,error}."""
    prize_id = prize_wallet_id(tournament_id)
    async with db_session() as session:
        already = await session.execute(
            select(WalletTx.id).where(WalletTx.source.like(_payout_like(tournament_id))).limit(1))
        if already.scalar():
            return {"ok": True, "already_paid": True}

        pool = int((await session.execute(
            select(func.coalesce(func.sum(WalletTx.amount), 0)).where(
                WalletTx.guild_id == guild_id, WalletTx.player_id == prize_id,
                WalletTx.status == "done"))).scalar() or 0)
        total = sum(amount for _, _, _, amount in allocations)
        if total > pool:
            return {"ok": False, "error": f"allocations ({total}) exceed the prize pool ({pool})"}

        for place, captain_id, team_name, amount in allocations:
            if amount <= 0:
                continue
            source = f"payout:{tournament_id}:{place}"
            note = f"tournament prize (place {place}): {team_name}"
            session.add(WalletTx(
                guild_id=guild_id, player_id=prize_id, kind="pay", amount=-amount, status="done",
                counterparty_id=captain_id, source=source, notes=note))
            session.add(WalletTx(
                guild_id=guild_id, player_id=captain_id, kind="receive", amount=amount, status="done",
                counterparty_id=prize_id, source=source, notes=note))
        await session.flush()
        logger.info(f"payout: tournament {tournament_id} distributed {total} tix to "
                    f"{len(allocations)} team(s)")
        return {"ok": True, "paid": allocations, "total": total, "pool": pool}
