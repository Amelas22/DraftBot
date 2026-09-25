"""Handing each drafter their pool when the draft ends.

This is the join between a draft and the card library. `assign_deck` has built
the "assigned" loan that `/borrow` collects since the library landed, but
nothing in the bot called it -- only the seeding script -- so a deck reached a
player's hands only by hand. This is what calls it.

It is deliberately NOT part of `post_team_logs`, which is the other thing that
happens to a finished draft's log. That function refuses to stamp itself done
whenever a team channel will not resolve, so the reconciler retries it: an
all-or-nothing rule about DISCORD CHANNELS. Whether a player was handed their
deck has nothing to do with whether a room existed, and hanging one off the
other would lose a loan to a missing channel.

The mapping is the part that has to be right. Sign-ups line up against
Draftmancer seats POSITIONALLY, and `map_discord_to_draftmancer` returns an
empty mapping rather than guess when the counts disagree. Getting it wrong here
does not mis-post a message: it hands somebody another player's forty-five
cards and takes collateral off them for it. So an empty mapping assigns nobody
anything -- not the subset that could be lined up.
"""
from typing import Any, Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import undefer

from services.library_service import library_for, offers
from database.db_session import AsyncSessionLocal
from helpers.mtgo_names import mtgo_name
from helpers.mtgo_untradeable import split_untradeable
from services.card_substitution_service import to_mtgo_names
from models.card_loan import ACTIVE_STATES, CardLoan
from models.draft_session import DraftSession
from services.card_lending_service import assign_deck
from services.draft_log_store import map_discord_to_draftmancer, pool_items
from services.mtgo_tradebot_client import max_cards_per_trade, too_large

# Sessions this process has already complained about, so a draft that can never
# assign anybody warns once instead of once a minute for three days.
_SAID: "set[str]" = set()

# Sessions whose seating the mapper refused. Held separately because this one
# also skips the WORK: the answer is derived from a captured log and a fixed
# sign-up list, so it cannot come out differently on a later tick.
_UNSEATABLE: "set[str]" = set()


def _source(session_id: Any) -> str:
    """What a loan from this draft is stamped with.

    Both the endDraft push and the reconciler's retry sweep call this, and the
    reconciler re-runs freely, so "have I already done this draft" has to be
    answerable from the ledger. Stamping the loan answers it without a column
    on the session and therefore without a migration.
    """
    return f"draft:{session_id}"


async def _already_assigned(session_id: Any) -> "set[str]":
    """Who this draft has already been assigned a deck, in any state -- a loan
    that has since been borrowed or returned still means we have done our job
    for them, and re-assigning would hand out the pool twice."""
    async with AsyncSessionLocal() as session:
        rows = await session.scalars(
            select(CardLoan.borrower_id).where(CardLoan.source == _source(session_id)))
        return {str(r) for r in rows.all()}


async def _holding_a_deck(borrower_ids: "list[str]") -> "set[str]":
    """Which of these already have an unfinished loan, in ANY server.

    A borrower may hold exactly one at a time -- the card_loans partial unique
    index says so -- so someone who has not returned last week's deck is passed
    over rather than the whole draft's assignment failing on their row.

    Takes no guild, because that index does not either: the library is a single
    MTGO account, so a deck out in one server is out everywhere. Asking per
    guild let a cross-server holder past this skip and into assign_deck, where
    the index refused them anyway -- they still got nothing, but arrived there
    down the collision path, which reports a concurrent assignment instead. The
    tally then counted them in neither the skips nor the failures, so nothing
    afterwards could say they had been passed over, or why.
    """
    if not borrower_ids:
        return set()
    async with AsyncSessionLocal() as session:
        rows = await session.scalars(
            select(CardLoan.borrower_id).where(
                CardLoan.borrower_id.in_([str(b) for b in borrower_ids]),
                CardLoan.state.in_(ACTIVE_STATES)))
        return {str(r) for r in rows.all()}


async def _cube_of(session_id: Any) -> "Optional[str]":
    """The cube this draft was played with, or None if it recorded none."""
    async with AsyncSessionLocal() as session:
        return await session.scalar(
            select(DraftSession.cube).where(
                DraftSession.session_id == str(session_id)))


async def _session_meta(session_id: Any) -> "Optional[tuple[Any, dict[str, Any]]]":
    """(guild_id, sign_ups) WITHOUT touching draft_data.

    Kept apart from the log on purpose. The reconciler re-runs this for every
    draft captured in the last 72 hours, once a minute, so loading a draft's
    whole carddata block to discover there is nothing to do would read tens of
    megabytes of JSON an hour to reach the same answer every time.
    """
    async with AsyncSessionLocal() as session:
        row = await session.scalar(
            select(DraftSession).where(DraftSession.session_id == str(session_id)))
    if row is None:
        logger.warning("deck assignment: no session row for {}", session_id)
        return None
    return (row.guild_id, dict(row.sign_ups or {}))


