"""A per-draft prize pool: entry fees in, matched shares out.

Mirrors services/tournament_escrow_service.py deliberately. The pool is a
synthetic wallet holder, so every movement is an ordinary transfer between two
holders -- the system total never changes and wallet_service.reconcile keeps
working untouched.

Nothing here books a debt. That is the point: an entry that cannot be funded is
refused, so playing can never leave a player owing.

One function moves entry money, `set_entry`, and it reconciles rather than
appends: it reads what the player currently holds in the pool and moves only the
difference. That covers joining (0 -> n), revising (n -> m) and leaving (n -> 0)
without three separate rules to keep in agreement -- and it is why a player who
leaves and rejoins the same draft pays again, where an append-only "charge this
entry" keyed per (draft, player) would treat the rejoin as a settled retry and
seat them holding nothing.

Note the ledgers differ on refunds: tournament escrow keys a refund as
`refund:<original source>` and nets it off, because an entry is refunded at most
once. A pool entry can be refunded repeatedly -- unmatched excess, then teardown
-- so refunds here carry their own reason in the key.
"""
from operator import itemgetter
from typing import TypedDict

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from database.db_session import db_session
from database.retry import with_db_retry
from services import wallet_service


class EntryResult(TypedDict):
    """What moving a player's entry decided.

    `deficit` is how many more tix they need, and is 0 whenever `ok` is True,
    so a caller can show the number without re-deriving it.
    """
    ok: bool
    deficit: int


def pool_wallet_id(session_id: str) -> str:
    """The holder that owns one draft's pool."""
    return f"prize:draft:{session_id}"


def _refund_source(session_id: str, player_id: str, reason: str, moves: int) -> str:
    """Idempotency key for one refund.

    `reason` distinguishes the unmatched-share refund at matching from a later
    teardown refund, so one cannot silently swallow the other. `moves` counts
    the transfers already exchanged with the holder, for the same reason the
    entry key carries it: a player who joins, leaves, rejoins and leaves again
    arrives back at an identical (reason, session, player), and a key built from
    those alone would treat the second leave as a settled retry -- taking them
    out of the queue with their money still in the pool.
    """
    return f"draft-refund:{reason}:{session_id}:{player_id}:{moves}"


async def _queue_open_in(session: AsyncSession, session_id: str) -> bool:
    """_queue_is_open against an existing session/transaction."""
    from models.draft_session import DraftSession
    from sqlalchemy import select

    row = (await session.execute(
        select(DraftSession.session_stage)
        .where(DraftSession.session_id == session_id))).first()
    if row is None:
        # No draft, no open queue. Reading the stage alone conflated "still
        # queueing" with "the row is gone", so a component left open after its
        # draft was deleted charged the player into a holder that nothing will
        # ever settle or tear down -- and check_pool cannot see that money
        # either, because it also returns early once the row is missing.
        return False
    return row[0] is None


async def pool_balance(guild_id: str, session_id: str) -> int:
    """What the draft's holder currently owns."""
    return await wallet_service.get_balance(guild_id, pool_wallet_id(session_id))


async def contributions(guild_id: str, session_id: str) -> dict[str, int]:
    """Net tix each player currently has in the pool: entries minus refunds.

    Read from the ledger rather than from StakeInfo, because StakeInfo records
    what a player DECLARED and what settlement must divide by is what actually
    moved.
    """
    net = await wallet_service.contributions_to(
        guild_id, pool_wallet_id(session_id))
    # Positive only. A payout is money leaving the holder TO a player, so it
    # nets negative against their entry; counting that as a contribution makes a
    # settled pool report holdings below zero and match_pool compute a negative
    # matched total. What this answers is "still at risk", not "net traffic".
    return {player: held for player, held in net.items() if held > 0}


async def _held_in(session: AsyncSession, guild_id: str, session_id: str,
                   player_id: str) -> int:
    """What one player has at risk, read inside the caller's transaction.

    Clamped at zero for the same reason contributions() drops negatives: once a
    player has been paid out their net with the holder goes below zero, and a
    negative holding would read as a debt to the pool.
    """
    net = await wallet_service.net_between(
        session, guild_id, pool_wallet_id(session_id), player_id)
    return max(net, 0)


