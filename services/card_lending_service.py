"""Lending a deck out of the card library, and getting it back.

The library's cards live in a second MTGO account (Team01) driven by its own
TradeBot serve. That makes every borrow and return half of a distributed
operation, and this module owns the DraftBot half: the claim ledger that says
who has what out. The serve owns physical reality and the trades themselves.

The one rule the whole module is built around: **the claim moves only when a job
reaches a terminal state.** `start_borrow` asks the serve for a trade and parks
the loan in `out_pending`; nothing says the borrower holds the deck until the
job comes back `done`. Marking it optimistically would leave the ledger
asserting a handover MTGO may never have made -- and the failure is silent,
because a trade that nobody accepts just sits there.

The mirror of that rule is that `job_id` is persisted. A bot that dies between
asking and settling comes back, finds the loan in `out_pending`, and can poll
the job to a conclusion. Without it the loan would be stranded with no way to
tell whether the cards ever moved.
"""
import asyncio
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from loguru import logger
from sqlalchemy import and_, or_, select, update

from config import is_money_server
from database.db_session import AsyncSessionLocal, db_session
from services.card_substitution_service import learn_substitutions
from database.retry import with_db_retry
from models.card_loan import ACTIVE_STATES, CardLoan
from models.library import Library
from models.match import MatchResult
from models.draft_session import DraftSession
from services.card_library_inventory import library_available
from services.library_service import library_for, library_id_for
from services.library_access_service import may_borrow
from helpers.money_gate import linked_username, serve_busy_reason
from services import wallet_service
from models.mtgo_job import MtgoJob
from services.mtgo_tradebot_client import (
    DEFAULT_WAIT_MINUTES, get_lending_client, max_cards_per_trade,
    too_large,
)

# How long a command waits on its own trade before handing over to the watchdog.
# The serve's own offer stands ~10 min; this is only about answering the player.
DEFAULT_POLL_S = 90
RESCAN_INTERVAL_S = 10 * 60
_watchdog_running = False   # on_ready refires on reconnects; start one loop only

# Where a loan goes back to when its trade fails: nothing moved, so the loan
# returns to the state it was in before we asked.
_ROLLBACK = {"out_pending": "assigned", "return_pending": "borrowed"}
# ...and where it lands when the trade succeeds.
_SETTLED = {"out_pending": "borrowed", "return_pending": "returned"}


# The library's serve trades with one person at a time, so borrows are
# serialised here rather than left to pile up inside it. Two callers landing
# together would otherwise both be told to accept a trade, and only one window
# would open.
_DISPATCH_LOCK = asyncio.Lock()

# Claims are checked-then-written, and every settler in this process races the
# others to do it: each command in flight runs a guild-wide scan every 5s and
# the watchdog scans again. Without this they all read "not booked" before any
# of them writes. The same reason wallet_service serialises its ledger writes.
_CLAIM_LOCK = asyncio.Lock()
QUEUE_TIMEOUT_S = 5 * 60


async def library_busy_reason() -> Optional[str]:
    """Why the library cannot open a trade right now, or None if it is free."""
    return await serve_busy_reason(get_lending_client())


async def _when_free(dispatch: "Callable[[], Awaitable[tuple[str, Any]]]",
                     borrower_id: Any, poll_s: float,
                     timeout_s: float) -> "tuple[str, bool]":
    """Run one trade as soon as the library is free, holding the caller's place.

    Returns (status, waited). `waited` is True when the caller had to queue, so
    the command can tell them their turn has come rather than leaving them
    wondering whether the first message ever did anything.

    Borrows and returns queue TOGETHER, in the one lock, because the serve
    opens one trade window at a time and does not care which direction it runs
    in. A return that skipped the queue would have the serve holding a window
    for one player while another was being told to accept theirs.

    The collateral hold happens inside the dispatch, which is to say AFTER the
    wait -- nobody is charged for time spent in the queue.
    """
    # Started BEFORE the lock, not after: a deadline set inside it gives every
    # waiter its own fresh five minutes, so three people queued can outlast
    # Discord's 15-minute followup window -- and the player who was told "you're
    # next" then gets no "your turn", after a deposit has been taken and a real
    # trade opened that nobody told them to accept.
    deadline = time.monotonic() + timeout_s
    async with _DISPATCH_LOCK:
        waited = False
        while True:
            if time.monotonic() >= deadline:
                logger.info("library: {} gave up waiting for a free serve", borrower_id)
                return ("still_busy", True)
            busy = await library_busy_reason()
            if not busy:
                break
            waited = True
            if time.monotonic() >= deadline:
                logger.info("library: {} gave up waiting for a free serve", borrower_id)
                return ("still_busy", waited)
            await asyncio.sleep(poll_s)
        status, _ = await dispatch()
        return (status, waited)


async def borrow_when_free(guild_id: Any, borrower_id: Any, poll_s: float = 10,
                           timeout_s: float = QUEUE_TIMEOUT_S,
                           offering: "Optional[list[dict[str, Any]]]" = None
                           ) -> "tuple[str, bool]":
    """Collect this borrower's deck as soon as the library is free.

    `offering` is the agreed subset when the library could not cover the whole
    deck. It travels as an argument rather than on the loan so that giving up
    on the queue leaves nothing behind to be picked up by the next borrow.
    """
    return await _when_free(lambda: _dispatch(
        guild_id, borrower_id, from_state="assigned", to_state="out_pending",
        send=_send_lend, job_type="borrow", offering=offering),
                            borrower_id, poll_s, timeout_s)


async def return_when_free(guild_id: Any, borrower_id: Any, poll_s: float = 10,
                           timeout_s: float = QUEUE_TIMEOUT_S) -> "tuple[str, bool]":
    """Take this borrower's deck back as soon as the library is free."""
    return await _when_free(lambda: _dispatch(
        guild_id, borrower_id, from_state="borrowed", to_state="return_pending",
        send=_send_collect, job_type="return"),
                            borrower_id, poll_s, timeout_s)


def collateral_holder(guild_id: Any) -> str:
    """The synthetic wallet that owns collateral while decks are out."""
    return f"library:collateral:{guild_id}"


