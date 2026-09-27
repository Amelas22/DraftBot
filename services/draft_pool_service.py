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
from typing import Awaitable, Callable, Iterable, TypedDict, TypeVar

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


_T = TypeVar("_T")


async def _money_transaction(work: Callable[[AsyncSession], Awaitable[_T]]) -> _T:
    """Run `work` in ONE transaction, under MONEY_LOCK, retried while the
    database is locked. Every write this module makes goes through here."""
    async def _do() -> _T:
        async with db_session() as session:
            return await work(session)

    async with wallet_service.MONEY_LOCK:
        return await with_db_retry(_do)


class PoolNotSettled(RuntimeError):
    """A batch of refunds could not all be made, so none of them was.

    Raised with every refund in the batch rolled back, so the pool is exactly
    as it was before the attempt.
    """


# What to tell players when a release fails after their draft has already
# ended (abandoned, scrapped, cancelled). Nothing was lost -- a failed release
# leaves the pool untouched -- but nothing will return the money on its own.
ENTRIES_STILL_HELD = ("⚠️ The entries couldn't be returned automatically. They're "
                      "still held safely in the pool -- an admin needs to return them.")


async def _refund_all_in(session: AsyncSession, guild_id: str, session_id: str,
                         refunds: list[tuple[str, int, str]]) -> None:
    """Book every (player, amount, reason) refund inside the caller's transaction.

    A refund that is refused raises, so the caller's transaction rolls back with
    every refund before it: a pool is never left half-refunded. The reason keys
    each refund for idempotency and names it in the ledger.
    """
    for player_id, amount, reason in refunds:
        if not await _refund_in(session, guild_id, session_id, player_id, amount, reason):
            raise PoolNotSettled(
                f"draft pool {session_id}: the refund of {amount} to {player_id} "
                f"({reason}) was refused, so none was made")


async def refund_entry(guild_id: str, session_id: str, player_id: str,
                       amount: int, reason: str) -> bool:
    """Return `amount` from the pool to the player. True once the money is back."""
    return await _money_transaction(lambda session: _refund_in(
        session, guild_id, session_id, player_id, amount, reason))


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

    result = await _money_transaction(lambda session: entry_in(
        session, guild_id, session_id, player_id, amount, reason))

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
        # Matching books its refunds through _refund_in and every teardown path
        # calls refund_entry; neither goes through here, so this guard does not
        # touch them.
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

# How much of their own side a capped player may carry.
#
# 55%, from the history rather than taste. The win rate of a side breaks at this
# line: its top entry wins 52.1% while holding 50-55% of the side and 44.5% at
# 55-60%. It also matters exactly where the line falls -- 100 beside two
# teammates on 50 is the single most common roster in the history (164 of them)
# and sits at precisely 50%, so a 50% ceiling would clip the shape that is doing
# fine. 55% leaves it alone and still reaches three quarters of every band below
# even money.
CAP_SHARE = 0.55


def snap_to_step(amount: int) -> int:
    """`amount` rounded DOWN to a whole stake step.

    Down, never up. This is a ceiling: rounding 85 up to 90 would let a player
    hold a larger share of their side than the one they opted into, which is the
    only thing the cap promises.

    To the STEP, not to the entries the dropdown offers (10/20/50/100, then
    50s). Snapping to those overshoots badly, because nothing sits between 50 and
    100: an allowance of 97 would become 50, refunding nearly twice what the rule
    asks and leaving the player at 38% of their side rather than 55%. Measured
    over the same rosters, ten-granularity lands every case at 50-55% where
    bucket-granularity ranged 33-53%. The step is also what the rest of the
    system already quotes -- level_side hands out 30, 70 and 90 routinely -- so a
    ceiling of 80 is no stranger a figure than a levelled draft already shows.
    """
    return max(amount, 0) // _STAKE_STEP * _STAKE_STEP