async def held_by(guild_id: str, session_id: str, player_id: str) -> int:
    """What one player currently has at risk in this draft."""
    async with db_session() as session:
        return await _held_in(session, guild_id, session_id, player_id)


async def _refund_in(session: AsyncSession, guild_id: str, session_id: str, player_id: str,
                     amount: int, reason: str) -> bool:
    """Return `amount` from the pool to the player, inside the caller's session.

    Refuses rather than driving the holder negative: a negative synthetic holder
    invents tix that were never deposited and surfaces in reconciliation as a
    system-total drift with no traceable cause.
    """
    if amount <= 0:
        raise ValueError("Refund amount must be positive")

    holder = pool_wallet_id(session_id)
    # Cap at THIS player's own contribution, not the holder's total. The holder
    # holds everyone's money, so a balance check alone would happily pay one
    # player out of their opponents' entries and still leave the pool positive.
    moves = await wallet_service.movements_in(session, guild_id, holder, player_id)
    held = await _held_in(session, guild_id, session_id, player_id)
    if amount > held:
        logger.error(
            f"draft pool {session_id}: refused to refund {amount} to {player_id} "
            f"({reason}) -- they hold only {held}. Refunding more would come out "
            f"of another player's entry.")
        return False

    source = _refund_source(session_id, player_id, reason, moves)
    if await wallet_service.transfer_legs(session, source):
        logger.info(f"draft pool {session_id}: refund {source} already booked")
        return True

    available = await wallet_service.balance_in(session, guild_id, holder)
    if amount > available:
        logger.error(
            f"draft pool {session_id}: cannot refund {amount} to {player_id} "
            f"({reason}) -- holder holds {available}. Refusing.")
        return False

    await wallet_service.transfer_in(
        session, guild_id, holder, player_id, amount, source,
        notes=f"Draft refund ({reason}) {session_id}")
    logger.info(f"draft pool {session_id}: refunded {amount} to {player_id} ({reason})")
    return True


async def refund_entry(guild_id: str, session_id: str, player_id: str,
                       amount: int, reason: str) -> bool:
    """Return `amount` from the pool to the player. True once the money is back."""
    async def _do():
        async with db_session() as session:
            return await _refund_in(session, guild_id, session_id, player_id,
                                    amount, reason)

    async with wallet_service.MONEY_LOCK:
        return await with_db_retry(_do)


async def set_entry(guild_id: str, session_id: str, player_id: str,
                    amount: int, reason: str = "revised") -> EntryResult:
    """Make this player's holding in the draft's pool equal `amount`.

    The ONE door money uses to enter or leave a queue. Expressing it as a target
    hold means joining, revising and leaving are one rule: charging the full
    figure again on a revision would double up, and keying an append-only charge
    per (draft, player) would make a rejoin look like a retry.

    A raise they cannot afford leaves the original entry untouched rather than
    stranding them between two amounts. `reason` tags the ledger when money
    comes back, so a teardown refund reads differently from a revision.

    Reads and writes in ONE transaction, under the same MONEY_LOCK every other
    transfer takes. The bot is single-threaded but not serialised -- asyncio
    interleaves at every await, and Discord dispatches each interaction as its
    own task -- so without this, two submissions from the same player both read
    "holds nothing" and both charge: eight overlapping clicks cost the sum of
    all eight rather than the one the player meant. Holding the lock across the
    whole read-modify-write, rather than only around the transfer, is what makes
    the delta this computes still true when it is written.
    """
    if amount < 0:
        raise ValueError("Entry amount cannot be negative")

    async def _do():
        async with db_session() as session:
            return await entry_in(session, guild_id, session_id,
                                  player_id, amount, reason)

    async with wallet_service.MONEY_LOCK:
        result = await with_db_retry(_do)

    # After the commit and outside the lock: check_pool opens its own reads, and
    # what it audits is committed state.
    if result["ok"]:
        await check_pool(guild_id, session_id)
    return result