async def set_collateral(guild_id: Any, borrower_id: Any, loan_id: Any,
                         amount: int, expect_job: Optional[str] = None
                         ) -> "dict[str, Any]":
    """Make this borrower's holding in the library's collateral wallet equal
    `amount`. Returns {"ok": True} or {"ok": False, "deficit": n}.

    A TARGET, not a hold -- the same rule draft_pool_service.set_entry uses for
    a stake, and for the same reasons. Taking a deposit is set_collateral(5);
    giving it back is set_collateral(0); a retry after a failed handover is
    set_collateral(5) again. Each reads what is actually held and moves only the
    difference, so calling it twice cannot charge twice and calling it from a
    watchdog, a retry or a replayed command all converge on the same state
    rather than accumulating.

    The idempotency key counts MOVEMENTS with the holder rather than describing
    the state, which is the part that has to be got right here. A borrower whose
    handover failed is refunded and is back to holding nothing, so a key built
    from the balance would repeat the first attempt's key, be swallowed as a
    retry, take nothing -- and hand over the deck for free, against a holder
    nobody had funded. That is the ordinary path for this feature, not an edge
    case: an unaccepted MTGO trade times out and the player is told to run
    /borrow again.

    `expect_job` makes this conditional on the loan still being on that trade,
    checked INSIDE the transaction that moves the money. A settler decides to
    refund, then has to wait for the money lock; in that gap the loan can settle
    and the borrower can retry, and "give back whatever is held" would then hand
    back the NEW attempt's deposit while its trade is live -- leaving them with
    a deck and their tix. Checking beforehand does not help: the check and the
    money have to be the same transaction, or the window simply moves.

    Reads and writes in ONE transaction under MONEY_LOCK, so the delta computed
    here is still true when it is written. asyncio interleaves at every await
    and Discord dispatches each interaction as its own task, so two clicks from
    one borrower would otherwise both read "holds nothing" and both charge.
    """
    if amount < 0:
        raise ValueError("Collateral cannot be negative")
    # Stringified ONCE. wallet_tx.guild_id is a String column, so a raw int
    # written here and a str read back match only because SQLite coerces on
    # insert; on any other backend the deposit would be booked under a key the
    # next read cannot find, and the borrower would be charged again.
    guild = str(guild_id)
    holder = collateral_holder(guild)
    borrower = str(borrower_id)

    async def _do():
        async with db_session() as session:
            if expect_job is not None:
                loan = await session.get(CardLoan, loan_id)
                if loan is None or loan.job_id != expect_job:
                    logger.info("library: loan {} moved on ({} != {}), leaving its "
                                "deposit alone", loan_id, getattr(loan, "job_id", None),
                                expect_job)
                    # The settler discards this: its own state write re-checks
                    # job_id and skips too, so the outcome is already correct.
                    # Returned so a future caller can tell a stale refusal from
                    # a borrower who simply has no tix.
                    return {"ok": False, "stale": True, "deficit": 0}
            held = max(await wallet_service.net_between(
                session, guild, holder, borrower), 0)
            if amount == held:
                return {"ok": True, "deficit": 0}

            moves = await wallet_service.movements_in(
                session, guild, holder, borrower)
            # The direction is in the prefix, not just the figures, so
            # wallet_history can tell "I paid a deposit" from "I got it back"
            # without re-deriving it from the sign of a leg.
            going_back = amount < held
            prefix = "loan-back:" if going_back else "loan-hold:"
            source = f"{prefix}{loan_id}:{borrower}:{moves}:{held}-{amount}"
            if await wallet_service.transfer_legs(session, source):
                return {"ok": True, "deficit": 0}

            if going_back:
                await wallet_service.transfer_in(
                    session, guild, holder, borrower, held - amount, source,
                    notes=f"card library deposit returned (loan {loan_id})")
                logger.info("library: returned {} tix to {} for loan {}",
                            held - amount, borrower, loan_id)
                return {"ok": True, "deficit": 0}

            delta = amount - held
            balance = await wallet_service.balance_in(session, guild, borrower)
            if delta > balance:
                return {"ok": False, "deficit": delta - balance}
            await wallet_service.transfer_in(
                session, guild, borrower, holder, delta, source,
                notes=f"card library deposit (loan {loan_id})")
            logger.info("library: held {} tix from {} for loan {}",
                        delta, borrower, loan_id)
            return {"ok": True, "deficit": 0}

    async with wallet_service.MONEY_LOCK:
        return await with_db_retry(_do)


async def on_inflow(guild_id: Any, player_id: Any, *a: Any, **k: Any) -> Any:
    """Tell the debt system tix have landed in someone's wallet.

    Indirected through this module so it can be imported lazily -- the
    resolution service pulls in the whole wallet stack -- and so tests have one
    place to observe it.
    """
    from services.mtgo_resolution_service import on_inflow as _on_inflow
    return await _on_inflow(str(guild_id), str(player_id), *a, **k)


# Loan states whose cards the VAULT still lists but that are already promised.
#
# A dispatched trade has moved nothing yet -- the cards sit in the account until
# the borrower accepts -- so the vault counts them and something has to take
# them back off. `dispatch_unknown` is the same situation with less certainty:
# a trade may be open and may already have been accepted, and treating those
# cards as free is how a second borrower is offered a deck the first is
# holding.
_PROMISED_STATES = ("out_pending", "dispatch_unknown")


async def available_now() -> "dict[str, int]":
    """What the library can lend right now.

    The vault is what it physically holds, which already excludes anything a
    borrower has collected. What it does NOT exclude is a trade that has been
    offered and not yet accepted -- those cards are still on the shelf but are
    promised, and offering them again would have two people accepting trades
    for one stack.

    Promises count wherever they were made. One shelf serves every server, so
    counting only this server's offers would let two people in two servers each
    be offered a trade for the same stack.
    """
    client = get_lending_client()
    vault = await client.vault()
    stock = {i["name"]: i["qty"] for i in ((vault or {}).get("top") or [])}
    async with AsyncSessionLocal() as session:
        promised = (await session.scalars(
            select(CardLoan).where(
                CardLoan.state.in_(_PROMISED_STATES)))).all()
    for loan in promised:
        # What was actually offered: a partial deck promises only its own cards.
        for item in loan.offered_cards:
            if item["name"] in stock:
                stock[item["name"]] -= item["qty"]
    return {name: max(0, qty) for name, qty in stock.items()}


async def deposit_shortfall(guild_id: Any, borrower_id: Any
                            ) -> "Optional[dict[str, int]]":
    """What borrowing costs here, what the borrower holds, and the gap.

    None where there are no figures to quote -- no loan, or a cube with no
    price in this server. That is a refusal, not a price of nothing, and
    folding it to 0 rendered a deck that "needs a 0 tix deposit".

    Read for the MESSAGE, after the borrow has already been refused -- the
    refusal itself is decided inside set_collateral under the money lock, where
    the balance cannot move underneath it. Re-reading here can only be a little
    stale, and a figure a tix out in a "top up and try again" line costs
    nothing; holding the lock open to narrate is the version that costs.
    """
    loan = await active_loan(borrower_id)
    deposit = await collateral_for(loan, guild_id) if loan else None
    if deposit is None:
        return None
    async with db_session() as session:
        have = await wallet_service.balance_in(session, str(guild_id), str(borrower_id))
    return {"deposit": deposit, "have": have,
            "short": max(0, deposit - have)}


async def library_behind(loan: Any, guild_id: Any) -> "Optional[Any]":
    """The library a loan draws on: the one recorded on it, else this server's.

    Recorded on the loan from the moment it is assigned, so a deck stays priced
    by the library it actually came out of even if the server is later rebound
    somewhere else. The guild is the fallback only for a hand-made repair row
    written without a stamp -- every loan the bot creates has one, because the
    column is created with the table.

    The guild's binding is checked FIRST even though the answer is usually the
    loan's own stamp, and the lookup is not wasted: a server with no binding
    lends NOTHING. Reading the stamp first would let an unbound server go on
    pricing and handing over decks that were assigned before it was unbound,
    which is exactly what unbinding is for.
    """
    bound = await library_for(guild_id)
    if bound is None:
        return None
    named = getattr(loan, "library_id", None)
    if named:
        async with AsyncSessionLocal() as session:
            found = await session.get(Library, str(named))
            return found  # A deleted shelf must never fall back to another one.
    return bound


