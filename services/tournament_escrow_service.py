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
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from database.db_session import db_session
from database.retry import with_db_retry
from models.tournament import Tournament, TournamentParticipant
from models.wallet_tx import WalletTx
from services import wallet_service
from services.tournament_service import get_active_tournament, remove_team, start_tournament


def escrow_source(tournament_id, participant_id) -> str:
    """Stable per-participant tag on the reserve (idempotency + refund handle + pot sum).
    Also used as the MtgoJob deposit ``context`` for entry-fee deposits — this module is
    the single owner of the format (parse with parse_escrow_source)."""
    return f"tourney:{tournament_id}:{participant_id}"


def parse_escrow_source(source: str):
    """Inverse of escrow_source: (tournament_id, participant_id), or None if not ours."""
    if not source or not source.startswith("tourney:"):
        return None
    try:
        _, tid, pid = source.split(":")
        return int(tid), int(pid)
    except ValueError:
        return None


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


def _mark_paid(p: TournamentParticipant, reserve_tx_id: int | None):
    """Stamp a participant paid inside the caller's session."""
    p.status = "paid"
    p.escrow_tx_id = reserve_tx_id
    p.paid_at = datetime.now()


def comp(participant: TournamentParticipant) -> bool:
    """Admin comp: mark a participant paid with NO escrow (seed-eligible, captain unbilled).
    Runs inside the CALLER's session so registration + comp commit atomically. Stamps
    paid_at so a comped row is distinguishable from a pre-escrow legacy one; the NULL
    escrow_tx_id records that no tix back this entry (it dilutes the pot on purpose)."""
    if participant.status == "paid":
        return False
    _mark_paid(participant, None)
    logger.info(f"escrow: participant {participant.id} comped (no escrow)")
    return True


async def secure_from_wallet(guild_id: str, captain_id: str, participant_id: int,
                             tournament_id: int, fee: int, team_name: str) -> dict:
    """Try to hold ``fee`` tix in the captain's wallet for this participant — reserve and
    participant flip commit atomically in ONE transaction (a crash can't leave a paid team
    with no hold, or a hold on a pending team).

    Returns one of:
      {ok: True, done: True, reserved: n}    — escrow held, participant now paid.
      {ok: True, done: False, deficit: n}    — wallet short by ``deficit``; caller must
                                               deposit that much, then call again.
      {ok: False, error: str}                — participant vanished mid-flight.
    Idempotent: an existing reserve for this participant is reused, not stacked."""
    if fee <= 0:
        raise ValueError("secure_from_wallet needs a positive fee (free entries never escrow)")

    source = escrow_source(tournament_id, participant_id)

    async def _do():
        async with db_session() as session:
            p = await session.get(TournamentParticipant, participant_id)
            if p is None:
                return {"ok": False, "error": "team no longer registered"}

            existing = await wallet_service._pending_reserve(session, guild_id, captain_id, source)
            if existing:
                _mark_paid(p, existing.id)
                return {"ok": True, "done": True, "reserved": -existing.amount, "reused": True}

            balance, reserved = await wallet_service._balances(session, guild_id, captain_id)
            available = balance - reserved
            if available < fee:
                return {"ok": True, "done": False, "deficit": fee - available, "available": available}

            reserve = WalletTx(
                guild_id=guild_id, player_id=captain_id, kind="escrow", amount=-fee,
                status="pending", source=source, notes=f"tournament entry: {team_name}")
            session.add(reserve)
            await session.flush()
            _mark_paid(p, reserve.id)
            logger.info(f"escrow: participant {participant_id} paid (reserve tx {reserve.id})")
            return {"ok": True, "done": True, "reserved": fee}

    try:
        async with wallet_service.MONEY_LOCK:
            return await with_db_retry(_do)
    except IntegrityError:
        # uq_wallet_tx_live_escrow: a concurrent register reserved this entry first —
        # rerun (unlocked read path finds and reuses the existing hold).
        logger.info(f"secure_from_wallet: concurrent reserve for {source}, reusing")
        return await _do()