def max_pool(stakes: Iterable[int]) -> int:
    """The biggest pot this queue could play for, over every legal split of it.

    An UPPER bound, not an exact figure, and deliberately so. It models
    levelling only -- an opted-in entry cap (cap_targets) can lower the
    achievable pot below this, because a capped player's ceiling depends on the
    teammates they are drawn WITH. On the prod copy that bites 22 of 917 real
    queues (2.4%), median 40 tix, worst case 900 advertised against 320.

    Exactness is not merely unimplemented here, it is unattainable at the point
    this is called: the signup board renders it while players are still joining,
    and the cap flags of players who have not joined yet cannot be known. So the
    board says "up to N", which stays true, and
    test_the_advertised_pot_is_an_UPPER_bound_once_entries_are_capped pins the
    inequality rather than an equality the cap can break.

    match_pool caps both sides at the smaller side's whole-ten TOTAL, so the
    holder ends up with twice that. This asks which split of the current queue
    makes that figure largest -- teams are random, so any of them can happen.

    It has to reason about totals, not about players facing each other. Pairing
    the entries off one-to-one -- largest against second largest -- is the
    intuitive model and understates the pot: 100 and 20 against 50 and 50 meet
    at 100 a side, because the two 50s add up, and a pairwise reading calls it
    140 instead of 200.

    Equal-size teams is the only constraint (team_creator refuses to fire an
    uneven roster), so the answer is the size-n/2 subset whose total comes
    closest to half the table without passing it: min(A, B) is then that subset,
    and the pot is twice it. Found by subset sum over counts, which is cheap at
    the sizes a draft comes in and exact, unlike a greedy pass.

    An ODD queue cannot fire at all, so there is no split to maximise over. The
    smallest entry stands aside at face value -- its own money is in, and the
    joiner who evens the roster up brings theirs when they arrive.

    Everything is floored to _STAKE_STEP first, because that is the unit the
    sides meet at: an entry of 25 backs 20 and hands back the 5.
    """
    units = sorted((n // _STAKE_STEP for n in stakes if n > 0), reverse=True)
    if not units:
        return 0

    # An odd roster cannot be split into two teams; hold the smallest back so
    # the rest divides, and add it on unmatched.
    odd = units.pop() if len(units) % 2 else 0
    half = len(units) // 2
    total = sum(units)

    # reachable[k] = every total a k-entry team could hold.
    reachable: list[set[int]] = [set() for _ in range(half + 1)]
    reachable[0].add(0)
    for u in units:
        for k in range(half, 0, -1):
            reachable[k] |= {s + u for s in reachable[k - 1]}

    # The best team total that is still the SMALLER of the two; its complement
    # is the other team, so this is min(A, B) and the pot is twice it.
    smaller = max((s for s in reachable[half] if 2 * s <= total), default=0)
    return (2 * smaller + odd) * _STAKE_STEP


def level_side(held: dict[str, int], budget: int) -> dict[str, int]:
    """How much of each entry stays at risk when a side must shrink to `budget`.

    Every entry fills to a common CEILING: a player holds min(their bet, the
    ceiling), and the ceiling is raised until the budget is spent. Whoever is
    over it carries the whole shortfall; whoever is under it is untouched.

    Scaling every bet by one ratio is the intuitive answer and the wrong one,
    for the same reason at two different sizes. It shaves the player who bet
    the draft minimum below that minimum to buy headroom for a bet ten times
    the size: 20 against 170 and 200 came out 12 / 96 / 112, and nobody held a
    figure they would recognise. Filling small bets whole and sharing the rest
    pro rata fixes that case and leaves the same squeeze one tier up, where 60
    and 400 against 100 grinds the 60 down to 10 to fund the 400. A ceiling
    answers both: those entries come out 20 / 100 / 100 and 50 / 50.

    It also makes the split monotone -- nobody who bet more can end up holding
    less, because min(bet, ceiling) is non-decreasing in the bet. Pro rata
    could not promise that once shares were rounded down to whole tens.

    Small bets are therefore filled whole as a CONSEQUENCE of the ceiling
    clearing them, not as a protected tier: when the other side cannot cover
    even the small bets, the ceiling drops below them and they are cut too.

    Everything is computed in units of ten and never exceeds a player's own
    entry, so a levelled stake is always a round number and always a number
    they agreed to. Whatever does not divide evenly is refunded by the caller.
    """
    caps = {p: n // _STAKE_STEP for p, n in held.items() if n >= _STAKE_STEP}
    remaining = budget // _STAKE_STEP
    alloc = {p: 0 for p in caps}

    while remaining > 0:
        # Everyone still under the ceiling. A player drops out once their own
        # bet is filled -- they cannot absorb any more, and the units they
        # would have taken raise the ceiling for whoever is left.
        rising = [p for p in caps if alloc[p] < caps[p]]
        if not rising:
            break

        share = remaining // len(rising)
        if share == 0:
            # Fewer whole tens left than players under the ceiling, so the
            # ceiling falls between two steps. Hand the odd tens to the
            # largest bets: they are the ones carrying the shortfall, and it
            # keeps the split monotone at the last step rather than letting a
            # smaller bet finish above a larger one.
            for player in sorted(rising, key=caps.__getitem__, reverse=True)[:remaining]:
                alloc[player] += 1
            break

        for player in rising:
            take = min(share, caps[player] - alloc[player])
            alloc[player] += take
            remaining -= take

    return {p: n * _STAKE_STEP for p, n in alloc.items() if n}


async def _declared_bets(session_id: str) -> tuple[dict[str, int], set[str]]:
    """Each player's declared bet, and which of them asked to be capped.

    A NULL is_capped reads as UNCAPPED, which is how the queue renders it: the
    toggle shows OFF for a row that never set the column, so the bot has to
    behave the way the button the player is looking at says it will.
    """
    from models.stake import StakeInfo
    from sqlalchemy import select

    async with db_session() as session:
        rows = (await session.execute(
            select(StakeInfo.player_id, StakeInfo.max_stake, StakeInfo.is_capped)
            .where(StakeInfo.session_id == session_id))).all()
    return ({player_id: bet for player_id, bet, _ in rows},
            {player_id for player_id, _, capped in rows if capped is True})


def _trim(held: dict[str, int], players: list[str],
          stays: dict[str, int]) -> dict[str, int]:
    """Plan the refund of whatever each of `players` holds above `stays`.

    Both ceilings a draft applies -- the player's own cap, and the one the two
    sides meet at -- are the same operation against a different target map, so
    they are the same code. It moves no money -- the refunds are booked together
    by _apply_refunds, so a failure part-way cannot leave a pool half-levelled --
    but it does update `held` in place (below).

    `players` is the scope rather than the keys of `stays`, because a player
    levelled all the way to zero is absent from `stays` and still has an entry
    to hand back.

    `held` is updated in place: the second ceiling has to be computed from what
    the first one left behind.
    """
    refunded: dict[str, int] = {}
    for player_id in players:
        excess = held[player_id] - stays.get(player_id, 0)
        if excess > 0:
            held[player_id] -= excess
            refunded[player_id] = excess
    return refunded


async def _apply_refunds(guild_id: str, session_id: str,
                         refunds: list[tuple[str, int, str]]) -> None:
    """Book match_pool's planned refunds all at once, or not at all.

    One commit per refund left a pool half-levelled at stage 'teams' whenever
    anything failed part-way, process death included.
    """
    if refunds:
        await _money_transaction(lambda session: _refund_all_in(
            session, guild_id, session_id, refunds))


def cap_targets(side: list[str], bets: dict[str, int], wants_cap: set[str],
                held: dict[str, int]) -> dict[str, int]:
    """What each player on `side` may keep once their own entry cap is applied.

    "Cap my entry so I never carry more than my share of my own team" is a
    personal ceiling a player opts into at signup, and it only ever trims:
    opting in cannot cost a player already inside their share.

    Measured against their TEAMMATES, not the opposing side, and that is the
    point of it. What players are protecting themselves from is being the one
    carrying a side -- and the history says the fear is well founded: a side
    whose top entry holds 55-60% of it wins 44.5%, and 65%+ wins 42.2%, against
    50% overall. Being merely the biggest entry is harmless (49.7%); being most
    of the side is not. A ceiling read from the opponents could not express that,
    because it says nothing about the team you are actually on.

    The share is of the whole side, so how MANY teammates you have changes the
    allowance -- 50 beside three teammates on 20 is 45% of its side and stands,
    where beside a single 20 it would be 71% and would not. A ceiling read from
    one teammate's figure cannot say that either.

    The ceiling comes from the DECLARED entries -- StakeInfo.max_stake -- never
    from what the side currently holds. match_pool is re-entrant, and a ceiling
    read from live holdings would ratchet down on every replay: levelling
    shrinks the teammates' holdings, so the next pass would compute a smaller
    allowance and trim again. The declared figure is what the player agreed to
    and levelling never writes to it, so every pass computes the same ceiling
    and the second finds nothing left to trim.
    """
    targets = {}
    for player in side:
        if player not in wants_cap:
            targets[player] = held[player]
            continue
        mates = sum(bets.get(p, 0) for p in side if p != player)
        ceiling = snap_to_step(int(mates * CAP_SHARE / (1 - CAP_SHARE)))
        if ceiling <= 0:
            # Nothing to cap against: a solo side, teammates with no declared
            # entry, or an allowance too small to reach one whole step. The test
            # is the CEILING and not the teammates' total, because a positive
            # total can still snap to nothing -- and a ceiling of zero would
            # refund this player's whole entry, the one thing the cap must never
            # do.
            targets[player] = held[player]
            continue
        targets[player] = min(held[player], ceiling)
    return targets


class MatchResult(TypedDict):
    matched: int                    # what each side ends up holding
    refunded: dict[str, int]        # returned because the other side could not cover it
    capped: dict[str, int]          # returned because the player asked to be capped
    held: dict[str, int]            # what each player is left playing for, after both ceilings


async def match_pool(guild_id: str, session_id: str,
                     team_a: list[str], team_b: list[str]) -> MatchResult:
    """Cap both sides at the smaller side's total, returning the excess.

    Two ceilings, in order: each player's own bet cap, then the one the two
    sides meet at. Capping first is what makes it a cap -- applied afterwards
    it would only ever duplicate what levelling had already done.

    Idempotent by construction rather than by a key: it refunds the difference
    between the sides, so once they are equal a second call finds nothing to
    refund. team_creator can be re-entered after a restart, and this has to be
    safe when it is.

    That covers a SEQUENTIAL replay, not a concurrent one. `held` is read once,
    before the refunds commit, so two overlapping calls would both compute
    their trim from the same snapshot and both book it -- taking a side down
    twice and leaving check_pool to raise on an imbalance already committed.
    What rules that out is upstream: every caller of create_and_display_teams
    (views.py's create-teams and start-draft buttons, ready_check's
    auto-create) tests and sets
    state_manager.is_creating_teams with no await in between, so the flag is a
    real mutex on this whole function. Adding a caller that skips it reopens
    the hole; a lock here would not, because the read is what goes stale.

    All or nothing: every refund is planned first and booked in one
    transaction (_apply_refunds), so a failure -- a refund refused or raising,
    or the process dying mid-way -- leaves the pool exactly as it was, and
    raises PoolNotSettled or the underlying error.
    """
    held = await contributions(guild_id, session_id)
    sides = ([p for p in team_a if held.get(p)], [p for p in team_b if held.get(p)])
    side_a, side_b = sides

    bets, wants_cap = await _declared_bets(session_id)
    capped: dict[str, int] = {}
    for side in (side_a, side_b):
        capped |= _trim(held, side, cap_targets(side, bets, wants_cap, held))

    # What each side can actually put up in whole tens. A stake is matched in
    # units of ten, so an entry of 25 backs 20 of the other side and hands back
    # the 5 -- and the figure the two sides meet at has to be one BOTH can
    # reach that way, not merely the smaller total.
    totals = [sum(snap_to_step(held[p]) for p in side)
              for side in sides]
    matched = min(totals)

    refunded: dict[str, int] = {}
    for side in sides:
        stays = level_side({p: held[p] for p in side}, matched)
        refunded |= _trim(held, side, stays)

    await _apply_refunds(guild_id, session_id,
                         [(p, n, "capped") for p, n in capped.items()]
                         + [(p, n, "unmatched") for p, n in refunded.items()])

    logger.info(f"draft pool {session_id}: matched at {matched} a side, "
                f"refunded {sum(capped.values())} over players' own caps and "
                f"{sum(refunded.values())} unmatched")
    await check_pool(guild_id, session_id)
    # The two reasons stay apart in the return value as well as in the ledger,
    # because team_creator's refund DM names each one's share and they are not
    # interchangeable: a cap is the player's own setting and levelling is not.
    # `held` rides along for the same caller -- _trim has already computed it in
    # place, so returning it saves that caller re-reading the ledger it was just
    # written from.
    return {"matched": matched, "refunded": refunded, "capped": capped,
            "held": dict(held)}


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

    async def pay(session: AsyncSession) -> dict[str, int]:
        # Every winner in ONE transaction. Paying them one at a time looks
        # harmless because each transfer is idempotent, but a failure between
        # two of them commits the first and leaves the holder half empty with
        # the sides no longer level -- and the next attempt runs check_pool,
        # sees the imbalance it caused, and refuses. An error in the middle of
        # paying a draft out would make that draft unsettleable forever.
        holder = pool_wallet_id(session_id)
        settled: dict[str, int] = {}
        for player_id, amount in shares.items():
            if amount <= 0:
                continue
            source = _payout_source(session_id, player_id)
            if await wallet_service.transfer_legs(session, source):
                # Someone else already paid this winner. Two match reports
                # can both read the pool before either takes MONEY_LOCK, so
                # both arrive here with a full set of shares; only the one
                # that actually books the transfer may claim it. Recording
                # it either way made `paid` mean "is square with the pool"
                # rather than "was paid by this call" -- harmless until a
                # caller started announcing payouts from it.
                continue
            await wallet_service.transfer_in(
                session, guild_id, holder, player_id, amount, source,
                notes=f"Draft winnings {session_id}")
            settled[player_id] = amount
        return settled

    paid = await _money_transaction(pay)

    logger.info(f"draft pool {session_id}: paid {sum(paid.values())} to {len(paid)} winners")
    await check_pool(guild_id, session_id)
    return {"paid": paid}


async def release_draft_pool(guild_id: str, session_id: str, reason: str, *,
                             delete_draft: bool = False) -> dict[str, dict[str, int]]:
    """Empty a draft's pool back to its contributors.

    One idempotent function for every path that ends a draft early. Idempotence
    -- not an event bus -- is what makes several callers safe, and it is why
    calling this on a draft that never had a pool is a no-op rather than an
    error: most drafts are not staked, and every teardown path calls it anyway.

    All or nothing, in one transaction: a refund that is refused or fails
    raises PoolNotSettled with the pool untouched.

    `delete_draft` deletes the draft's row in the same transaction, for a queue
    being torn down. Releasing and deleting separately leaves a window in which
    an entry -- from a stake selector opened minutes earlier -- is charged into
    a row that is about to go; together, under MONEY_LOCK, nothing can land
    between them, and entry_in refuses a draft with no row from then on. Releasing one
    refund at a time left a pool half-released -- and the inactive-queue reaper
    deletes the row right after, stranding whatever was still held.
    """
    async def release(session: AsyncSession) -> dict[str, int]:
        # Read inside the transaction, under the lock: an entry revised between
        # an outside read and the lock would have its stale amount refused.
        net = await wallet_service.contributions_to_in(
            session, guild_id, pool_wallet_id(session_id))
        held = {player_id: amount for player_id, amount in net.items() if amount > 0}
        await _refund_all_in(session, guild_id, session_id,
                             [(player_id, amount, reason) for player_id, amount in held.items()])
        if delete_draft:
            from sqlalchemy import select
            from models.draft_session import DraftSession
            row = (await session.execute(select(DraftSession).where(
                DraftSession.session_id == session_id))).scalar_one_or_none()
            if row is not None:
                await session.delete(row)    # the ORM delete, as callers did before
        return held

    try:
        refunded = await _money_transaction(release)
    except PoolNotSettled:
        raise
    except Exception as e:
        # One thing for every teardown path to catch, whatever failed: the
        # transaction rolled back, so the pool is untouched either way.
        raise PoolNotSettled(
            f"draft pool {session_id}: could not release ({reason}): {e}") from e
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