async def entry_in(session: AsyncSession, guild_id: str, session_id: str,
                   player_id: str, amount: int,
                   reason: str = "revised") -> EntryResult:
    """Move a player's entry inside the CALLER's transaction.

    The atomic building block: a caller that also has roster state to write --
    the sign-up row, the StakeInfo, a leave -- passes its own session, and the
    money and the state commit together. Without that, a failure between the
    two commits leaves a player charged for a draft they are not in, and
    nothing reconciles it: check_pool deliberately says nothing while the queue
    is open, because a contributor who is not yet signed up is normal then.

    The caller must hold wallet_service.MONEY_LOCK for the whole transaction --
    set_entry is the version that does that for you when there is no state to
    write alongside.
    """
    holder = pool_wallet_id(session_id)
    held = await _held_in(session, guild_id, session_id, player_id)

    # "left" and "removed" are teardown: a player is leaving the draft, and
    # their money has to come with them whenever that happens. Every other
    # reason is a player revising a stake, which is what a stale panel replays.
    teardown = reason in ("left", "removed")
    if amount != held and not teardown and not await _queue_open_in(session, session_id):
        # Money may not move in EITHER direction once the queue closes. A
        # stake select or modal opened while queueing can be submitted after
        # teams are formed; money arriving then belongs to nobody the matching
        # pass considered, and money leaving then makes one side lighter than
        # the other. Both break the levelness the payout is derived from, and
        # the decrease is the worse of the two because it succeeds: the refund
        # commits, and every later mutation -- the payout included -- raises on
        # an invariant the player has no way to repair. Refuse here, so that
        # state is never reached rather than reconciled afterwards.
        #
        # Matching's own pro-rata refund and every teardown path call
        # refund_entry directly and are unaffected by this guard.
        logger.info(f"draft pool {session_id}: refusing a late stake change "
                    f"from {player_id} ({held} -> {amount}) -- the book has "
                    f"already closed")
        return {"ok": False, "deficit": 0}
    if amount == held:
        return {"ok": True, "deficit": 0}

    if amount < held:
        if not await _refund_in(session, guild_id, session_id, player_id,
                                held - amount, reason):
            # The holder could not cover it. Say so rather than reporting a
            # move that did not happen -- the caller must not tell a player
            # their stake changed when it did not.
            return {"ok": False, "deficit": 0}
        logger.info(f"draft pool {session_id}: {player_id} {held} -> {amount} ({reason})")
        return {"ok": True, "deficit": 0}

    delta = amount - held
    # The key counts movements, not balances. A player who joins, leaves and
    # rejoins returns to held=0, so a key built from the balances alone would
    # repeat the original join's key and be swallowed as a retry -- seating them
    # in a staked draft holding none of their money.
    moves = await wallet_service.movements_in(session, guild_id, holder, player_id)
    source = f"draft-entry:{session_id}:{player_id}:{moves}:{held}-{amount}"
    if await wallet_service.transfer_legs(session, source):
        logger.info(f"draft pool {session_id}: entry {source} already booked")
        return {"ok": True, "deficit": 0}

    balance = await wallet_service.balance_in(session, guild_id, player_id)
    if delta > balance:
        return {"ok": False, "deficit": delta - balance}

    await wallet_service.transfer_in(
        session, guild_id, player_id, holder, delta, source,
        notes=f"Draft entry {amount} ({session_id})")
    logger.info(f"draft pool {session_id}: {player_id} {held} -> {amount}")
    return {"ok": True, "deficit": 0}


# Tix are wagered in tens -- the queue offers 20, 50, 100, and multiples of 50
# above that -- so a matched stake of 96 is not a bet anyone placed.
_STAKE_STEP = 10

# A bet at or under this is a small bet, and is filled whole before anything is
# shared out. It is the tier boundary the queue has always advertised: "allocate
# all 20/50 bets, then proportionally distribute all remaining bets".
_SMALL_BET = 50