async def collateral_for(loan: Any, guild_id: Any) -> Optional[int]:
    """What this deck costs to borrow, or None if it cannot be borrowed.

    The price belongs to the LIBRARY the cards came from, not to the server. A
    server admin can rewrite `configs/<guild>.json` through the bot's own
    commands, so anything they can reach is a number they can lower -- and a
    library's terms are set by whoever runs it, from a shell tool.

    None means there is no library here, which is not the same as free: zero is
    a price somebody chose, and reading "nobody configured this" as free is how
    an unbound server would start giving cards away.
    """
    library = await library_behind(loan, guild_id)
    return None if library is None else int(library.collateral_tix or 0)


async def shortfall(guild_id: Any, cards: "list[dict[str, Any]]",
                    loan_id: Optional[int] = None) -> "list[dict[str, Any]]":
    """Which of these cards the library cannot fully cover, and by how much.

    Ledger absence means zero entitlement; only the vault is truncated.
    A collecting loan may use its own reservation, but not another loan's.
    """
    if loan_id is not None:
        async with AsyncSessionLocal() as session:
            loan = await session.get(CardLoan, loan_id)
        library = await library_behind(loan, guild_id)
        library_id = library.id if library else None
    else:
        library_id = await library_id_for(guild_id)
    avail = await lendable_now(library_id, exclude_loan_id=loan_id)
    return [{"name": c["name"], "want": c["qty"], "have": avail.get(c["name"], 0)}
            for c in cards
            if avail.get(c["name"], 0) < c["qty"]]


async def _park_for_review(loan: Any) -> None:
    """Take a loan out of reach after a dispatch we cannot account for.

    Leaving it "assigned" is what lets the borrower simply run /borrow again:
    the dispatcher accepts an assigned loan, set_collateral is a target rather
    than a charge so the retry costs nothing, and a second trade opens against
    a first that may be live. The user-facing message asks them not to, and a
    message is not a guard.
    """
    async with AsyncSessionLocal() as session:
        fresh = await session.get(CardLoan, loan.id)
        if fresh is not None and fresh.state == "assigned":
            fresh.state = "dispatch_unknown"
            await session.commit()
    loan.state = "dispatch_unknown"


async def lendable_now(library_id: Any, *, exclude_loan_id: Optional[int] = None
                       ) -> "dict[str, int]":
    """What THIS library can actually hand over right now: {name: copies}.

    The lesser of two answers, and it needs both.

    The vault is physical: it knows what is in the MTGO account and what has
    already been promised to somebody, and nothing else does. But several
    libraries share that account, so the vault cannot say whose a card is -- ask
    it alone and a communal borrow happily hands out a sponsor's Power, and the
    sponsor's own withdrawal fails later for cards that were never theirs to
    lose.

    The ledger is the claim: it knows exactly how many copies this library is
    owed by its donors, and how many of those are already spoken for. But it
    cannot see the shelf, so it does not know whether the copies are really
    there right now.

    Taking the minimum means a borrow is refused when EITHER is short, which is
    the only safe direction: lending on the strength of one while the other
    disagrees is how one library eats another's cards.
    """
    if not library_id:
        return {}
    physical = await available_now()
    entitled = await library_available(library_id, exclude_loan_id=exclude_loan_id)
    # The ledger is authoritative about ENTITLEMENT, so a card it has no rows
    # for is one this library holds none of -- it was deposited to somebody
    # else, or to nobody. Absent therefore means zero here, unlike the vault,
    # where absent means "not in the truncated listing" and has to be read as
    # present. Getting these two absences the same way round is what would let
    # one library lend another's cards.
    return {name: min(qty, physical.get(name, qty))
            for name, qty in entitled.items()}


def _cap(avail: "dict[str, int]", cards: "list[dict[str, Any]]"
         ) -> "list[dict[str, Any]]":
    """Trim a deck to what `avail` allows, dropping what it allows none of."""
    out = []
    remaining = dict(avail)
    for item in cards:
        qty = min(int(item["qty"]), int(remaining.get(item["name"], 0)))
        if qty > 0:
            out.append({"name": item["name"], "qty": qty})
            remaining[item["name"]] -= qty
    return out


async def trim_to_available(guild_id: Any, loan_id: Any) -> "list[dict[str, Any]]":
    """What of this deck the library can actually hand over right now.

    A pure read: it works out the subset and returns it, and NOTHING is written
    until the trade for it is accepted (see _dispatch). Staging the subset
    on the loan first looked harmless and was not -- it left a persisted offer
    that a queue timeout stranded, that a second click could rewrite while the
    trade for the first was in flight, and that every path ending without a
    trade had to remember to clear. The offer now exists only as an argument
    until it is a fact.
    """
    async with AsyncSessionLocal() as session:
        loan = await session.get(CardLoan, loan_id)
        if loan is None or loan.state != "assigned" or str(loan.guild_id) != str(guild_id):
            return []
        cards = list(loan.cards or [])
    library = await library_behind(loan, guild_id)
    avail = await lendable_now(library.id if library else None, exclude_loan_id=loan.id)
    trimmed = _cap(avail, cards)
    logger.info("library: loan {} can be covered as {}", loan_id, trimmed)
    return trimmed


async def _mtgo_handle(discord_user_id: Any) -> Optional[str]:
    """The borrower's MTGO username, or None if they have never linked one.

    Thin wrapper over the shared helper so the tests have one place to patch.
    Read at dispatch rather than stored on the loan: a player who re-links
    should not have an old handle baked into a loan they took out last week.
    """
    return await linked_username(discord_user_id)


async def assign_deck(guild_id: Any, borrower_id: Any, cards: "list[dict[str, Any]]",
                      source: Optional[str] = None,
                      library_id: "Optional[str]" = None) -> int:
    """Give a borrower a deck they may collect. Returns the new loan's id.

    The library is stamped on the loan at assignment, so the deck stays priced
    and stocked by the library it actually came out of even if the server is
    rebound afterwards. Defaults to whichever library serves this server.
    """
    if library_id is None:
        found = await library_for(guild_id)
        library_id = str(found.id) if found else None
    if library_id is None:
        raise ValueError("Cannot assign a deck in an unbound server")
    async with AsyncSessionLocal() as session:
        loan = CardLoan(guild_id=str(guild_id), borrower_id=str(borrower_id),
                        library_id=library_id,
                        cards=cards, state="assigned", source=source)
        session.add(loan)
        await session.commit()
        return loan.id