async def _draft_log(session_id: Any) -> "Optional[dict[str, Any]]":
    """The captured Draftmancer log, or None if it has not landed yet.

    The reconciler sweeps sessions whose log may not have arrived. Nothing to
    assign is not a failure; it calls again once the log is captured.
    """
    async with AsyncSessionLocal() as session:
        row = await session.scalar(
            select(DraftSession)
            .options(undefer(DraftSession.draft_data))
            .where(DraftSession.session_id == str(session_id)))
    if row is None or not row.draft_data:
        return None
    return dict(row.draft_data)


def _shared_names(sign_ups: "dict[str, Any]") -> "set[str]":
    """Display names held by more than one drafter in this session.

    The seating is requested from Draftmancer by USERNAME, and two players with
    the same one are resolved in the order they connected -- which need not be
    the order they signed up in. The mapping back is positional, so it cannot
    notice the swap: it returns a full, confident, wrong answer, and the two
    players are handed each other's decks and charged collateral for them.

    The bot already refuses to guess on duplicate names when it is only
    deciding who to PING ("a wrong ping is worse than no ping"). This is the
    same hazard with cards attached.
    """
    seen: "dict[str, int]" = {}
    for name in sign_ups.values():
        key = str(name)
        seen[key] = seen.get(key, 0) + 1
    return {name for name, n in seen.items() if n > 1}


def _say_once(key: str, message: str, *args: Any) -> None:
    """Warn the first time this process sees `key`, then drop to debug.

    A draft that can never assign anybody -- a seating the mapper will not
    trust, a borrower who never returns their deck -- stays selected by the
    reconciler for its full 72-hour window. At one tick a minute that is over
    four thousand identical warnings, which does not make the problem easier to
    find; it makes every OTHER problem harder to find.
    """
    if key in _SAID:
        logger.debug(message, *args)
        return
    _SAID.add(key)
    logger.warning(message, *args)


async def _pool_the_library_can_lend(
    cards: "list[dict[str, Any]]",
) -> "list[dict[str, Any]]":
    """A drafted pool in the vocabulary custody is booked in.

    Named rather than inlined so the rule has somewhere to be tested and
    somewhere to be found. Two rules beyond the face-splitting pool_items has
    already done: cards MTGO has never had come out, because an order naming
    one is refused whole; and the rest are renamed to whatever MTGO calls them,
    because the ledger holds them under the name that MOVED.

    What is dropped here is not reported to the drafter. Unlike a deposit --
    where the cube's owner can fix their list -- nobody can act on a pool: the
    draft has happened, and a card the library could never lend was never going
    to be part of the deck it offers.
    """
    kept, _ = split_untradeable(cards)
    return await to_mtgo_names(kept)