def level_side(held: dict[str, int], budget: int) -> dict[str, int]:
    """How much of each entry stays at risk when a side must shrink to `budget`.

    Small bets are filled whole, then the large ones share what is left in
    proportion to their size. Scaling EVERY bet by the same ratio is the
    intuitive answer and the wrong one: it shaves the player who bet the draft
    minimum down below that minimum to pay for headroom on a bet ten times the
    size. In one real draft, 20 against 170 and 200 came out as 12 / 96 / 112 --
    the 20-tix player under the 20-tix floor, and nobody holding a figure they
    would recognise as their bet. The same entries now come out 20 / 90 / 110.

    Backing more still means carrying more of the shortfall, which is what
    proportional buys over splitting the remainder equally: the 200 keeps more
    on the table than the 170 rather than both being flattened to the same cap.

    Everything is computed in units of ten and never exceeds a player's own
    entry, so a levelled stake is always a round number and always a number
    they agreed to. Whatever does not divide evenly is refunded by the caller.
    """
    caps = {p: n // _STAKE_STEP for p, n in held.items() if n >= _STAKE_STEP}
    remaining = budget // _STAKE_STEP
    alloc = {p: 0 for p in caps}

    small = {p: c for p, c in caps.items() if c * _STAKE_STEP <= _SMALL_BET}
    large = {p: c for p, c in caps.items() if c * _STAKE_STEP > _SMALL_BET}

    if sum(small.values()) <= remaining:
        # Every small bet in full, then the rest is the large bets' to share.
        for player, cap in small.items():
            alloc[player] = cap
            remaining -= cap
        sharing = large
    else:
        # Not even the small bets fit. Nobody gets filled whole, so everyone
        # shares on the same terms rather than the earliest-sorted winning.
        sharing = caps

    demand = sum(sharing.values())
    if demand and remaining > 0:
        # Largest remainder, not largest bet. Every share is rounded DOWN to a
        # whole ten and the units that fall off are handed to whoever lost the
        # most in the rounding. Awarding them by bet size instead lets the
        # biggest bet round up to its full amount while the smallest rounds
        # away to nothing -- which is the very squeeze this function exists to
        # prevent, reappearing at the last step.
        shortfall = []
        for player, cap in sharing.items():
            exact = remaining * cap
            whole = min(exact // demand, cap)
            alloc[player] += whole
            shortfall.append((exact - whole * demand, player))
        shortfall.sort(reverse=True)

        # One unit each, in remainder order, until the budget is exactly spent.
        # Handing a player every unit that fits would undo the split: the first
        # name in the list would round all the way up to its full bet.
        left = remaining - sum(alloc[p] for p in sharing)
        while left > 0:
            spent = left
            for _, player in shortfall:
                if left <= 0:
                    break
                if alloc[player] < caps[player]:
                    alloc[player] += 1
                    left -= 1
            if left == spent:
                break       # everyone is at their cap; the rest goes back

    return {p: n * _STAKE_STEP for p, n in alloc.items() if n}


async def match_pool(guild_id: str, session_id: str,
                     team_a: list[str], team_b: list[str]) -> dict[str, object]:
    """Cap both sides at the smaller side's total, returning the excess.

    Idempotent by construction rather than by a key: it refunds the difference
    between the sides, so once they are equal a second call finds nothing to
    refund. team_creator can be re-entered after a restart, and this has to be
    safe when it is.
    """
    held = await contributions(guild_id, session_id)
    sides = ([p for p in team_a if held.get(p)], [p for p in team_b if held.get(p)])
    # What each side can actually put up in whole tens. A stake is matched in
    # units of ten, so an entry of 25 backs 20 of the other side and hands back
    # the 5 -- and the figure the two sides meet at has to be one BOTH can
    # reach that way, not merely the smaller total.
    totals = [sum(held[p] // _STAKE_STEP * _STAKE_STEP for p in side)
              for side in sides]
    matched = min(totals)

    refunded: dict[str, int] = {}
    for side in sides:
        stays = level_side({p: held[p] for p in side}, matched)
        for player_id in side:
            excess = held[player_id] - stays.get(player_id, 0)
            if excess > 0 and await refund_entry(guild_id, session_id, player_id,
                                                 excess, "unmatched"):
                refunded[player_id] = excess

    logger.info(f"draft pool {session_id}: matched at {matched} a side, "
                f"refunded {sum(refunded.values())} unmatched")
    await check_pool(guild_id, session_id)
    return {"matched": matched, "refunded": refunded}


def _payout_source(session_id: str, player_id: str) -> str:
    return f"draft-payout:{session_id}:{player_id}"


async def settle_pool(guild_id: str, session_id: str,
                      winning_team: list[str]) -> dict[str, dict[str, int]]:
    """Split the pool among the winning team, in proportion to what each has at
    risk. One transfer per winner, and the holder is empty afterwards.

    Idempotent by source: the victory path can be re-entered, and paying twice
    out of an already-empty holder would fail rather than duplicate -- but the
    source guard means it does not even try.
    """
    # Before moving anything: the pool must be in a state a draft can be in.
    await check_pool(guild_id, session_id)

    held = await contributions(guild_id, session_id)
    winners = {p: held[p] for p in winning_team if held.get(p)}
    balance = await pool_balance(guild_id, session_id)

    if not winners or balance <= 0:
        logger.info(f"draft pool {session_id}: nothing to settle "
                    f"({len(winners)} winners, holder {balance})")
        return {"paid": {}}

    # Every winner doubles their matched stake. That is not a rounding-friendly
    # approximation of a proportional split -- it is exact, and it is exact
    # BECAUSE matching levelled the sides: both totalled M, so the pool is 2M,
    # and a winner holding c takes 2M * c / M = 2c. No division, no remainder.
    shares = {player: held * 2 for player, held in winners.items()}

    # No check here. check_pool has already established that the sides are
    # level, which is precisely what makes `owed == balance` -- so a discrepancy
    # cannot have survived to this line. Re-testing it here would be asking the
    # same question in a worse place: at payout every cause looks alike, whereas
    # the invariant raises at the mutation that broke it.

    async def _do() -> dict[str, int]:
        # Every winner in ONE transaction. Paying them one at a time looks
        # harmless because each transfer is idempotent, but a failure between
        # two of them commits the first and leaves the holder half empty with
        # the sides no longer level -- and the next attempt runs check_pool,
        # sees the imbalance it caused, and refuses. An error in the middle of
        # paying a draft out would make that draft unsettleable forever.
        holder = pool_wallet_id(session_id)
        settled: dict[str, int] = {}
        async with db_session() as session:
            for player_id, amount in shares.items():
                if amount <= 0:
                    continue
                source = _payout_source(session_id, player_id)
                if not await wallet_service.transfer_legs(session, source):
                    await wallet_service.transfer_in(
                        session, guild_id, holder, player_id, amount, source,
                        notes=f"Draft winnings {session_id}")
                settled[player_id] = amount
        return settled

    async with wallet_service.MONEY_LOCK:
        paid = await with_db_retry(_do)

    logger.info(f"draft pool {session_id}: paid {sum(paid.values())} to {len(paid)} winners")
    await check_pool(guild_id, session_id)
    return {"paid": paid}


async def release_draft_pool(guild_id: str, session_id: str,
                             reason: str) -> dict[str, dict[str, int]]:
    """Empty a draft's pool back to its contributors.

    One idempotent function for every path that ends a draft early. Idempotence
    -- not an event bus -- is what makes several callers safe, and it is why
    calling this on a draft that never had a pool is a no-op rather than an
    error: most drafts are not staked, and every teardown path calls it anyway.
    """
    held = await contributions(guild_id, session_id)
    refunded: dict[str, int] = {}
    for player_id, amount in held.items():
        if await refund_entry(guild_id, session_id, player_id, amount, reason):
            refunded[player_id] = amount
    if refunded:
        logger.info(f"draft pool {session_id}: released {sum(refunded.values())} "
                    f"to {len(refunded)} players ({reason})")
    await check_pool(guild_id, session_id)
    return {"refunded": refunded}


async def settle_draw(guild_id: str, session_id: str) -> dict[str, dict[str, int]]:
    """A drawn draft: give every entry back.

    "A draw pays nobody" is only half an instruction. Settlement is victory-only,
    so a drawn draft never reaches settle_pool -- and if nothing else empties the
    holder, every player's entry stays in it while the draft is marked completed,
    with no later path that would ever attribute the money. Paying nobody has to
    mean refunding everybody.
    """
    return await release_draft_pool(guild_id, session_id, "draw")


class PoolInvariantViolated(RuntimeError):
    """The pool is not in a state the draft can be in.

    Raised at the point of corruption rather than discovered later at payout.
    Every mutation re-establishes the invariant below, so whichever call raises
    is the one that broke it -- which is the whole reason for checking after
    each, instead of once at settlement where every cause looks alike.
    """


async def check_pool(guild_id: str, session_id: str) -> None:
    """THE invariant. True after every mutation, in every phase of a draft.

    1. The holder owns exactly the net of what has passed through it.
       Compared against the RAW net, not the at-risk view: once a winner is
       paid, their net goes negative, so the at-risk figure legitimately exceeds
       an emptied holder. The point of this clause is to notice money moving in
       or out of prize:draft:<id> by some route other than this module.

    2. Every contributor is playing this draft.
       Settlement pays teams, so a stranger's tix would be handed to the
       winners. Only checked once the book has closed: the entry is charged
       BEFORE the sign-up row is written, deliberately, so a player who cannot
       pay leaves nothing to unwind -- and between those two writes a
       contributor is legitimately not yet in sign_ups.

    3. Once the book is closed, the two sides hold equal amounts.
       This is what makes the payout exact: both sides at M means the holder is
       2M and a winner holding c takes exactly 2c. Before the book closes the
       sides do not exist; after the pool empties there is nothing to be
       unequal.

    Raises PoolInvariantViolated naming the clause and the numbers. The only
    valid outcome is silence.
    """
    from database.db_session import db_session
    from models.draft_session import DraftSession
    from sqlalchemy import select

    net = await wallet_service.contributions_to(guild_id, pool_wallet_id(session_id))
    balance = await pool_balance(guild_id, session_id)

    if balance != sum(net.values()):
        raise PoolInvariantViolated(
            f"draft pool {session_id}: the holder owns {balance} but its transfers "
            f"net to {sum(net.values())}. Something outside this module moved money "
            f"in or out of prize:draft:{session_id}.")

    if balance == 0:
        return          # nothing at risk: settled, released, or never funded

    async with db_session() as session:
        row = (await session.execute(
            select(DraftSession.session_stage, DraftSession.team_a,
                   DraftSession.team_b, DraftSession.sign_ups)
            .where(DraftSession.session_id == session_id))).first()
    if row is None:
        # Money held for a deleted draft IS stranded -- but that is a teardown
        # ORDERING fault, prevented by releasing the pool before the row is
        # deleted and asserted by its own test. Nothing this function can say
        # about the sides applies once there are no sides to read.
        return

    stage, team_a, team_b, sign_ups = row
    if stage is None or not (team_a and team_b):
        return          # the book is still open; the sides do not exist yet

    held = {p: n for p, n in net.items() if n > 0}
    strangers = set(held) - (set(sign_ups or {}) | set(team_a or []) | set(team_b or []))
    if strangers:
        raise PoolInvariantViolated(
            f"draft pool {session_id}: {sorted(strangers)} hold money but are not "
            f"playing. Settlement pays teams, so their tix would go to the winners.")

    a = sum(held.get(p, 0) for p in team_a)
    b = sum(held.get(p, 0) for p in team_b)
    if a != b:
        raise PoolInvariantViolated(
            f"draft pool {session_id}: the book has closed but the sides hold {a} "
            f"and {b}. They were never levelled, so no payout is derivable -- a "
            f"winner cannot take double a stake that was never matched.")


async def format_entries(guild_id: str, session_id: str,
                         sign_ups: dict[str, str]) -> tuple[list[str], int]:
    """What each player has at risk, for the teams embed.

    Replaces get_formatted_stake_pairs under the pool. There are no pairs to
    name: everyone is in against everyone, which is the whole point of the
    change -- a player needs to know what they put in and what the pot is, not
    who they happen to be matched against.
    """
    held = await contributions(guild_id, session_id)
    if not held:
        return [], 0
    ranked = sorted(held.items(), key=itemgetter(1), reverse=True)
    lines = [f"**{sign_ups.get(p, 'Unknown')}**: {n} tix" for p, n in ranked]
    return lines, sum(held.values())


async def format_outcomes(guild_id: str, session_id: str, sign_ups: dict[str, str],
                          winning_team: list[str]) -> tuple[list[str], int]:
    """What each player won or lost, for the victory embed.

    Read from the payouts the pool actually made rather than from pairings,
    so the numbers a player reads are the numbers that moved. Nobody owes
    anybody: the money changed hands when the result was confirmed.
    """
    ledger = await wallet_service.contributions_to(guild_id, pool_wallet_id(session_id))
    if not ledger:
        return [], 0

    winners = set(winning_team or [])
    lines: list[str] = []
    pot = 0
    for player_id, net in sorted(ledger.items(), key=itemgetter(1)):
        name = sign_ups.get(player_id, "Unknown")
        if player_id in winners:
            # net is negative for a paid winner: they put in c and took 2c.
            lines.append(f"**{name}** won {-net} tix")
            pot += -net
        elif net > 0:
            lines.append(f"**{name}** lost {net} tix")
    return lines, pot
