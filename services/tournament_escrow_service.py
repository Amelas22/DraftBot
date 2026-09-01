"""
Tournament entry fees — the tournament side of the tix ledger, bridging tournament_service
(the participant row) and wallet_service (the money).

Every movement is a plain transfer between wallet holders, tagged with a per-participant
``source`` so it's idempotent and reversible:
  * secure   — transfer the fee from the captain into the tournament's PRIZE WALLET and
               flip the participant to 'paid', in one transaction. A short wallet moves
               nothing and reports the deficit; the team stays pending until the tix
               arrive (see sweep_pending_entries), however they arrive.
  * refund   — transfer it back out of the pot when a team drops before start.
  * on start — nothing to move: the pot was funded as each team registered.
  * payout   — transfer from the pot to the winners' captains.

The prize wallet is an ordinary holder with a synthetic id (``prize:tourney:<id>``), so the
pot is counted by the reconciliation audit and pays out like any other wallet. Because
transfers net to zero, vault == SUM(wallets) holds at every instant by construction.
"""
from datetime import datetime

from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from database.db_session import db_session
from database.retry import with_db_retry
from models.tournament import Tournament, TournamentParticipant
from models.wallet_tx import WalletTx
from services import wallet_service
from services.wallet_service import prize_wallet_id
from services.tournament_service import (get_active_tournament, list_participants,
                                         remove_team, start_tournament)


# How many unpaid team names the start refusal spells out before summarising the rest.
# 20 names sits comfortably inside Discord's 2000-char message limit at any roster size.
_UNPAID_NAMES_SHOWN = 20


def escrow_source(tournament_id, participant_id) -> str:
    """LEGACY entry key, kept for READING only — entries booked before the move to
    entry_source() are tagged this way and must still be findable by a refund and by
    the sweep's already-paid probe. Never write a new transfer under it."""
    return f"tourney:{tournament_id}:{participant_id}"


def entry_source(tournament_id, team_id, attempt: int = 0) -> str:
    """Idempotency key for one of a team's entry fees, and the handle a refund
    reverses.

    Keyed on the persistent Team identity, never on the participant row.
    ``remove_team`` deletes the participant, and tournament_participants.id is a plain
    SQLite rowid alias (no AUTOINCREMENT), so the next registrant inherits the freed
    id. A participant-keyed source then collided with the dropped team's already-booked
    transfer: the sweep saw "already paid", stamped the newcomer paid without charging
    them, and left the pot one fee short. The Team row outlives a drop, so this key
    cannot be recycled.

    ``attempt`` numbers the registrations. The ledger keeps one 'pay' and one
    'receive' per source -- enforced by uq_wallet_tx_transfer_legs -- so a team
    that drops and registers again cannot book a second pair under the same key:
    the write is rejected and the team is left unpaid. A refund does not remove
    the first entry, it books a compensating pair, so the key has to move on
    with each registration rather than being reused.

    Attempt 0 keeps the original string, so every entry already on the books
    keeps the key it was written under and stays findable by a refund.
    """
    base = f"tourney:{tournament_id}:team:{team_id}"
    return base if not attempt else f"{base}#{attempt}"


def _attempt_of(base: str, source: str) -> int:
    """Which registration a booked source belongs to. Attempt 0 is the bare key."""
    return 0 if source == base else int(source.rsplit("#", 1)[1])


async def _entry_sources(session, tournament_id: int, team_id) -> list[str]:
    """Every source this team's entry has been booked under, oldest first."""
    base = entry_source(tournament_id, team_id)
    rows = (await session.execute(
        select(WalletTx.source).where(
            WalletTx.kind == "receive",
            or_(WalletTx.source == base,
                WalletTx.source.like(f"{base}#%")),
        ).distinct())).scalars().all()
    return sorted((str(r) for r in rows), key=lambda src: _attempt_of(base, src))


def _next_attempt(base: str, booked: list[str]) -> int:
    """The registration number to book the next entry under.

    One past the highest already used, not the count: counting assumes the
    numbers have no gaps, and a single missing one would send this back to a
    key the ledger already holds, where the unique index rejects it.
    """
    return max((_attempt_of(base, src) for src in booked), default=-1) + 1


