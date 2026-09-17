"""Putting cards INTO the card library, and recording what it then owes back.

A deposit is a loan in reverse. The depositor hands cards to the library's MTGO
account and the library owes them back, which is the same mirrored pair the debt
ledger already writes for a loan with the two roles swapped:

    /borrow    lender = the house, borrower = the player   -> net < 0, "you owe"
    /deposit   lender = the player, borrower = the house   -> net > 0, "owed to you"

So one signed view answers both questions and there is no second table. The rule
that keeps it honest is the one the lending side already follows: the claim moves
only when a trade reports done, and it records what the serve says actually
crossed rather than what was asked for -- a depositor whose binder is short sends
fewer cards than they meant to, and the ledger has to say so.

Printings are the serve's business. It records which ones it received and hands
those exact copies back on a withdraw, so nothing here names a catId.
"""
import asyncio
import time
from datetime import datetime
from typing import Any, Optional

from loguru import logger
from sqlalchemy import select

from database.db_session import AsyncSessionLocal
from models.mtgo_job import MtgoJob
from services import wallet_service
from services.card_lending_service import (
    _mtgo_handle, available_now, library_busy_reason, _DISPATCH_LOCK,
)
from services.mtgo_tradebot_client import (
    DEFAULT_WAIT_MINUTES, get_lending_client, jobs_from, max_cards_per_trade,
    too_large,
)

# Its OWN kind, not the wallet's 'deposit'. That one is a TIX deposit with no
# card name, and the wallet's resumer picks up every pending row it recognises
# -- a card deposit filed under it would be polled against the wrong serve, get
# a 404, and be recorded as a failure. That exact collision cost a live loan its
# deposit on the lending side before it was found.
# How long a command waits on its own trade before handing over to the
# watchdog. The serve's offer stands ~10 min; this is only about answering the
# person who ran the command.
DEFAULT_POLL_S = 90

JOB_KIND = "card-deposit"
WITHDRAW_KIND = "card-withdraw"


def chunk_cards(cards: "list[dict[str, Any]]",
                limit: int) -> "list[list[dict[str, Any]]]":
    """Split a card list into orders no bigger than one MTGO trade.

    The split happens HERE rather than at the serve, and that is the whole
    point. A serve-side split is one order across several jobs with nothing
    tying them back together, so a scan that sees some of the jobs concludes
    the rest failed -- which is why that path was taken out. These are separate
    orders, one trade and one job each, settled by the machinery that already
    settles a single deposit. Nothing has to be reassembled afterwards.

    A card whose own quantity exceeds a trade is split across chunks, because a
    stack of one name cannot go any other way, and refusing it would make a
    cube undepositable for a reason its owner cannot act on.
    """
    chunks: "list[list[dict[str, Any]]]" = []
    current: "list[dict[str, Any]]" = []
    room = limit
    for card in cards:
        left = int(card.get("qty") or 0)
        while left > 0:
            take = min(left, room)
            if take > 0:
                current.append({"name": card["name"], "qty": take})
                left -= take
                room -= take
            if room == 0:
                chunks.append(current)
                current = []
                room = limit
    if current:
        chunks.append(current)
    return chunks


async def start_deposit(guild_id: Any, owner_id: Any,
                        cards: "list[dict[str, Any]]") -> "tuple[str, Optional[str]]":
    """Ask the serve to take these cards into the library. Returns (status, detail).

    Nothing is booked here: the obligation appears only when the trade reports
    done, so a depositor who never accepts owes and is owed nothing.
    """
    if not cards:
        return ("nothing_to_deposit", None)

    total = sum(int(c.get("qty") or 0) for c in cards)
    if too_large(total):
        # Refused rather than split, on the same terms as a loan: the serve
        # would run several trades and settling those correctly is a materially
        # harder problem than settling one.
        return ("too_large", f"{total} cards, and MTGO moves "
                             f"{max_cards_per_trade()} in one trade")

    handle = await _mtgo_handle(owner_id)
    if not handle:
        return ("not_linked", None)

    client = get_lending_client()
    if not client.enabled:
        return ("unavailable", None)

    # One trade window at a time, shared with borrows and returns -- the serve
    # does not care which direction a trade runs in.
    async with _DISPATCH_LOCK:
        busy = await library_busy_reason()
        if busy:
            return ("busy", busy)
        resp = await client.deposit(handle, cards, wait_minutes=DEFAULT_WAIT_MINUTES)

    if resp and resp.get("_ambiguous"):
        # The request reached the serve and only the ANSWER was lost, so a real
        # trade may be open -- and if the depositor accepts it their cards are
        # inside the library with no job row and nothing that will ever look for
        # them. Find it: a deposit of this exact list, for this user, moments
        # ago, IS our trade. The lending side does the same, and here the cards
        # at risk are somebody's own rather than the house's.
        adopted = await client.find_recent_deck_job("deposit", handle, cards) or {}
        adopted_id = adopted.get("id")
        if not adopted_id:
            logger.error("deposit for {} may or may not have opened and no matching "
                         "job was found -- needs a look", owner_id)
            return ("dispatch_unknown", None)
        logger.warning("library adopted orphaned deposit job {} for {}",
                       adopted_id, owner_id)
        await _record_jobs(guild_id, owner_id, handle, [(str(adopted_id), total)])
        return ("dispatched", str(adopted_id))

    jobs = jobs_from(resp, total)
    if not jobs:
        if resp:
            logger.error("deposit for {} came back unbookable ({})", owner_id, list(resp))
            return ("dispatch_unknown", None)
        return ("dispatch_failed", None)

    await _record_jobs(guild_id, owner_id, handle, jobs)
    logger.info("library: deposit dispatched for {} as {}", handle, [j for j, _ in jobs])
    return ("dispatched", jobs[0][0])