async def expire_stale_assignments() -> int:
    """Retract deck offers for drafts that are over. Returns how many.

    An assignment is a promise to a drafter who might still collect it. Once
    the draft is UNDERWAY -- its first match result recorded -- whoever was
    going to borrow has borrowed, and anybody still holding an uncollected
    offer played without it. A draft that reached 'completed' or was abandoned
    is over whether or not every result was entered, and waiting for one that
    is never coming would reserve those cards for good.

    Only 'assigned' is retracted. A collected deck really is out, and expiring
    it would abandon the claim on cards somebody is holding.

    Leaving these alive cost three things at once: the cards stayed reserved so
    nobody else could borrow them, the deck stayed collectable long after the
    draft it belonged to, and -- least visibly and most damagingly -- the
    drafter's one active-loan slot stayed occupied, so they were silently
    passed over at every later draft.

    A loan with no draft behind it is left alone: nothing here can tell whether
    a hand-made assignment is stale.
    """
    async with AsyncSessionLocal() as session:
        offers = list((await session.scalars(
            select(CardLoan).where(CardLoan.state == "assigned",
                                   CardLoan.source.like("draft:%")))).all())
        if not offers:
            return 0

        finished = await _drafts_that_are_over(
            session, {str(l.source).split(":", 1)[1] for l in offers})
        retract = [l for l in offers
                   if str(l.source).split(":", 1)[1] in finished]
        if not retract:
            return 0
        expired = await _retract(session, retract)

    if expired:
        logger.info("library: retracted {} uncollected deck offer(s) for drafts "
                    "that are over", expired)
    return expired


async def _drafts_that_are_over(session: Any, sessions: "set[str]") -> "set[str]":
    """Which of these drafts no longer owe anybody an uncollected deck.

    Either the draft reached a terminal stage, or its first result is in --
    at which point whoever was going to borrow has borrowed.
    """
    over = set((await session.scalars(
        select(DraftSession.session_id).where(
            DraftSession.session_id.in_(sessions),
            DraftSession.session_stage.in_(("completed", "abandoned"))))).all())
    started = set((await session.scalars(
        select(MatchResult.session_id).where(
            MatchResult.session_id.in_(sessions),
            MatchResult.result_submitted_at.isnot(None)))).all())
    return over | started


async def _retract(session: Any, loans: "list[Any]") -> int:
    """Mark these offers expired, but only the ones still merely on offer.

    Written as a guarded UPDATE rather than by assigning to the ORM objects.
    Those were loaded before the queries above, and a borrow can dispatch in
    that gap: assigning state would then write "expired" over a loan that had
    become out_pending, leaving the row finished with a live job_id on it. The
    cards leave, settlement stops looking, the deposit stays held, and the
    borrower's slot is handed back while they are holding a deck.

    The WHERE clause settles it in the database, so it holds against another
    process too -- which _DISPATCH_LOCK, being an asyncio lock, does not.
    """
    result = await session.execute(
        update(CardLoan)
        .where(CardLoan.id.in_([l.id for l in loans]), CardLoan.state == "assigned")
        .values(state="expired"))
    await session.commit()
    expired = int(result.rowcount or 0)
    for loan in loans:
        # One line each: a count alone cannot answer "why did my deck vanish?"
        logger.warning("library: retracted {}'s uncollected deck from {} "
                       "(loan {}, {} cards)", loan.borrower_id, loan.source,
                       loan.id, sum(int(c.get("qty") or 0) for c in (loan.cards or [])))
    if expired != len(loans):
        logger.info("library: {} of {} offers had been collected since they were "
                    "read, and were left alone", len(loans) - expired, len(loans))
    return expired


async def active_loan(borrower_id: Any) -> "Optional[CardLoan]":
    """The borrower's one unfinished loan, or None. The database guarantees
    there is at most one (see card_loans' partial unique index).

    Takes no guild. The library is a single MTGO account, so a borrower holding
    a deck is holding its only copies of those cards -- standing in another
    server does not entitle them to a second one. Scoping this by guild let
    server B fail to see the loan server A had written, tell the borrower they
    had nothing outstanding, and hand them the same cards again.
    """
    async with AsyncSessionLocal() as session:
        return await session.scalar(
            select(CardLoan).where(
                CardLoan.borrower_id == str(borrower_id),
                CardLoan.state.in_(ACTIVE_STATES)))