async def _booked_entry(session, tournament_id: int, participant: TournamentParticipant,
                        booked: list[str] | None = None):
    """``(credit_leg, source)`` for this team's entry fee if it is still STANDING,
    else ``(None, None)``.

    Tries the current Team-keyed source, then the legacy participant-keyed one. Both
    readers need this: a refund has to find the leg to know what to return, and the
    sweep has to recognise an entry it already charged so it doesn't bill twice.

    "Still standing" is the load-bearing part. The ledger is append-only, so a refund
    does not remove the entry leg — it books a compensating pair under ``refund:<src>``
    and the original stays forever. An existence check would therefore read a refunded
    entry as paid, and since a re-registered team keeps its Team identity, dropping and
    re-registering would seat it for free every time. Netting the refund off is what
    makes the key safe to reuse across registrations rather than merely unrecycled.
    """
    # Latest registration first: an older, refunded entry must not mask the one
    # standing now.
    if booked is None:
        booked = await _entry_sources(session, tournament_id, participant.team_id)
    for src in list(reversed(booked)) + [escrow_source(tournament_id, participant.id)]:
        leg = await wallet_service.transfer_credit(session, src)
        if leg is None:
            continue
        if await wallet_service.transfer_legs(session, f"refund:{src}"):
            continue  # paid once, then refunded — this entry no longer stands
        return leg, src
    return None, None


# Prize-pool split presets (top-heavy, per MTG convention). Ratios need not sum to 100 —
# compute_allocations normalizes by the sum of the places actually paid.
PAYOUT_STRUCTURES = {
    "winner_take_all": [100],
    "top2": [65, 35],
    "top3": [50, 30, 20],
    "top4": [40, 30, 20, 10],
}

# top25pct — the league-page structure — pays 25% of the field (rounded down,
# minimum one place) rather than a fixed place count, so its share list is
# derived per-field in _structure_ratios instead of living in the dict above.
# Stepped ladder: 5/3/2/2 for the first four places, 2 shares for places 5-6,
# and 1 share for every place beyond — so first place's cut scales up as a
# larger field adds low tail places instead of being diluted by them.
TOP25_SHARES = [5, 3, 2, 2, 2, 2]
TOP25_EXTENSION_SHARE = 1

# Everything a create/payout command may name: static presets plus the dynamic one.
PAYOUT_CHOICES = list(PAYOUT_STRUCTURES) + ["top25pct"]


def describe_structure(name: str) -> str:
    if name == "top25pct":
        return "top 25% of teams (5/3/2/2 shares)"
    ratios = PAYOUT_STRUCTURES.get(name)
    if not ratios:
        return name
    if name == "winner_take_all":
        return "winner-take-all"
    return f"top {len(ratios)} ({'/'.join(str(r) for r in ratios)})"