async def start_withdrawal(guild_id: Any, owner_id: Any) -> "tuple[str, Optional[str]]":
    """Give a depositor back everything the library is holding for them.

    Everything, and NAMED BY NOTHING: the serve pins the exact printings it
    received from its own movement record, so a whole-position withdraw that
    listed cards could only disagree with what actually crossed.

    Cards a borrower is currently holding are the one thing that stops this.
    The library owes them and cannot hand them over, so the trade would open
    and fail on a short binder -- better to say which cards are out than to
    make somebody watch that happen in MTGO.
    """
    held = await held_for(guild_id, owner_id)
    if not held:
        return ("nothing_held", None)

    total = sum(int(c["qty"]) for c in held)
    if too_large(total):
        return ("too_large", f"{total} cards, and MTGO moves "
                             f"{max_cards_per_trade()} in one trade")

    stock = await available_now(guild_id)
    # An unlisted card reads as PRESENT, not missing: /vault truncates its
    # listing, so absence is not evidence of absence, and a deposit of any size
    # runs off the end of it. Refusing on unknown would turn the normal case
    # into "your cards are out on loan" when they are sitting right there. If
    # we are wrong the trade says so, which is the same answer a minute later.
    out = [f"{int(c['qty']) - stock.get(c['name'], int(c['qty']))}x {c['name']}"
           for c in held
           if stock.get(c["name"], int(c["qty"])) < int(c["qty"])]
    if out:
        return ("some_on_loan", ", ".join(sorted(out)))

    handle = await _mtgo_handle(owner_id)
    if not handle:
        return ("not_linked", None)

    client = get_lending_client()
    if not client.enabled:
        return ("unavailable", None)

    async with _DISPATCH_LOCK:
        busy = await library_busy_reason()
        if busy:
            return ("busy", busy)
        resp = await client.withdraw_cards(handle, wait_minutes=DEFAULT_WAIT_MINUTES)

    if resp and resp.get("_ambiguous"):
        logger.error("withdrawal for {} may or may not have opened -- needs a look", owner_id)
        return ("dispatch_unknown", None)

    jobs = jobs_from(resp, total)
    if not jobs:
        if resp:
            logger.error("withdrawal for {} came back unbookable ({})", owner_id, list(resp))
            return ("dispatch_unknown", None)
        return ("dispatch_failed", None)

    await _record_jobs(guild_id, owner_id, handle, jobs, kind=WITHDRAW_KIND)
    logger.info("library: withdrawal dispatched for {} as {}", handle, [j for j, _ in jobs])
    return ("dispatched", jobs[0][0])


async def _record_jobs(guild_id: Any, owner_id: Any, handle: str,
                       jobs: "list[tuple[str, int]]", kind: str = JOB_KIND) -> None:
    async with AsyncSessionLocal() as session:
        for job_id, n in jobs:
            if await session.get(MtgoJob, job_id) is None:
                session.add(MtgoJob(job_id=job_id, kind=kind, guild_id=str(guild_id),
                                    player_id=str(owner_id), mtgo_user=handle,
                                    amount=n, card_name=None, status="pending"))
        await session.commit()