async def _dispatch(guild_id: Any, borrower_id: Any, *, from_state: str, to_state: str,
                    send: "Callable[[Any, str, Any], Awaitable[Any]]",
                    job_type: str,
                    offering: "Optional[list[dict[str, Any]]]" = None
                    ) -> "tuple[str, Optional[CardLoan]]":
    """Shared body of borrow and return: check, ask the serve, park the loan.

    `send(client, handle, cards)` performs the trade. The loan is only moved to
    `to_state` once the serve has accepted the job and given us its id.
    """
    loan = await active_loan(borrower_id)
    if loan is None:
        return ("no_loan", None)
    if loan.state != from_state:
        # Anything already in flight must not be dispatched again -- a second
        # trade would hand out a second copy of a deck the library has once.
        return (("already_in_flight" if loan.state.endswith("_pending")
                 else f"already_{loan.state}"), loan)

    if to_state == "out_pending" and str(loan.guild_id) != str(guild_id):
        # Collected where it was drafted, or not at all. active_loan is global
        # -- one MTGO account, so a deck out anywhere is out -- but everything
        # downstream of a borrow is not: settlement reads loan.guild_id, so a
        # deposit held against the command's server is released against the
        # loan's and nothing ever gives it back. The cog's invite check reads
        # the command's server too, which let an assigned deck from a curated
        # library be collected from an open one that prices the same cube.
        #
        # A RETURN is deliberately allowed from anywhere: it takes no money,
        # releases the deposit against the loan's own server, and refusing it
        # would strand the library's cards over a technicality.
        logger.warning("library: {} tried to collect loan {} from {}, but it was "
                       "drafted in {}", borrower_id, loan.id, guild_id, loan.guild_id)
        return ("wrong_server", loan)

    client = get_lending_client()
    if not client.enabled:
        return ("unavailable", loan)

    handle = await _mtgo_handle(borrower_id)
    if not handle:
        # No MTGO account means no counterparty; a job dispatched now could
        # never complete and would sit in the serve until someone cancelled it.
        return ("not_linked", loan)

    # Refused before anything moves, deposit included. The serve would SPLIT an
    # order this big across several trades run one at a time, and settling a
    # split correctly -- each trade booking its own cards, a scan that sees only
    # some of them, a deposit released against what is really still owed -- is a
    # materially harder problem than settling one. Getting it wrong costs a
    # borrower their deposit or their cards, so the line is drawn here.
    if too_large(sum(int(c.get("qty") or 0) for c in (offering or loan.cards))):
        logger.info("library: refusing a {}-card order for loan {} -- over the "
                    "{} the serve moves in one trade",
                    sum(int(c.get("qty") or 0) for c in (offering or loan.cards)),
                    loan.id, max_cards_per_trade())
        return ("too_large", loan)

    # The deposit is taken BEFORE the cards leave: a deposit that cannot be
    # taken costs nothing, where cards handed out against one that never landed
    # cannot be recalled. Only on the way out -- a return takes nothing.
    collateral = 0
    library_id = loan.library_id
    if to_state == "out_pending":
        library = await library_behind(loan, guild_id)
        if library is None:
            # The guild has no usable library config. The cog's gate should have
            # caught this, so reaching here means something changed underneath
            # us -- refuse rather than lend without the deposit it asked for.
            logger.warning("library: refusing to lend in {} -- this cube has no "
                           "price here", guild_id)
            return ("unavailable", loan)
        library_id = str(library.id)
        if not await may_borrow(library_id, borrower_id):
            return ("not_invited", loan)
        requested = loan.cards if offering is None else offering
        # Validate the subset again AFTER queueing, before charging or sending.
        # Repeated names must consume one shared quantity, not each reuse it.
        deck = {}
        for card in loan.cards:
            deck[card["name"]] = deck.get(card["name"], 0) + int(card["qty"])
        if not requested or _cap(deck, requested) != requested:
            return ("short_cards", loan)
        available = await lendable_now(library_id, exclude_loan_id=loan.id)
        if _cap(available, requested) != requested:
            return ("short_cards", loan)
        collateral = int(library.collateral_tix or 0)
        if collateral and not is_money_server(guild_id):
            # A library that charges needs the wallet, and finding that out at
            # transfer time -- after the borrower has been promised a deck --
            # is the bad version. A library that charges nothing needs no
            # wallet at all, which is why this is asked and not assumed.
            logger.warning("library: {} charges {} tix but {} has no wallet",
                           library_id, collateral, guild_id)
            return ("no_wallet", loan)
        if collateral:
            held = await set_collateral(guild_id, borrower_id, loan.id, collateral)
            if not held.get("ok"):
                return ("short_funds", loan)
    elif not library_id:
        # Returns must use the original shelf, even after a server is unbound.
        logger.error("library: loan {} has no library; repair its stamp before returning", loan.id)
        return ("unavailable", loan)

    offering = offering or loan.cards
    total = sum(int(c.get("qty") or 0) for c in offering)
    job = await send(client, handle, offering)

    # One trade, always: a deck above the serve's per-trade limit is refused
    # above, before anything is sent, so there is never a split answer to read
    # here. (The serve refuses an over-limit order rather than splitting it;
    # what needs several trades -- a whole cube -- is chunked by the caller
    # and arrives here one chunk at a time.)
    jobs: "list[tuple[str, int]]" = (
        [(job["id"], total)] if job and job.get("id") else [])
    job_id = jobs[0][0] if jobs else None

    if not jobs:
        if (job or {}).get("_ambiguous"):
            # The request reached the serve and only the ANSWER was lost, so a
            # real trade may already be open. Look for it: a job matching this
            # user and this exact deck, made moments ago, IS our trade, and
            # adopting it puts the loan back on the ordinary settling path.
            # The wallet path has done this for tix since it was written.
            # Excluding what this loan already has open. A scan matches on type,
            # handle and card list, and an earlier attempt for the same loan is
            # identical on all three -- so without this the recovery can adopt
            # the trade a previous dispatch opened and report its outcome as
            # this one's. The tix runner has passed exclude_ids since the order
            # chunking landed; the card path never did.
            adopted = await client.find_recent_deck_job(
                job_type, handle, offering,
                exclude_ids=[loan.job_id] if loan.job_id else ())
            if adopted and adopted.get("id"):
                jobs, job_id = [(adopted["id"], total)], adopted["id"]
            if not jobs:
                # Nothing to adopt, so the request may genuinely never have
                # landed. Refunding and inviting a retry is how a second deck
                # goes out against no deposit: the deposit stays, and a human
                # unpicks the rest.
                logger.error("Library trade for loan {} ({}) may or may not have been "
                             "accepted and no matching job was found -- deposit held, "
                             "needs a look", loan.id, handle)
                await _park_for_review(loan)
                return ("dispatch_unknown", loan)
            logger.warning("Library adopted orphaned {} job {} for loan {}",
                           job_type, job_id, loan.id)
        elif job:
            # The serve answered with something we could not book -- a split
            # whose parts do not sum to the order, or a shape we do not know.
            # Trades may be LIVE, so this is NOT a refusal: refunding here is
            # how cards go out against no deposit.
            logger.error("Library {} for loan {} came back unbookable ({}) -- deposit "
                         "held, needs a look", job_type, loan.id, list(job))
            await _park_for_review(loan)
            return ("dispatch_unknown", loan)

    if not jobs:
        logger.warning("Library trade was not accepted for loan {} ({})", loan.id, handle)
        # Unconditional, on the same terms as settlement: a refusal moved
        # nothing, so the deposit goes back now, and nothing else would ever
        # look at this loan again -- it has no job to settle. Gating on the
        # figure this attempt computed would strand a deposit taken when the
        # guild was charging more than it is now.
        if to_state == "out_pending":
            await set_collateral(loan.guild_id, borrower_id, loan.id, 0)
            await on_inflow(loan.guild_id, borrower_id)
        return ("dispatch_failed", loan)

    await _record_batches(loan.guild_id, borrower_id, handle, job_type, jobs,
                          library_id=library_id,
                          order_id=dispatch_order(loan.id, job_id))
    async with AsyncSessionLocal() as session:
        fresh = await session.get(CardLoan, loan.id)
        # What was SENT, committed with the trades that send it. Settlement
        # adopts this as the deck, so freezing it here is what stops a late
        # click changing the obligation a live trade is creating. job_id is the
        # first batch's id, which for a borrow is its only one; the batch rows
        # remain the record of the order.
        fresh.state, fresh.job_id, fresh.pending_cards = to_state, job_id, offering
        fresh.library_id = library_id
        await session.commit()
    logger.info("Library {} dispatched for {} as {} trade(s): {}",
                to_state, handle, len(jobs), [j for j, _ in jobs])
    return ("dispatched", loan)


def _send_lend(client: Any, handle: str, cards: Any) -> Any:
    return client.borrow(handle, cards, wait_minutes=DEFAULT_WAIT_MINUTES)


def _send_collect(client: Any, handle: str, cards: Any) -> Any:
    """Hand the whole loan back, WITHOUT naming the cards.

    The serve pins the exact printings it lent from its own movement record, so
    omitting the list settles everything open for this borrower -- which is what
    returning a deck means. Naming them instead re-states a list that can only
    disagree with what actually crossed: a borrow that half-landed would ask for
    cards they never received.
    """
    return client.return_cards(handle, wait_minutes=DEFAULT_WAIT_MINUTES)


async def start_borrow(guild_id: Any, borrower_id: Any,
                       offering: "Optional[list[dict[str, Any]]]" = None
                       ) -> "tuple[str, Optional[CardLoan]]":
    """Ask the serve to hand this borrower their deck, or the agreed subset."""
    async with _DISPATCH_LOCK:
        return await _dispatch(
            guild_id, borrower_id, from_state="assigned", to_state="out_pending",
            send=_send_lend, job_type="borrow", offering=offering)


async def start_return(guild_id: Any, borrower_id: Any) -> "tuple[str, Optional[CardLoan]]":
    """Ask the serve to take the deck back."""
    async with _DISPATCH_LOCK:
        status, loan = await _dispatch(
            guild_id, borrower_id, from_state="borrowed", to_state="return_pending",
            send=_send_collect, job_type="return")
    # "already_assigned" reads oddly for a return; the caller never took it out.
    return (("not_borrowed" if status == "already_assigned" else status), loan)