async def complete_entry_after_deposit(guild_id: str, captain_id: str,
                                       tournament_id: int, participant_id: int) -> dict:
    """The 'entry-fee deposit landed → secure the escrow' continuation, shared by the
    register command's background follow-up and the pending-jobs resumer (one copy of
    the guards, one behavior). Returns secure_from_wallet's dict plus team/tournament
    names for messaging, or {ok: False, skipped: True} when there's nothing to do
    (team gone, already paid, or fee dropped to zero)."""
    async with db_session() as session:
        t = await session.get(Tournament, tournament_id)
        p = await session.get(TournamentParticipant, participant_id)
    if not t or not p or p.status == "paid" or (t.entry_fee or 0) <= 0:
        return {"ok": False, "skipped": True}
    res = await secure_from_wallet(guild_id, captain_id, p.id, t.id, t.entry_fee, p.team_name)
    res.update({"team_name": p.team_name, "tournament_name": t.name, "fee": t.entry_fee})
    return res


async def sweep_pending_entries() -> int:
    """Complete every pending entry whose captain's wallet can now cover the fee.

    A registration is only complete once the tix are actually in the vault — but it must
    not die just because one trade window closed while the custodian was busy with someone
    else's job. So the pending team is durable: it stays registrable until the tournament
    starts, and this sweep (run on the watchdog tick) finishes it as soon as the funds are
    there, no matter how they arrived — the retried deposit, a plain /wallet deposit, or a
    teammate's /wallet pay. secure_from_wallet is a no-op when the wallet is still short,
    so a sweep over an underfunded entry costs one balance read and changes nothing.

    Returns the number of entries completed."""
    async with db_session() as session:
        rows = (await session.execute(
            select(TournamentParticipant, Tournament)
            .join(Tournament, TournamentParticipant.tournament_id == Tournament.id)
            .where(
                Tournament.status == "registration",
                Tournament.entry_fee > 0,
                TournamentParticipant.status != "paid",
            ))).all()
    completed = 0
    for participant, tournament in rows:
        try:
            res = await secure_from_wallet(
                str(tournament.guild_id), participant.captain_user_id, participant.id,
                tournament.id, tournament.entry_fee, participant.team_name)
        except Exception as e:
            logger.warning(f"sweep_pending_entries: {participant.team_name} failed: {e}")
            continue
        if res.get("done"):
            completed += 1
            logger.info(f"sweep: '{participant.team_name}' completed registration for "
                        f"'{tournament.name}' (funds arrived)")
    return completed


async def resume_entry_from_context(guild_id: str, captain_id: str, context: str):
    """Resumer hook: if ``context`` is one of ours (escrow_source format), finish the
    entry. Unknown contexts are ignored — this module owns the format."""
    parsed = parse_escrow_source(context)
    if parsed is None:
        return
    tournament_id, participant_id = parsed
    res = await complete_entry_after_deposit(guild_id, captain_id, tournament_id, participant_id)
    if res.get("done"):
        logger.info(f"resumed escrow: participant {participant_id} paid after recovered deposit")


async def start_and_fund(guild_id, rng) -> dict:
    """Close registration, seed the schedule, and move held escrow into the prize wallet —
    one transaction under MONEY_LOCK, so concurrent /tournament start calls serialize and
    seeding + the pot move commit together (or neither does). Owns the lock so no cog
    touches MONEY_LOCK directly. Returns {tournament_id, name, fee, pot}; raises
    ValueError with a user-facing message when there's nothing to start."""
    async with wallet_service.MONEY_LOCK:
        async with db_session() as session:
            tournament = await get_active_tournament(session, guild_id)
            if tournament is None:
                raise ValueError("There is no tournament to start.")
            if tournament.status != "registration":
                raise ValueError(f"**{tournament.name}** has already started.")
            fee = tournament.entry_fee or 0
            await start_tournament(session, tournament.id, rng)
            pot = 0
            if fee > 0:
                pot = (await reallocate_to_prize(session, str(guild_id), tournament.id))["moved"]
            return {"tournament_id": tournament.id, "name": tournament.name,
                    "fee": fee, "pot": pot}