async def settle_deposits(guild_id: Any = None) -> "dict[str, Any]":
    """Poll deposits in flight and book what actually arrived.

    Safe to run repeatedly: a job still running is left alone, and a resolved
    one is never resolved twice. The claim is keyed by the trade that carried
    it, so two settlers reading the same finished deposit book it once.
    """
    settled: "dict[str, Any]" = {}
    async with AsyncSessionLocal() as session:
        stmt = select(MtgoJob).where(MtgoJob.kind.in_((JOB_KIND, WITHDRAW_KIND)),
                                     MtgoJob.status == "pending")
        if guild_id is not None:
            stmt = stmt.where(MtgoJob.guild_id == str(guild_id))
        pending = list((await session.scalars(stmt)).all())

    client = get_lending_client()
    for job_row in pending:
        job = await client.get_job(job_row.job_id, mark_missing=True)
        state = (job or {}).get("state")
        if state not in ("done", "failed"):
            if (job or {}).get("_missing"):
                # The serve has no such job: it restarted and lost its list, so
                # this can never report. Nothing was booked, so nothing unwinds.
                state = "failed"
            else:
                continue
        if state == "done":
            # Off the serve's own record of the trade, and from the side the
            # BOT was on: it receives a deposit and gives a withdrawal back.
            side = "receive" if job_row.kind == JOB_KIND else "give"
            moved = [{"name": i.get("name"), "qty": int(i.get("qty") or 0)}
                     for i in ((job or {}).get(side) or []) if i.get("name")]
            await _book_movement(job_row.guild_id, job_row.player_id, moved,
                                 job_row.job_id, job_row.kind)
        await _resolve(job_row.job_id, state)
        settled[job_row.job_id] = {"state": state, "detail": (job or {}).get("detail")}
    return settled


async def poll_until_settled(guild_id: Any, job_id: str, timeout_s: float = DEFAULT_POLL_S,
                             interval_s: float = 5) -> "dict[str, Any]":
    """Watch one trade until it lands, or give up waiting.

    The offer stands about ten minutes for a human to accept, so asking once is
    asking too early -- the job is queued and the command falls silent, having
    just told somebody to go and accept a trade. This exists so the person who
    ran the command gets an answer while they are still looking at Discord.

    Giving up is NOT failing: the job keeps its row and the watchdog settles it
    later. Returns {"state": done|failed|running, "detail": ...}.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        settled = await settle_deposits(guild_id)
        if job_id in settled:
            return settled[job_id]
        if time.monotonic() >= deadline:
            return {"state": "running", "detail": None}
        await asyncio.sleep(interval_s)


async def _book_movement(guild_id: Any, owner_id: Any, items: "list[dict[str, Any]]",
                         job_id: str, kind: str) -> None:
    """Move the claim for one finished trade, card by card.

    A deposit creates the obligation and a withdrawal retires it -- the same
    mirrored pair a loan writes, with the roles swapped both times. Keyed by
    the trade, so several settlers reading one finished trade book it once.
    """
    from services.card_lending_service import _already_booked, _CLAIM_LOCK
    from services import debt_service
    async with _CLAIM_LOCK:
        for item in items:
            source_id = f"mtgojob:{job_id}:{item['name']}"
            if await _already_booked(guild_id, source_id):
                continue
            match kind:
                case "card-deposit":
                    # The depositor is owed the copies back.
                    await debt_service.create_card_loan(
                        guild_id=str(guild_id), lender_id=str(owner_id),
                        borrower_id=wallet_service.HOUSE_LIBRARY, card_name=item["name"],
                        quantity=item["qty"], created_by="card-library",
                        source_id=source_id)
                case "card-withdraw":
                    # ...and the library has now given them back.
                    await debt_service.create_card_return(
                        guild_id=str(guild_id), returner_id=wallet_service.HOUSE_LIBRARY,
                        owner_id=str(owner_id), card_name=item["name"],
                        quantity=item["qty"], created_by="card-library",
                        source_id=source_id)
                case _:
                    raise ValueError(f"not a card-library job kind: {kind!r}")


async def _resolve(job_id: str, status: str) -> None:
    async with AsyncSessionLocal() as session:
        row = await session.get(MtgoJob, job_id)
        if row is not None and row.status == "pending":
            row.status, row.resolved_at = status, datetime.now()
            await session.commit()


async def held_for(guild_id: Any, owner_id: Any) -> "list[dict[str, Any]]":
    """What the library is holding for this depositor, from the claim ledger."""
    from services import debt_service
    rows = await debt_service.get_open_card_positions(
        str(guild_id), str(owner_id), wallet_service.HOUSE_LIBRARY)
    return [{"name": r["card_name"], "qty": r["net"]} for r in rows if r["net"] > 0]