def _structure_ratios(structure: str, field_size: int) -> list:
    """The share list a structure pays over a field of ``field_size`` teams."""
    if structure == "top25pct":
        places = max(1, field_size // 4)
        return (TOP25_SHARES + [TOP25_EXTENSION_SHARE] * (places - len(TOP25_SHARES)))[:places]
    return PAYOUT_STRUCTURES.get(structure) or PAYOUT_STRUCTURES["winner_take_all"]


def compute_allocations(pool: int, structure: str, ranked: list) -> list:
    """Split ``pool`` tix over the ranked teams by the named structure.

    ``ranked`` is [(captain_id, team_name)] in finish order (1st first). Returns
    [(place, captain_id, team_name, amount)] for amounts > 0. Integer tix; the whole pool is
    always distributed — normalized to the places actually paid (so fewer teams than the
    structure names just concentrates the split), with the rounding remainder going to 1st."""
    ratios = _structure_ratios(structure, len(ranked))
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


def _mark_paid(p: TournamentParticipant):
    """Stamp a participant paid inside the caller's session."""
    p.status = "paid"
    p.paid_at = datetime.now()


def comp(participant: TournamentParticipant) -> bool:
    """Admin comp: mark a participant paid with NO fee (seed-eligible, captain unbilled).
    Runs inside the CALLER's session so registration + comp commit atomically. Stamps
    paid_at so a comped row is distinguishable from a pre-escrow legacy one; no tix back
    this entry, so it dilutes the pot on purpose."""
    if participant.status == "paid":
        return False
    _mark_paid(participant)
    logger.info(f"escrow: participant {participant.id} comped (no fee)")
    return True


async def secure_from_wallet(guild_id: str, captain_id: str, participant_id: int,
                             tournament_id: int, fee: int, team_name: str) -> dict:
    """Transfer ``fee`` tix from the captain into the tournament's prize wallet and mark
    the team paid — one transaction, so a crash can't leave a paid team that never paid,
    or a charged captain whose team is still pending.

    Returns one of:
      {ok: True, done: True, paid: n}        — fee is in the pot, participant now paid.
      {ok: True, done: False, deficit: n}    — wallet short by ``deficit``; nothing moved.
      {ok: False, error: str}                — participant vanished mid-flight.
    Idempotent by ``source``: a re-run finds the transfer already booked and just
    re-stamps the participant."""
    if fee <= 0:
        raise ValueError("secure_from_wallet needs a positive fee (free entries never pay)")

    prize_id = prize_wallet_id(tournament_id)

    async def _do():
        async with db_session() as session:
            p = await session.get(TournamentParticipant, participant_id)
            if p is None:
                return {"ok": False, "error": "team no longer registered"}

            # Read once and reuse: the probe and the next-attempt number ask
            # the same question of the ledger.
            booked = await _entry_sources(session, tournament_id, p.team_id)
            leg, _ = await _booked_entry(session, tournament_id, p, booked)
            if leg is not None:
                _mark_paid(p)
                return {"ok": True, "done": True, "paid": fee, "reused": True}

            balance = await wallet_service.balance_in(session, guild_id, captain_id)
            if balance < fee:
                return {"ok": True, "done": False, "deficit": fee - balance, "available": balance}

            # Names the Team, which a drop cannot free, plus which registration
            # this is — see entry_source. Re-using the key after a refund is
            # rejected by the ledger's unique index and leaves the team unpaid.
            attempt = _next_attempt(entry_source(tournament_id, p.team_id), booked)
            await wallet_service.transfer_in(
                session, guild_id, captain_id, prize_id, fee,
                entry_source(tournament_id, p.team_id, attempt),
                notes=f"tournament entry: {team_name}")  # funds checked just above
            _mark_paid(p)
            logger.info(f"escrow: participant {participant_id} paid {fee} into {prize_id}")
            return {"ok": True, "done": True, "paid": fee}

    async with wallet_service.MONEY_LOCK:
        return await with_db_retry(_do)


async def refund_entry(session, guild_id: str, tournament_id: int,
                       participant: TournamentParticipant) -> int:
    """Return a paid entry fee from the pot to its captain, inside the caller's session.
    Idempotent by the ``refund:`` source; returns the amount refunded (0 if the entry was
    never paid, e.g. a comp, or was already refunded)."""
    # Reverses whichever key the entry was actually booked under, so a fee paid before
    # the key changed still refunds — and stays idempotent against its own refund tag.
    leg, source = await _booked_entry(session, tournament_id, participant)
    if leg is None:
        return 0  # comped or never funded
    refund_source = f"refund:{source}"
    if await wallet_service.transfer_legs(session, refund_source):
        return 0  # already refunded
    # No funds check: this unwinds a pot the entry itself funded, so the claim is the
    # captain's by right (payout is post-finish; refunds are registration-only).
    await wallet_service.transfer_in(
        session, guild_id, prize_wallet_id(tournament_id), participant.captain_user_id,
        leg.amount, refund_source, notes=f"entry refund: {participant.team_name}")
    return leg.amount


def _unpaid_entry_conditions(captain_id: str = None) -> list:
    """What counts as a still-open entry: a fee-charging tournament still taking
    registrations, with a team that hasn't paid. One definition, used by both the sweep
    that completes such entries and the lookup of boards whose figures they drive."""
    conditions = [
        Tournament.status == "registration",
        Tournament.entry_fee > 0,
        TournamentParticipant.status != "paid",
    ]
    if captain_id:
        conditions.append(TournamentParticipant.captain_user_id == captain_id)
    return conditions


async def open_boards_for_captain(captain_id: str) -> list[int]:
    """Tournament ids where ``captain_id`` still has an unpaid entry.

    Those boards render "needs N more tix" from this captain's balance, so a partial
    deposit (or an auto-draw that spends one) moves N without completing anything —
    the sweep's completed-ids alone would leave them stale."""
    async with db_session() as session:
        return list((await session.execute(
            select(Tournament.id)
            .join(TournamentParticipant,
                  TournamentParticipant.tournament_id == Tournament.id)
            .where(*_unpaid_entry_conditions(captain_id))
            .distinct())).scalars().all())


async def open_registration_boards() -> list[int]:
    """Every tournament still taking registrations. The watchdog re-renders all of them
    each tick: balances move by routes no board-refresh call site covers (a teammate's
    /wallet pay, a withdraw, a debt settlement), so a periodic re-render is what keeps
    'needs N more tix' honest without chasing each money path."""
    async with db_session() as session:
        return list((await session.execute(
            select(Tournament.id).where(Tournament.status == "registration")
        )).scalars().all())


async def sweep_pending_entries(captain_id: str = None) -> list[int]:
    """Complete every pending entry whose captain's wallet can now cover the fee.

    A registration is only complete once the tix are actually in the vault — but it must
    not die just because one trade window closed while the custodian was busy with someone
    else's job. So the pending team is durable: it stays registrable until the tournament
    starts, and this sweep (run on the watchdog tick) finishes it as soon as the funds are
    there, no matter how they arrived — the retried deposit, a plain /wallet deposit, or a
    teammate's /wallet pay. secure_from_wallet is a no-op when the wallet is still short,
    so a sweep over an underfunded entry costs one balance read and changes nothing.

    Pass ``captain_id`` to scope it to one person (after their deposit lands, only their
    entries can have become payable). Returns the tournament ids whose entries completed."""
    conditions = _unpaid_entry_conditions(captain_id)
    async with db_session() as session:
        rows = (await session.execute(
            select(TournamentParticipant, Tournament)
            .join(Tournament, TournamentParticipant.tournament_id == Tournament.id)
            .where(*conditions))).all()
    completed = []
    for participant, tournament in rows:
        try:
            res = await secure_from_wallet(
                str(tournament.guild_id), participant.captain_user_id, participant.id,
                tournament.id, tournament.entry_fee, participant.team_name)
        except Exception as e:
            logger.warning(f"sweep_pending_entries: {participant.team_name} failed: {e}")
            continue
        if res.get("done"):
            completed.append(tournament.id)
            logger.info(f"sweep: '{participant.team_name}' completed registration for "
                        f"'{tournament.name}' (funds arrived)")
    return completed


async def close_registration_and_seed(guild_id, rng) -> dict:
    """Close registration and seed the schedule; the pot was funded as teams registered,
    so nothing moves here. MONEY_LOCK serializes a racing second /tournament start past
    the status check. Returns {tournament_id, name, fee, pot}; raises ValueError with a
    user-facing message when there's nothing to start."""
    async with wallet_service.MONEY_LOCK:
        async with db_session() as session:
            tournament = await get_active_tournament(session, guild_id)
            if tournament is None:
                raise ValueError("There is no tournament to start.")
            if tournament.status != "registration":
                raise ValueError(f"**{tournament.name}** has already started.")
            fee = tournament.entry_fee or 0
            # Refuse rather than warn. remove_team is registration-only, so a start
            # that went ahead would flip the tournament to 'active' and take away the
            # only tool for clearing the teams it just complained about — the warning
            # would land one moment after it could be acted on. Stopping here leaves
            # the TO in the phase where dropping still works, and the choice between
            # dropping and waiting for payment stays theirs. Checked before
            # start_tournament's own "at least 2 paid teams" guard so the more
            # actionable message wins. Free tournaments mark everyone 'paid', so this
            # is a no-op there.
            unpaid = sorted(p.team_name for p in await list_participants(session, tournament.id)
                            if p.status != "paid")
            if unpaid:
                # Naming every team is unbounded, and this reaches Discord as message
                # content (2000 chars). A league that grew 16 -> 42 teams in four days
                # can outrun that, and the failure would be the same shape as the bug
                # this guard exists to avoid: the command blows up instead of telling
                # the TO what to do. Name enough to act on, count the rest.
                shown, extra = unpaid[:_UNPAID_NAMES_SHOWN], len(unpaid) - _UNPAID_NAMES_SHOWN
                names = ", ".join(shown) + (f", and {extra} more" if extra > 0 else "")
                who = (f"{unpaid[0]} hasn't paid the entry fee" if len(unpaid) == 1
                       else f"{len(unpaid)} teams haven't paid the entry fee: {names}")
                raise ValueError(
                    f"{who}. Drop them with `/tournament remove_team`, or wait for "
                    f"payment, then run `/tournament start` again."
                )
            await start_tournament(session, tournament.id, rng)
            pot = await _pool(session, str(guild_id), tournament.id) if fee > 0 else 0
            return {"tournament_id": tournament.id, "name": tournament.name,
                    "fee": fee, "pot": pot}


async def drop_with_refund(tournament_id: int, team_name: str) -> dict:
    """Remove a team AND refund its entry fee out of the prize wallet, atomically in one
    transaction (so the participant delete and the refund can never half-apply — a team
    can't end up removed with its fee stuck in the pot). Registration phase only.

    Returns {team_name, refunded}. Raises ValueError like tournament_service.remove_team
    (which owns the removal rules; this only adds the refund)."""
    async with wallet_service.MONEY_LOCK:
        async with db_session() as session:
            p = await remove_team(session, tournament_id, team_name)
            tournament = await session.get(Tournament, tournament_id)
            guild_id = str(tournament.guild_id)
            captain_id = str(p.captain_user_id)
            dropped_name = p.team_name
            refunded = await refund_entry(session, guild_id, tournament_id, p)
            logger.info(f"escrow: dropped '{dropped_name}' from tournament {tournament_id} "
                        f"(refunded {refunded})")
            result = {"team_name": dropped_name, "refunded": refunded}

    # AFTER the lock (see execute_payout): settling reaches settle_debt_from_wallet, which
    # takes MONEY_LOCK and it is not reentrant, and DMs are network I/O. Neither belongs
    # inside the lock.
    if refunded:
        from notification_service import notify_entry_refund
        from services import mtgo_resolution_service as resolution
        await resolution.on_inflow(guild_id, captain_id, notify_entry_refund,
                                   refunded, team_name=dropped_name)
    return result


async def _pool(session, guild_id: str, tournament_id: int) -> int:
    """The prize wallet's balance inside an existing session — entry fees in, refunds and
    payouts out. Just a wallet balance now, so the preview and the payout cap agree by
    construction."""
    return await wallet_service.balance_in(session, guild_id, prize_wallet_id(tournament_id))


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
    more than the pool holds. Returns {ok, already_paid?, paid, total, pool, tournament_name}
    or {ok:False,error}.

    ``tournament_name`` is reporting only, never money: it is what lets a caller tell a
    human WHICH event paid. The id alone is enough to move the tix and no longer enough
    once the result reaches a log line or a player. It is None if the tournament row is
    gone — a missing name must not fail a payout."""
    prize_id = prize_wallet_id(tournament_id)

    async def _do():
        async with db_session() as session:
            if await _already_paid(session, tournament_id):
                return {"ok": True, "already_paid": True}

            tournament = await session.get(Tournament, tournament_id)
            t_name = tournament.name if tournament is not None else None
            pool = await _pool(session, guild_id, tournament_id)
            total = sum(amount for _, _, _, amount in allocations)
            if total > pool:
                return {"ok": False, "error": f"allocations ({total}) exceed the prize pool ({pool})"}

            for place, captain_id, team_name, amount in allocations:
                if amount <= 0:
                    continue
                # no per-leg funds check: the pool cap above covers the whole payout
                await wallet_service.transfer_in(
                    session, guild_id, prize_id, captain_id, amount,
                    f"payout:{tournament_id}:{place}",
                    notes=f"tournament prize (place {place}): {team_name}")
            await session.flush()
            logger.info(f"payout: '{t_name}' ({tournament_id}) distributed {total} tix to "
                        f"{len(allocations)} team(s)")
            return {"ok": True, "paid": allocations, "total": total, "pool": pool,
                    "tournament_name": t_name}

    async with wallet_service.MONEY_LOCK:
        result = await with_db_retry(_do)

    # AFTER the lock, and in ONE pass over allocations rather than a loop per concern:
    # settling reaches settle_debt_from_wallet, which takes MONEY_LOCK itself and the lock
    # is not reentrant, and DMs are network I/O. A prize is an inflow, so a winner who owes
    # someone pays them from it rather than being able to withdraw it first.
    # Imported here, not at module scope: mtgo_resolution_service imports this module the
    # same deferred way (see its sweep at resume time), so both directions stay lazy.
    # Skipped on the already_paid short-circuit, so a re-run cannot re-announce prizes.
    if result.get("ok") and not result.get("already_paid"):
        from notification_service import notify_tournament_payout
        from services import mtgo_resolution_service as resolution
        for place, captain_id, team_name, amount in allocations:
            if amount > 0:
                await resolution.on_inflow(
                    guild_id, captain_id, notify_tournament_payout, amount,
                    place=place, team_name=team_name,
                    tournament_name=result.get("tournament_name"))
    return result