async def poll_until_settled(guild_id: Any, borrower_id: Any, expect: str,
                             timeout_s: float = DEFAULT_POLL_S,
                             interval_s: float = 5,
                             job_id: Optional[str] = None) -> "tuple[str, Optional[str]]":
    """Watch one borrower's in-flight trade until it lands, or give up waiting.

    `expect` is what success looks like for the trade that was dispatched --
    'borrowed' or 'returned'. It is required because the resting state does not
    say by itself what happened: a borrow that SUCCEEDED and a return that
    FAILED both come to rest at 'borrowed' with no job in flight, the first
    because the cards arrived and the second because they never came home.
    Reading the state alone told a player whose return failed that their deck
    had been collected.

    Giving up is not failing: the loan keeps its job_id and the watchdog
    resolves it later. This exists only so the player who ran the command gets
    an answer while they are still looking at Discord.

    `job_id` is the trade THIS command dispatched. Without it the poller
    watches a borrower rather than a trade, and a failed borrow the player has
    already retried would have the first command's poller report the second
    attempt's success as its own. A loan carrying some other job is still in
    flight as far as this caller is concerned.

    Returns (outcome, detail) where outcome is 'borrowed', 'returned', 'failed',
    or 'running' if the trade outlived us. detail is the serve's reason on a
    failure, which is what the player actually needs.
    """
    failed_state = _ROLLBACK["out_pending" if expect == "borrowed" else "return_pending"]
    deadline = time.monotonic() + timeout_s
    detail = None
    while True:
        just_settled = await settle_in_flight(guild_id)
        loan = await active_loan(borrower_id)
        if loan is None:
            # Gone from the active set: only a completed return does that.
            return ("returned", None) if expect == "returned" else ("running", None)
        if loan.id in just_settled:
            detail = just_settled[loan.id].get("detail") or detail
        if job_id is not None and loan.job_id not in (None, job_id):
            # Some other trade is in flight on this loan: ours is over and was
            # replaced. Whatever happens to that one is not our outcome.
            return ("running", None)
        if loan.job_id is None:
            if loan.state == expect:
                return (expect, None)
            if loan.state == failed_state:
                return ("failed", detail)
        if time.monotonic() >= deadline:
            return ("running", None)
        await asyncio.sleep(interval_s)


async def lending_jobs_watchdog(bot: Any = None, interval_s: float = RESCAN_INTERVAL_S) -> None:
    """Settle library trades whose command poller died -- timeout, restart, or a
    gateway reconnect. Covers both directions: decks going out on loan and cards
    coming in on deposit. Started once from bot.py's on_ready.

    on_ready refires on every reconnect, so the guard matters: without it each
    reconnect would add another loop, and they would poll the serve in chorus.
    """
    global _watchdog_running
    if _watchdog_running:
        return
    _watchdog_running = True
    logger.info("Card-library watchdog started (every {}s)", interval_s)
    while True:
        try:
            settled = await settle_in_flight()
            if settled:
                logger.info("Card-library watchdog settled {} loan(s)", len(settled))
        except Exception as e:
            logger.exception("Card-library watchdog (loans) failed: {}", e)
        try:
            # Its own scan: an offer for a finished draft holds cards and a
            # borrower's only loan slot, and nothing else will ever retract it.
            await expire_stale_assignments()
        except Exception as e:
            logger.exception("Card-library watchdog (stale offers) failed: {}", e)
        try:
            # Deposits run against the same serve and are settled here rather
            # than by a second loop: two watchdogs polling one serve would just
            # take turns waiting for each other. Its own try, because the two
            # scans share nothing but the serve -- a loan that raises used to
            # mean deposits went unsettled until the next round, or forever if
            # it raised every time.
            from services.card_deposit_service import settle_deposits
            deposited = await settle_deposits()
            if deposited:
                logger.info("Card-library watchdog settled {} deposit(s)", len(deposited))
        except Exception:
            logger.exception("Card-library deposit scan failed")
        await asyncio.sleep(interval_s)


async def _record_batches(guild_id: Any, borrower_id: Any, handle: str,
                          job_type: str, jobs: "list[tuple[str, int]]", *,
                          library_id: str, order_id: str) -> None:
    """One durable row per trade the serve is going to run for this order.

    One row per trade. A borrow sends exactly one -- a deck above the serve's
    per-trade limit is refused before anything moves, so the list that arrives
    here is always a single job. The shape is a list because settling reads the
    same rows the deposit side writes, and a cube IS chunked: there, recording
    every trade is what lets a depositor who accepts three invites of five be
    credited for the three.

    `card_name` is left NULL because a batch spans several cards -- what each
    trade actually carried is read back off the serve when it settles, which is
    the only account of it that cannot be wrong.
    """
    async with AsyncSessionLocal() as session:
        for job_id, n in jobs:
            row = await session.get(MtgoJob, job_id)
            if row is None:
                session.add(MtgoJob(job_id=job_id, kind=job_type, guild_id=str(guild_id),
                                    library_id=library_id, order_id=order_id,
                                    player_id=str(borrower_id), mtgo_user=handle,
                                    amount=n, card_name=None, status="pending"))
                continue
            # Already known: adoption hands back a job the serve had already
            # created, which may be one we recorded before the response was
            # lost. Waiting on it again is the point, so it goes back to
            # pending rather than colliding on the key.
            if (row.guild_id, row.player_id, row.library_id, row.order_id,
                    row.kind, row.status) != (
                    str(guild_id), str(borrower_id), library_id, order_id,
                    job_type, "pending"):
                raise ValueError(f"job {job_id} does not belong to this pending library trade")
        await session.commit()


def order_of(loan: Any) -> str:
    """The order key the trades of this loan's CURRENT dispatch are stamped with.

    Per dispatch, not per loan. A loan is dispatched more than once -- a borrow,
    then a return, and a retry after anything that left it assigned -- and a key
    that only named the loan would gather every attempt under one order.

    That is not theoretical. A crash between recording the batches and writing
    the loan's state leaves a pending row behind with the loan still assigned.
    Under a per-loan key the retry's settlement finds BOTH rows, books both, and
    the borrower owes two decks against one deposit; and a borrow's orphan is
    pulled into the later return, where `_settle_loan` reads the kind off the
    oldest row and settles the return as a borrow.

    The first batch's job id is what makes it unique, and the loan already
    stores it -- so the key is derivable at settling time from the row itself,
    with no column to keep in step.
    """
    return dispatch_order(loan.id, loan.job_id)


def dispatch_order(loan_id: Any, job_id: Any) -> str:
    """The order key for one dispatch, named before the loan records it."""
    return f"loan:{loan_id}:{job_id}"


