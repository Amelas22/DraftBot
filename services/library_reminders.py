"""Telling a borrower their deck is ready, and asking for it back.

A whitelisted library says nothing on the shared signup board -- the board is
one message for a room where most people cannot borrow (see
cube_views.pack_options.library_signup_note). So for those borrowers a DM is not
a convenience, it is the only signal that a deck exists at all.

Two moments, and they need different homes:

* READY is an event. assign_drafted_decks sends it when it creates the loan; a
  borrower waiting to start should not wait for a ten-minute poll. But the loan
  is COMMITTED before the DM is attempted, and every later assignment pass skips
  a borrower who already has one, so an undelivered DM has to be recoverable
  from the loan alone: ready_dm_at records a delivered announcement and the
  watchdog retries anything unstamped.
* RETURN is a condition, true from some point onward. The lending watchdog
  evaluates it, which already polls and already has the bot. Ten-minute
  granularity is fine for "please hand those back".

The decisions live here as plain functions over a loan so the interesting cases
-- mid-draft, already asked, a deck never collected -- are testable without a
draft or a Discord client.
"""
from datetime import datetime, timedelta
from typing import Any, NamedTuple, Optional

from loguru import logger

# When to ask. The first ask waits an hour after the borrower finished -- room
# to hand the deck back unprompted, or to play a dead rubber -- and then repeats
# hourly until it comes back: a sponsor's cards sitting in a finished drafter's
# account help nobody, and a daily ask let a deck sit out for most of a day.
# The lending watchdog evaluates this every ten minutes, so each ask lands up
# to ten minutes after it falls due.
REMIND_AFTER_FINISH = timedelta(hours=1)
REPEAT_AFTER = timedelta(hours=1)

# A draft with no sign of life for this long is treated as over. Without it an
# offer can be immortal: a draft that is partly reported and then fizzles never
# settles and never finishes its matches, so neither half of the rule in
# who_can_still_use ever fires. The cards stay reserved and -- least visibly and
# most damagingly -- the borrower's one active-loan slot stays occupied, so they
# are silently passed over at every later draft.
QUIET_AFTER = timedelta(hours=1)

# The one state with anything to give back. An 'assigned' loan is an offer
# nobody collected, which expire_stale_assignments retracts on its own, and the
# in-flight states mean a trade is already moving.
RETURNABLE_STATE = "borrowed"

# The one state a ready DM is owed in. A loan that has moved on needs no
# announcement: 'borrowed' means they collected it without needing the DM, and
# 'expired'/'returned' mean nobody needs those cards any more.
ANNOUNCEABLE_STATE = "assigned"

# Formats whose pairings all exist the moment teams form, so "no unreported
# match left" really does mean the player has finished. Swiss is deliberately
# absent: utils.calculate_pairings builds its rounds one at a time, so a Swiss
# player who has reported every match that EXISTS may still have rounds to come,
# and asking then would take the cards they need to play them.
FULLY_PAIRED_TYPES = ("random", "staked", "premade", "winston")

_DRAFT_SOURCE_PREFIX = "draft:"


def session_of(loan: Any) -> Optional[str]:
    """Which draft this deck went out for, or None if it went out for no draft.

    Loans are stamped 'draft:<session_id>' by draft_deck_assignment._source.
    Fixture loans carry another prefix and belong to no draft, so neither return
    trigger can ever apply to them.
    """
    source = getattr(loan, "source", None) or ""
    if not source.startswith(_DRAFT_SOURCE_PREFIX):
        return None
    return source[len(_DRAFT_SOURCE_PREFIX):] or None


def reminder_due(loan: Any, *, done_playing: bool, draft_settled: bool,
                 finished_at: Optional[datetime] = None,
                 now: Optional[datetime] = None) -> bool:
    """Should we ask for this deck back right now?

    Asked when the borrower has no more matches to play with it, or when the
    draft is settled -- whichever comes first -- once REMIND_AFTER_FINISH has
    passed since then. `finished_at` is that moment; None means it isn't known
    (a draft settled with no reported result, such as an abandoned one), and
    then the ask goes at once, as it always did.

    Settled, not fully played: a side clinching 5 of 9 decides the draft with
    four matches still unreported, and the cards should come back then. Whoever
    wants to play a dead rubber anyway can borrow again; holding a sponsor's
    cards against matches that no longer change anything is the wrong default.
    A borrower whose own draft is still live is never asked -- there the cards
    are how they play the rest of it.

    Then once per REPEAT_AFTER, which is the whole reason
    last_reminded_at exists: the watchdog re-evaluates every ten minutes and a
    condition that stays true would otherwise be a DM every ten minutes.
    """
    if loan.state != RETURNABLE_STATE:
        return False
    if session_of(loan) is None:
        return False
    if not (done_playing or draft_settled):
        return False
    at = now or datetime.now()
    if loan.last_reminded_at is None:
        return finished_at is None or at - finished_at >= REMIND_AFTER_FINISH
    # Repeats key on the last ask alone: a dead rubber reported after the first
    # ask must not push the next one back. Due ON the interval, not after it --
    # ">=" makes "due an hour after" mean exactly that at the boundary.
    return at - loan.last_reminded_at >= REPEAT_AFTER