async def assign_drafted_decks(session_id: Any) -> int:
    """Give every drafter their own pool as a deck they may collect.

    Returns how many were assigned. Safe to call repeatedly, which it will be:
    the push path runs it the moment the log is captured and the reconciler
    runs it again, every minute, for anything that did not take.

    The order of the checks is chosen so that the work grows with what is left
    to DO rather than with how many drafts exist: the guild's config, then who
    already has their deck, then who cannot be given one -- and only if somebody
    is still owed a deck does it read the draft log at all.
    """
    meta = await _session_meta(session_id)
    if meta is None:
        return 0
    guild_id, sign_ups = meta

    # Offered by the LIBRARY this server draws on. A server admin can rewrite
    # their own configs/<guild>.json through the bot, so nothing they control
    # may decide this; the binding and the cube list are both set from a shell
    # tool. A server with no library, or a cube the library does not offer,
    # assigns nobody a deck.
    cube = await _cube_of(session_id)
    library = await library_for(guild_id)
    if cube is None or library is None or not await offers(library.id, cube):
        logger.debug("deck assignment: {} is not offered to {} here",
                     cube or "(no cube)", session_id)
        return 0

    done = await _already_assigned(session_id)
    candidates = [d for d in sign_ups if d not in done]
    if not candidates:
        # The steady state, and the one this is called in most often: every
        # drafter already has their deck. Two cheap queries and out.
        logger.debug("deck assignment: {} is already done ({} loans)",
                     session_id, len(done))
        return 0

    # A borrower may hold exactly one unfinished loan -- the card_loans partial
    # unique index says so -- so someone who has not returned last week's deck
    # is passed over rather than the whole draft failing on their row.
    busy = await _holding_a_deck(candidates)
    assignable = [d for d in candidates if d not in busy]
    if not assignable:
        _say_once(f"busy:{session_id}",
                  "deck assignment: nobody on {} can be given a deck -- {} of {} "
                  "drafters are still holding one. They are not queued: once this "
                  "draft ages out of the reconciler's window their pools are gone.",
                  session_id, len(busy), len(sign_ups))
        return 0

    if str(session_id) in _UNSEATABLE:
        # Already decided, and the decision cannot change: the seating is read
        # from this session's own sign-ups and log, neither of which moves once
        # the draft is captured. Returning here rather than at the check below
        # is what stops the reconciler re-reading the log, and re-emitting
        # map_discord_to_draftmancer's own warning, once a minute for 72 hours.
        return 0

    draft_data = await _draft_log(session_id)
    if draft_data is None:
        return 0

    duplicated = _shared_names(sign_ups)
    if duplicated:
        _UNSEATABLE.add(str(session_id))
        logger.warning(
            "deck assignment: {} has drafters sharing a display name ({}), so the "
            "seating cannot be tied to a Discord account -- assigning NOBODY a "
            "deck. Seats are requested by username, and two players with one name "
            "are resolved in the order they connected, which need not be the order "
            "they signed up in.", session_id, ", ".join(sorted(duplicated)))
        return 0

    seats = map_discord_to_draftmancer(draft_data, sign_ups)
    if not seats:
        _UNSEATABLE.add(str(session_id))
        logger.warning(
            "deck assignment: the seating for {} cannot be trusted ({} sign-ups), "
            "so NOBODY is being assigned a deck. Assigning the part that lined up "
            "would hand someone another player's cards.", session_id, len(sign_ups))
        return 0

    assigned = 0
    empty: "list[str]" = []
    failed: "list[str]" = []
    for discord_id in assignable:
        seat = seats.get(discord_id)
        if seat is None:
            continue
        # Named the way the SERVE names them, not the way Draftmancer does. It
        # matches exactly and refuses an order containing a name it does not
        # know, so one two-faced card takes the whole deck down with it.
        #
        # Three rules make a card list nameable to the serve, and a pool needs
        # all three. mtgo_name settles the two-faced ones; the library's own
        # vocabulary settles the other two -- cards MTGO has never had (no deck
        # can include one, because the order would be refused whole) and cards
        # MTGO calls something else. Custody is booked under the name that
        # MOVED, so a pool left in Draftmancer's vocabulary names a Universes
        # Beyond card the library cannot match: the entitlement reads as zero,
        # _cap quietly drops it, and the drafter loses a card the library is
        # holding for them.
        cards = await _pool_the_library_can_lend(
            pool_items(draft_data, seat, name_of=mtgo_name))
        if not cards:
            # An empty pool is not a deck, and a loan of nothing would still
            # occupy their one active-loan slot and block the next draft.
            empty.append(discord_id)
            continue
        total = sum(int(c["qty"]) for c in cards)
        if too_large(total):
            # A loan nobody can collect is worse than no loan: it takes the
            # borrower's one active slot and there is no way to give it back,
            # because /return only accepts a deck that was actually borrowed.
            failed.append(discord_id)
            logger.error("deck assignment: {}'s pool from {} is {} cards and MTGO "
                         "moves {} in one trade, so it would be unborrowable -- "
                         "not assigning it", discord_id, session_id, total,
                         max_cards_per_trade())
            continue
        try:
            await assign_deck(guild_id, discord_id, cards,
                              source=_source(session_id),
                              library_id=str(library.id))
        except IntegrityError:
            # The push path and a reconciler tick can both be inside this
            # function for one session; they share an event loop and interleave
            # at every await. The loser hits the one-active-loan index, and the
            # row it collided with is the deck being assigned right now.
            #
            # Counted as a failure even so. It is benign only on that reading,
            # and the skip above cannot rule out the other one: a loan taken
            # out between it and this line collides identically. Leaving the
            # branch uncounted made the tally stop adding up, which is how a
            # drafter who got nothing became invisible.
            failed.append(discord_id)
            logger.warning("deck assignment: {} collided with the one-active-loan "
                           "index on {} -- they hold a deck already", discord_id,
                           session_id)
        except Exception:
            # One player's row, not the draft. Everyone after them still gets a
            # deck, which is the same rule the skips above follow.
            failed.append(discord_id)
            logger.opt(exception=True).error(
                "deck assignment: could not assign {}'s pool from {}",
                discord_id, session_id)
        else:
            assigned += 1

    _report(session_id, sign_ups, assigned, done, busy, empty, failed)
    return assigned


def _report(session_id: Any, sign_ups: "dict[str, Any]", assigned: int,
            done: "set[str]", busy: "set[str]", empty: "list[str]", failed: "list[str]") -> None:
    """Say what happened, every time, including when the answer is "nothing".

    Logging only on success is how a whole draft assigns nobody in silence: the
    count guard swallows the one line that would have said so, and neither
    caller looks at the return value. Nobody -- player or operator -- gets a
    signal that anything was meant to happen.
    """
    # "nothing to lend" rather than "unreadable": it used to mean only that the
    # draft log had no pool for that seat, and it now also means the pool had
    # one but nothing in it was a card the library could lend -- every name
    # dropped as not-on-MTGO. Those are the same non-event to this feature and
    # a very different thing to go and look at, and an operator sent after a
    # parse failure that never happened reads the log and finds nothing wrong.
    tail = (f"{assigned} assigned, {len(done)} already had one, {len(busy)} "
            f"holding a deck, {len(empty)} with nothing to lend, "
            f"{len(failed)} failed, of {len(sign_ups)} drafters")
    if failed or empty:
        logger.warning("deck assignment for {}: {} -- nothing-to-lend={} failed={}",
                       session_id, tail, empty, failed)
    elif assigned:
        logger.info("deck assignment for {}: {}", session_id, tail)
    else:
        logger.debug("deck assignment for {}: {}", session_id, tail)