async def drop_with_refund(tournament_id: int, team_name: str) -> dict:
    """Remove a team AND release its escrow hold, atomically in one transaction (so the
    participant delete and the reserve cancel can never half-apply — a team can't end up
    seeded-but-unpaid, nor deleted-but-still-holding tix). Registration phase only.

    Returns {team_name, refunded}. Raises ValueError like tournament_service.remove_team
    (which owns the removal rules; this only adds the refund)."""
    async with db_session() as session:
        p = await remove_team(session, tournament_id, team_name)
        refunded = 0
        if p.escrow_tx_id:
            tx = await session.get(WalletTx, p.escrow_tx_id)
            if tx is not None and tx.status == "pending":
                tx.status = "cancelled"
                refunded = -tx.amount
        await session.flush()
        logger.info(f"escrow: dropped '{p.team_name}' from tournament {tournament_id} "
                    f"(refunded {refunded})")
        return {"team_name": p.team_name, "refunded": refunded}


async def total_escrowed(guild_id: str, tournament_id: int) -> int:
    """Total tix currently held across a tournament's participants (the pot so far)."""
    prefix = escrow_source(tournament_id, "")  # 'tourney:<id>:'
    async with db_session() as session:
        held = await wallet_service._sum_amount(
            session,
            WalletTx.guild_id == guild_id,
            WalletTx.status == "pending",
            WalletTx.source.like(prefix + "%"),
        )
        return -held  # reserves are negative; report positive held


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


async def _pool(session, guild_id: str, tournament_id: int) -> int:
    """Prize wallet balance inside an existing session (the one 'what counts as the pool'
    predicate — the preview and the payout cap must agree)."""
    return await wallet_service._sum_amount(
        session,
        WalletTx.guild_id == guild_id,
        WalletTx.player_id == prize_wallet_id(tournament_id),
        WalletTx.status == "done",
    )


async def _already_paid(session, tournament_id: int) -> bool:
    r = await session.execute(
        select(WalletTx.id).where(
            WalletTx.source.like(f"payout:{tournament_id}:%")).limit(1))
    return r.scalar() is not None


async def prize_pool(guild_id: str, tournament_id: int) -> int:
    """The tournament's prize wallet balance (settled tix available to pay out)."""
    async with db_session() as session:
        return await _pool(session, guild_id, tournament_id)


async def is_paid_out(tournament_id: int) -> bool:
    """True once any payout has been booked for this tournament (idempotency guard)."""
    async with db_session() as session:
        return await _already_paid(session, tournament_id)


async def execute_payout(guild_id: str, tournament_id: int, allocations: list) -> dict:
    """Disburse the prize pool to the winners' captains, atomically in one transaction.

    ``allocations`` is [(place, captain_id, team_name, amount)]. For each, debits the prize
    wallet and credits the captain (an internal transfer, so vault==SUM(wallets) holds).
    Idempotent by source ``payout:<tid>:<place>`` — a re-run books nothing. Refuses to pay
    more than the pool holds. Returns {ok, already_paid?, paid, total, pool} or {ok:False,error}."""
    prize_id = prize_wallet_id(tournament_id)

    async def _do():
        async with db_session() as session:
            if await _already_paid(session, tournament_id):
                return {"ok": True, "already_paid": True}

            pool = await _pool(session, guild_id, tournament_id)
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

    try:
        async with wallet_service.MONEY_LOCK:
            return await with_db_retry(_do)
    except IntegrityError:
        # uq_wallet_tx_transfer_legs: a concurrent payout booked first — re-run;
        # _do's _already_paid branch reports it.
        logger.info(f"execute_payout: tournament {tournament_id} paid out concurrently")
        return await _do()