def _deck_summary(cards: Any) -> str:
    """How many cards, not which ones: a 45-card list does not fit a DM and the
    borrower can see the deck itself with /library deck."""
    from debt_views.helpers import card_count_label

    try:
        total = sum(int(c.get("qty", 1)) for c in (cards or []))
    except (AttributeError, TypeError, ValueError):
        total = 0
    return card_count_label(total)


def deck_ready_message(cards: Any, collateral: int) -> str:
    """The DM that replaces what the shared board used to say.

    Names the deposit when there is one, because what it costs is what decides
    whether they can take it -- finding that out at /library borrow is the
    version that wastes their evening. Today's price is the right figure here,
    unlike in return_message: they have not paid yet.
    """
    msg = (f"📚 Your drafted deck is ready to borrow — **{_deck_summary(cards)}**.\n"
           "Run `/library borrow` to have it traded to you.")
    if collateral:
        msg += (f"\nThis library asks for a **{collateral} tix** deposit, "
                "refunded when you return the deck.")
    return msg


def return_message(loan: Any) -> str:
    """The ask. Says what it is for, because a borrower who has finished a draft
    has no other reason to think about the cards again.

    Names no figure. It used to quote the library's CURRENT price rather than
    what this borrower put up, so a library that changed its terms mid-loan
    promised the wrong refund in either direction. /library return states the
    real one at the moment it hands it back, where it cannot be stale.
    """
    return (f"📦 Please return your borrowed deck — **{_deck_summary(loan.cards)}**.\n"
            "Run `/library return` and the library will collect it, and any "
            "deposit you put up comes back with it.\n"
            "Other drafters are waiting on those cards.")


async def _dm(loan: Any, message: str, what: str) -> bool:
    """Send one library DM. True only if it actually went.

    send_dm RETURNS False for a blocked inbox, an HTTP error or an unparseable
    id -- it does not raise, by documented contract, and ignoring that return is
    how an undelivered DM gets recorded as sent. No try/except for the same
    reason: there is nothing for it to catch.
    """
    import notification_service
    from bot_registry import get_bot

    bot = get_bot()
    if bot is None:      # no bot (tests, migrations, CLI): nobody to tell
        return False
    return bool(await notification_service.send_dm(
        bot, loan.borrower_id, message, label=f"{what} for {loan.borrower_id}"))


async def _stamp(loan_id: Any, field: str, at: datetime) -> None:
    """Record that a DM landed. Only ever called after one did."""
    from database.db_session import db_session
    from models.card_loan import CardLoan

    async with db_session() as session:
        row = await session.get(CardLoan, loan_id)
        if row is not None:
            setattr(row, field, at)
            await session.commit()


async def announce_ready_decks(now: Optional[datetime] = None) -> int:
    """Tell every borrower who is owed a ready DM and has not had one.

    The retry that closes the gap between committing a loan and delivering its
    announcement -- see the module docstring. Three things end the obligation,
    and all three are expressed by this query rather than by flags: they were
    successfully told (ready_dm_at), they already hold the cards, or the offer
    is gone.
    """
    from sqlalchemy import select

    from database.db_session import db_session
    from models.card_loan import CardLoan

    at = now or datetime.now()
    async with db_session() as session:
        owed = list((await session.scalars(
            select(CardLoan.id).where(
                CardLoan.state == ANNOUNCEABLE_STATE,
                CardLoan.ready_dm_at.is_(None)))).all())

    told = 0
    for loan_id in owed:
        if await announce_one(loan_id, at=at):
            told += 1
    if told:
        logger.info("library reminders: announced {} ready deck(s)", told)
    return told