async def _batches_for(loan: Any) -> "list[MtgoJob]":
    """The trades still in flight for one loan's order, oldest first.

    Asked by order, not by borrower. Looking them up by borrower assumed that
    every pending row a borrower has is part of their current order, which a
    crashed dispatch breaks: the orphan it leaves behind is adopted into the
    next order, and because the caller reads the KIND off the first row, a
    return then settles as a borrow. The real return is never booked, the
    orphan's cards are booked as a second loan, and the loan can never close.

    Rows carrying NO order are matched by borrower as well. They are the ones
    written before the stamp existed -- and, more to the point, a row that
    should have a stamp and has not got one is malformed, and must still be
    FOUND so that settling can refuse it. Missing it would be worse than the
    bug above: the loan's own job_id is read as a batch instead, which
    manufactures a well-formed stand-in for the very row that is wrong.

    The house's /cards lend writes kind='borrow' for the same guild and player
    against the OTHER serve, so the card_name filter stays: a library batch
    spans several cards and names none, and those rows always name one.
    """
    async with AsyncSessionLocal() as session:
        return list((await session.scalars(
            select(MtgoJob).where(
                or_(MtgoJob.order_id == order_of(loan),
                    and_(MtgoJob.order_id.is_(None),
                         MtgoJob.guild_id == str(loan.guild_id),
                         MtgoJob.player_id == str(loan.borrower_id))),
                MtgoJob.kind.in_(("borrow", "return")),
                MtgoJob.card_name.is_(None),
                MtgoJob.status == "pending")
            .order_by(MtgoJob.created_at))).all())


def _loan_as_batch(loan: Any) -> "list[MtgoJob]":
    """The loan's own job_id, dressed as the single batch it is.

    A loan dispatched before batch rows existed carries its trade on the row
    itself. Those loans are in flight across the upgrade, and a settler that
    only knew about batch rows would leave them stranded forever -- so the row
    is read as an order of one. Not persisted: it is a view of what is there.
    """
    match (loan.job_id, loan.state):
        case (None, _) | ("", _):
            return []
        case (_, "out_pending"):
            kind = "borrow"
        case (_, "return_pending"):
            kind = "return"
        case _:
            return []       # at rest: nothing of this loan is in flight
    return [MtgoJob(job_id=loan.job_id, kind=kind, guild_id=loan.guild_id,
                    library_id=loan.library_id, order_id=order_of(loan),
                    player_id=loan.borrower_id, mtgo_user="", amount=0,
                    card_name=None, status="pending")]


async def _resolve_batch(job_id: str, status: str) -> None:
    """Mark one trade finished. A no-op for a pre-upgrade loan, whose trade was
    never a row -- clearing the loan's job_id is what retires that one."""
    async with AsyncSessionLocal() as session:
        row = await session.get(MtgoJob, job_id)
        if row is not None and row.status == "pending":
            row.status, row.resolved_at = status, datetime.now()
            await session.commit()


async def _items_moved(job: "dict[str, Any]", kind: str) -> "list[dict[str, Any]]":
    """What a finished trade actually carried, off the serve's own record.

    Always the side the BOT was on. A borrow and a withdrawal are what it GAVE;
    a return and a deposit are what it RECEIVED. Read rather than assumed: a
    trade is the unit that succeeds or fails, so the only honest account of what
    changed hands is the list the serve booked.

    All four kinds live in one table on purpose. They pair off into opposite
    sides, so a second copy of this elsewhere is a second chance to get a pair
    backwards -- and a kind read off the wrong side reports an empty trade,
    which settles as "nothing crossed" and loses somebody's cards. An unknown
    kind is refused for the same reason: guessing a side is the failure.
    """
    # Which side of the trade the BOT was on, named as the serve names it.
    # Spelled out rather than built from the direction: the field is past tense
    # and one of the two irregular (gaveActual, not giveActual), so a derived
    # name reads plausibly and resolves to nothing.
    match kind:
        case "borrow" | "card-withdraw":
            side = "gaveActual"
        case "return" | "card-deposit":
            side = "receivedActual"
        case _:
            raise ValueError(f"not a library job kind: {kind!r}")

    # Outcome over intent. give/receive echo the order as ASKED; gaveActual /
    # receivedActual are what the trade carried, and the serve maintains them
    # separately because they differ: it substitutes a card whose MTGO printing
    # carries another name, and it can move fewer copies than were asked for.
    #
    # Booking the ask was wrong in both directions. Job 9a9f62d38f9c on
    # 2026-09-25 was asked for "Spectacular Spider-Man" and moved "Ademi of the
    # Silkchutes"; custody went on the shelf under a name MTGO has never had,
    # and /withdraw answered `409 asked for 1x ... but only 0 held` -- the cards
    # were unreachable by the only command that hands them back.
    #
    # Read the outcome, never the order. The serve always emits both arrays --
    # they are non-nullable fields projected unconditionally by every job
    # endpoint, /jobs and /jobs/{id} alike -- so there is no "the serve did not
    # say" case to fall back for, and a fallback here would only ever fire on a
    # response shape this code does not consume.
    #
    # Empty is a fact, not a gap: "empty until the trade closes; never a
    # substitute for `state`", as the serve puts it. What keeps that honest is
    # the state check at the settler, which books only a job the serve has
    # already called done.

    # Learned HERE because this is the one place a finished job is read -- both
    # the loan settler and the deposit settler come through it. Recording it at
    # the two callers instead would be two places to keep in step, and the one
    # that drifted would leave cards on the shelf that nothing can ask for.
    await learn_substitutions(job.get("substitutions"), job_id=job.get("id"))
    # Summed by name, not listed as they come. The claim for a trade is keyed
    # by job and card name, so two entries for one name would read as the same
    # movement and the second would be dropped as already booked -- quietly
    # losing whichever copies came in the second entry.
    totals: "dict[str, int]" = {}
    for i in (job.get(side) or []):
        name = i.get("name")
        if name:
            totals[name] = totals.get(name, 0) + int(i.get("qty") or 0)
    return [{"name": n, "qty": q} for n, q in totals.items()]


async def _already_booked(guild_id: Any, source_id: str) -> bool:
    """Has this trade's claim already been written? The ledger is append-only
    and has no unique index on source_id, so the check is the guard."""
    from models.debt_ledger import DebtLedger
    async with AsyncSessionLocal() as session:
        return bool(await session.scalar(
            select(DebtLedger.id).where(DebtLedger.guild_id == str(guild_id),
                                        DebtLedger.source_id == source_id).limit(1)))


async def _book_claim(guild_id: Any, borrower_id: Any, kind: str,
                      items: "list[dict[str, Any]]", job_id: str) -> None:
    """Move the claim for one finished batch, card by card.

    Booked PER BATCH, not per order, so a depositor who accepts three invites
    of five owes exactly the three and the two that timed out are owed by
    nobody. (A borrow is one trade -- see _dispatch's refusal above -- so for a
    loan the distinction is moot; it matters on the chunked deposit side, which
    settles through the same booking.) The claim is the mirrored DebtLedger
    pair the tix side has always used: `card_name` names the entity and
    `amount` is copies.

    Keyed by the TRADE that carried it, and skipped if that key is already
    booked. Several settlers read the same finished trade -- every command in
    flight runs a guild-wide scan every 5s, and the watchdog scans again -- and
    a claim keyed by a fresh uuid cannot collide, so nothing stops each of them
    booking it. The borrower then owes two or three times what they hold. This
    is what the tix side gets from keying its claims by job id.
    """
    from services import debt_service
    async with _CLAIM_LOCK:
        for item in items:
            source_id = f"mtgojob:{job_id}:{item['name']}"
            if await _already_booked(guild_id, source_id):
                logger.info("library: {} already booked for {}", source_id, borrower_id)
                continue
            match kind:
                case "borrow":
                    await debt_service.create_card_loan(
                        guild_id=str(guild_id), lender_id=wallet_service.HOUSE_MTGO,
                        borrower_id=str(borrower_id), card_name=item["name"],
                        quantity=item["qty"], created_by="card-library",
                        source_id=source_id)
                case "return":
                    await debt_service.create_card_return(
                        guild_id=str(guild_id), returner_id=str(borrower_id),
                        owner_id=wallet_service.HOUSE_MTGO, card_name=item["name"],
                        quantity=item["qty"], created_by="card-library",
                        source_id=source_id)
                case _:
                    raise ValueError(f"not a library job kind: {kind!r}")