async def announce_one(loan_id: Any, at: Optional[datetime] = None) -> bool:
    """Tell one borrower their deck is waiting, recording it only if it landed.

    Shared by the assignment path and the retry sweep, so what counts as having
    been told exists once. Takes an id rather than a row because the row has to
    be re-read at dispatch anyway -- the same convention as set_collateral and
    trim_to_available.
    """
    from database.db_session import db_session
    from models.card_loan import CardLoan
    from services.card_lending_service import collateral_for

    async with db_session() as session:
        loan = await session.get(CardLoan, loan_id)
    if loan is None or loan.state != ANNOUNCEABLE_STATE or loan.ready_dm_at:
        return False

    collateral = 0
    try:
        collateral = await collateral_for(loan, loan.guild_id) or 0
    except Exception:
        logger.opt(exception=True).warning(
            "library reminders: could not read the terms behind loan {}; "
            "announcing without naming a deposit", loan_id)

    if not await _dm(loan, deck_ready_message(loan.cards, collateral), "deck ready"):
        return False
    await _stamp(loan_id, "ready_dm_at", at or datetime.now())
    return True


async def send_due_reminders(now: Optional[datetime] = None) -> int:
    """Ask for every deck that is due back. Returns how many were asked.

    Called from the lending watchdog, so it must never raise: a DM problem for
    one borrower cannot stop the tick that also settles trades.
    """
    from sqlalchemy import or_, select

    from database.db_session import db_session
    from models.card_loan import CardLoan

    at = now or datetime.now()
    async with db_session() as session:
        # The cooldown is ruled out in SQL, before any per-draft verdict work:
        # most ticks find nothing, and the ones that do should not first ask
        # "is this draft settled" for every deck asked about an hour ago.
        # reminder_due still decides; this only avoids work it would reject.
        loans = list((await session.scalars(
            select(CardLoan).where(
                CardLoan.state == RETURNABLE_STATE,
                or_(CardLoan.last_reminded_at.is_(None),
                    CardLoan.last_reminded_at <= at - REPEAT_AFTER)))).all())
    if not loans:
        return 0

    # Reads per DRAFT, not per loan: a pod shares its draft, so eight borrowers
    # would otherwise ask the same questions eight times. The finished-players
    # map is cached rather than a per-borrower answer, so one entry serves every
    # borrower in that pod.
    verdicts: "dict[str, DraftState]" = {}
    due: "list[Any]" = []
    for loan in loans:
        session_id = session_of(loan)
        if session_id is None:
            continue
        if session_id not in verdicts:
            verdicts[session_id] = await draft_state(session_id)
        state = verdicts[session_id]
        mine = state.done_at.get(str(loan.borrower_id))
        # Finished when their own last match was reported, else when the draft
        # was decided. Their own always wins when known: it can be no later than
        # the draft's latest result. Neither known -> None, and the ask goes now.
        finished_at = mine or (state.last_result if state.settled else None)
        if reminder_due(loan, done_playing=mine is not None, draft_settled=state.settled,
                        finished_at=finished_at, now=at):
            due.append(loan)

    asked = 0
    for loan in due:
        # Re-read immediately before sending. The list was decided up front and
        # every DM is a network round trip, so a borrower can hand their deck
        # back while an earlier one is still in flight -- and asking them then
        # reads as the library having lost track of its own shelf.
        if not await _still_out(loan.id):
            continue
        if await _dm(loan, return_message(loan), "return reminder"):
            await _stamp(loan.id, "last_reminded_at", at)
            asked += 1
    if asked:
        logger.info("library reminders: asked {} borrower(s) to return a deck", asked)
    return asked


async def _still_out(loan_id: Any) -> bool:
    """Is this deck still in the borrower's hands, right now?"""
    from database.db_session import db_session
    from models.card_loan import CardLoan

    async with db_session() as session:
        row = await session.get(CardLoan, loan_id)
    return row is not None and row.state == RETURNABLE_STATE


class DraftState(NamedTuple):
    """What the library needs to know about one draft, read once."""
    done_at: "dict[str, datetime]"      # finished players -> when they reported their last match
    settled: bool                       # see draft_state
    last_result: Optional[datetime]     # latest reported result; None if nothing was reported
    # Newest sign of life: the latest result, else when teams were made or the
    # draft started. None when there is nothing to measure from. See gone_quiet.
    last_activity: Optional[datetime] = None

    def gone_quiet(self, now: Optional[datetime] = None) -> bool:
        """Has this draft shown no sign of life for QUIET_AFTER?

        Measured from the last thing that happened, not from the draft's start,
        so a long evening that is still reporting results is not stale. A draft
        with nothing to measure from is left alone.
        """
        if self.last_activity is None:
            return False
        return (now or datetime.now()) - self.last_activity > QUIET_AFTER