async def settle_in_flight(guild_id: Any = None) -> "dict[Any, dict[str, Any]]":
    """Poll every library trade in flight and apply what it actually did.

    Each trade is polled and resolved on its own, and each that completes books
    its own cards. A borrow is a single trade today -- anything over the serve's
    per-trade limit is refused rather than split -- so in practice the loan
    comes to rest on that one. The loop is over batches anyway because it is the
    same settling path the chunked deposit side uses, and because a loan must
    not come to rest while any trade of it is still open.

    Safe to run repeatedly and on startup: a batch still running is left
    exactly as it was, and a resolved batch is never resolved twice.

    Returns {loan_id: {"state": .., "detail": ..}} for what it settled.
    """
    settled_detail: "dict[Any, dict[str, Any]]" = {}
    async with AsyncSessionLocal() as session:
        stmt = select(CardLoan).where(CardLoan.state.in_(tuple(_SETTLED)))
        if guild_id is not None:
            stmt = stmt.where(CardLoan.guild_id == str(guild_id))
        pending = (await session.scalars(stmt)).all()

    client = get_lending_client()
    for loan in pending:
        try:
            await _settle_loan(client, loan, settled_detail)
        except Exception:
            # One loan, not the scan. Booking a claim can now REFUSE -- a job
            # whose library stamp is missing or disagrees with its loan has
            # nowhere correct to put the cards -- and letting that escape would
            # leave every later loan unsettled on this pass and on every pass
            # after it. The loan stays in flight for a human instead.
            logger.opt(exception=True).error(
                "library: could not settle loan {} for {} -- left in flight",
                loan.id, loan.borrower_id)
    return settled_detail


async def _settle_loan(client: Any, loan: Any,
                       settled_detail: "dict[Any, dict[str, Any]]") -> None:
    """Bring one loan's whole order to rest, or leave it in flight."""
    batches = await _batches_for(loan) or _loan_as_batch(loan)
    if not batches:
        return
    kind = batches[0].kind
    still_open: bool = False
    landed: "list[dict[str, Any]]" = []
    detail: Optional[str] = None

    for batch in batches:
        job = await client.get_job(batch.job_id, mark_missing=True)
        state = (job or {}).get("state")
        if state not in ("done", "failed"):
            if (job or {}).get("_missing"):
                # The serve says outright it has no such job -- it restarted
                # and lost its list -- so this trade can never complete or
                # report. Leaving it "running" strands the loan for good.
                state = "failed"
                job = {"detail": "the library restarted and lost this trade"}
            else:
                still_open = True      # genuinely running, or unreadable
                continue
        if state == "done":
            # Book what THIS trade carried, now: the borrower has those
            # cards whatever happens to the rest of the order.
            moved = await _items_moved(job or {}, kind)
            if not batch.library_id or batch.library_id != loan.library_id:
                raise ValueError(f"Library stamp missing or mismatched for job {batch.job_id}")
            await _book_claim(wallet_service.library_scope(batch.library_id),
                              loan.borrower_id, kind, moved, batch.job_id)
            landed.extend(moved)
        else:
            detail = (job or {}).get("detail") or detail
            logger.warning("Library {} batch {} failed for loan {}: {}",
                           kind, batch.job_id, loan.id, (job or {}).get("detail"))
        await _resolve_batch(batch.job_id, state)

    if still_open:
        return          # the order is not finished; nothing comes to rest yet

    settled_detail[loan.id] = await _settle_order(loan, kind, landed, detail)


async def _settle_order(loan: Any, kind: str, landed: "list[dict[str, Any]]",
                        detail: Optional[str]) -> "dict[str, Any]":
    """Bring a loan to rest once no batch of its order is still open.

    The claim has already been booked per batch; this is the loan row and the
    deposit. `landed` is what actually crossed, which is what decides both --
    an order where nothing crossed is a failure however many trades it ran.
    """
    moved = bool(landed)
    guild, borrower = loan.guild_id, loan.borrower_id
    scope = wallet_service.library_scope(loan.library_id)
    remaining: "list[dict[str, Any]]" = (
        await _still_owed(scope, borrower) if kind == "return" and moved else [])

    # The money half, BEFORE the claim is written: a crash in between leaves
    # the loan exactly as it was, to be settled again, and set_collateral
    # converges rather than paying twice.
    match kind:
        case "borrow":
            gives_back = not moved      # nothing ever arrived
        case "return":
            gives_back = moved and not remaining
        case _:
            raise ValueError(f"not a library job kind: {kind!r}")
    if gives_back:
        await set_collateral(guild, borrower, loan.id, 0, expect_job=loan.job_id)
        await on_inflow(guild, borrower)

    async with AsyncSessionLocal() as session:
        fresh = await session.get(CardLoan, loan.id)
        if fresh is None:
            return {"state": "gone", "detail": detail}
        if (fresh.job_id, fresh.state) != (loan.job_id, loan.state):
            # Polling the serve took time and the loan moved on: its own poller
            # settled this order and the borrower dispatched the next one. An
            # outcome belongs to the trade it came from, so this is not ours.
            logger.info("library: loan {} moved on while settling; leaving it", loan.id)
            return {"state": "superseded", "detail": detail}
        match kind:
            case "borrow":
                fresh.state = "borrowed" if moved else "assigned"
                if moved:
                    fresh.borrowed_at = datetime.now()
            case "return":
                # Nothing came home means they still hold all of it. The claim
                # ledger is only consulted when something DID cross: a loan
                # booked before the ledger knew about it reads as "owes
                # nothing" there, and would be retired on a return that failed.
                settled = moved and not remaining
                fresh.state = "returned" if settled else "borrowed"
                if settled:
                    fresh.returned_at = datetime.now()
            case _:
                raise ValueError(f"not a library job kind: {kind!r}")
        fresh.job_id, fresh.pending_cards = None, None
        await session.commit()
    return {"state": "done" if moved else "failed", "detail": detail}


async def _still_owed(guild_id: Any, borrower_id: Any) -> "list[dict[str, Any]]":
    """Cards this borrower still owes the library, from the claim ledger."""
    from services import debt_service
    positions = await debt_service.get_open_card_positions(
        str(guild_id), str(borrower_id), wallet_service.HOUSE_MTGO)
    return [{"name": p["card_name"], "qty": -p["net"]}
            for p in positions if p["net"] < 0]