def who_can_still_use(state: DraftState, player_ids: Any,
                      now: Optional[datetime] = None) -> "set[str]":
    """Which of these players still have a match that can change this draft.

    The library's one rule about time: the moment this stops including somebody
    is the moment reminder_due starts asking them for the deck BACK (done_at and
    settled are exactly what send_due_reminders reads). Handing one out, taking
    an uncollected offer back and chasing a collected one are the same question
    asked from three sides, so they read the same DraftState.

    Asked per PLAYER, never per pod. One drafter reporting says nothing about
    whether another still needs their cards, and refusing a whole pod on the
    first result strands everyone who had not collected yet.

    Empty when the draft is decided: a side clinching settles it with dead
    rubbers still unreported, and cards should not go out for those. Empty too
    when it has simply gone quiet -- see DraftState.gone_quiet, which is what
    stops a fizzled draft pinning a deck and a loan slot for good.

    Note the per-player half is vacuous for a progressively-paired format, where
    done_at cannot answer from the rows that exist yet: for those only
    settlement and silence end it.
    """
    if state.settled or state.gone_quiet(now):
        return set()
    return {str(p) for p in player_ids if str(p) not in state.done_at}


async def who_can_still_use_a_deck(session_id: Any, player_ids: Any,
                                   now: Optional[datetime] = None) -> "set[str]":
    """who_can_still_use, reading the draft first."""
    return who_can_still_use(await draft_state(session_id), player_ids, now)


async def draft_state(session_id: Any) -> DraftState:
    """One read of a draft and its results, for every loan out against it.

    done_at -- who has no unreported match left, and when they reported their
    last one: the moment they were done with a borrowed deck. Empty for a
    progressively-paired format, where the question cannot be answered from the
    rows that exist yet -- see FULLY_PAIRED_TYPES. Keys on result_submitted_at
    rather than winner_id: a DRAW is reported and has no winner, so keying on
    the winner would leave that player permanently unfinished.
    MatchResult.find_unreported_for_user makes the other choice, which is why
    this does not reuse it.

    settled -- is this draft decided? Named for what it means, not "over":
    who_can_still_use asks the narrower question the library acts on (can THIS
    player still use a deck), and this is one of its halves. Defers to helpers.stale_drafts.is_finished_draft,
    which reads the victory message as well as the stage -- the stage alone is
    wrong for most finished drafts, which never advance past 'pairings'. So a
    draft counts as settled the moment a side clinches, with dead rubbers
    possibly unreported, which is deliberate: see reminder_due. 'abandoned' is
    checked here because is_finished_draft does not treat a collapsed draft as
    finished, and for getting cards back it plainly is.

    last_result -- for a settled draft, the moment it was decided, give or take
    a dead rubber reported after the clinch (which only ever makes the first ask
    later, never early).

    last_activity -- the newest of last_result, teams_start_time and
    draft_start_time: what DraftState.gone_quiet measures silence from.
    """
    from sqlalchemy import select

    from database.db_session import db_session
    from helpers.stale_drafts import is_finished_draft
    from models.draft_session import DraftSession
    from models.match import MatchResult
    from services.card_library_inventory import FINISHED_STAGES

    async with db_session() as session:
        row = await session.scalar(
            select(DraftSession).where(DraftSession.session_id == str(session_id)))
        if row is None:
            return DraftState({}, False, None)
        results = (await session.execute(
            select(MatchResult.player1_id, MatchResult.player2_id,
                   MatchResult.result_submitted_at)
            .where(MatchResult.session_id == str(session_id)))).all()

    settled = row.session_stage in FINISHED_STAGES or is_finished_draft(row)
    last_result = max((t for _, _, t in results if t is not None), default=None)
    last_activity = max((t for t in (last_result, row.teams_start_time,
                                     row.draft_start_time) if t is not None),
                        default=None)
    if row.session_type not in FULLY_PAIRED_TYPES:
        return DraftState({}, settled, last_result, last_activity)

    last_played: "dict[str, datetime]" = {}
    outstanding: "set[str]" = set()
    for player1_id, player2_id, submitted_at in results:
        for player_id in (player1_id, player2_id):
            if player_id is None:
                continue
            pid = str(player_id)
            if submitted_at is None:
                outstanding.add(pid)
            elif pid not in last_played or submitted_at > last_played[pid]:
                last_played[pid] = submitted_at
    done_at = {pid: t for pid, t in last_played.items() if pid not in outstanding}
    return DraftState(done_at, settled, last_result, last_activity)
