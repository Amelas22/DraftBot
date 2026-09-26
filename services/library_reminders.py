"""Telling a borrower their deck is ready, and asking for it back.

A whitelisted library says nothing on the shared signup board -- the board is
one message for a room where most people cannot borrow (see
cube_views.pack_options.library_signup_note). So for those borrowers a DM is
not a convenience, it is the only signal that a deck exists at all.

Two moments, and they need different homes:

* READY is an event. It happens when assign_drafted_decks creates the loan, and
  a borrower waiting to start wants it then, not up to ten minutes later -- so
  it is sent from the assignment itself.
* RETURN is a condition, true from some point onward. It is evaluated by the
  lending watchdog, which already polls and already has the bot. Ten-minute
  granularity is fine for "please hand those back".

The decisions live here as plain functions over a loan so the interesting cases
-- mid-draft, already asked, a deck never collected -- are testable without a
draft or a Discord client.
"""
from datetime import datetime, timedelta
from typing import Any, Optional

from loguru import logger

# How long before a borrower still holding cards is asked again. Escalates
# rather than nags: the first ask can be missed or forgotten, and a sponsor's
# cards sitting in a finished drafter's account help nobody.
REPEAT_AFTER = timedelta(hours=24)

# The only state with anything to give back. An 'assigned' loan is an offer
# nobody collected, which expire_stale_assignments retracts on its own, and the
# in-flight states mean a trade is already moving.
RETURNABLE_STATE = "borrowed"

# The one state a ready DM is owed in. A loan that has moved on needs no
# announcement: 'borrowed' means they collected it without needing the DM, and
# 'expired'/'returned' mean nobody needs those cards any more.
ANNOUNCEABLE_STATE = "assigned"

_DRAFT_SOURCE_PREFIX = "draft:"


def session_of(loan: Any) -> Optional[str]:
    """Which draft this deck went out for, or None if it went out for no draft.

    Loans are stamped 'draft:<session_id>' by draft_deck_assignment._source.
    Fixture loans carry another prefix and belong to no draft, so neither
    return trigger can ever apply to them.
    """
    source = getattr(loan, "source", None) or ""
    if not source.startswith(_DRAFT_SOURCE_PREFIX):
        return None
    return source[len(_DRAFT_SOURCE_PREFIX):] or None


def reminder_due(loan: Any, *, done_playing: bool, draft_over: bool,
                 now: Optional[datetime] = None) -> bool:
    """Should we ask for this deck back right now?

    Asked when the borrower has no more matches to play with it, or when the
    draft is DECIDED -- whichever comes first.

    Decided, not fully played: a side clinching 5 of 9 settles the draft with
    four matches still unreported, and the cards should come back then. Whoever
    wants to play a dead rubber anyway can borrow again; holding a sponsor's
    cards against matches that no longer change anything is the wrong default.
    A borrower whose own draft is still live is never asked -- there the cards
    are how they play the rest of it.

    Then at most once per REPEAT_AFTER, which is the whole reason
    last_reminded_at exists. The watchdog re-evaluates every ten minutes and a
    condition that stays true would otherwise be a DM every ten minutes.
    """
    if loan.state != RETURNABLE_STATE:
        return False
    if session_of(loan) is None:
        return False
    if not (done_playing or draft_over):
        return False
    if loan.last_reminded_at is None:
        return True
    return (now or datetime.now()) - loan.last_reminded_at > REPEAT_AFTER


def _deck_summary(cards: Any) -> str:
    """How many cards, not which ones: a 45-card list does not fit a DM and the
    borrower can see the deck itself with /library deck."""
    try:
        total = sum(int(c.get("qty", 1)) for c in (cards or []))
    except (AttributeError, TypeError, ValueError):
        total = 0
    return f"{total} card{'' if total == 1 else 's'}"


def deck_ready_message(cards: Any, collateral: int = 0) -> str:
    """The DM that replaces what the shared board used to say.

    Names the deposit when there is one, because what it costs is what decides
    whether they can take it -- finding that out at /library borrow is the
    version that wastes their evening.
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

    Names no figure. It used to quote collateral_for, which reads the library's
    CURRENT price rather than what this borrower actually put up -- a library
    that changed its terms mid-loan would have promised the wrong refund, in
    either direction. What they escrowed lives in the collateral wallet
    (card_lending_service.collateral_holder), and quoting a number is not worth
    a read that has to be right; /library return states the refund at the moment
    it happens, when it cannot be stale.
    """
    return (f"📦 Please return your borrowed deck — **{_deck_summary(loan.cards)}**.\n"
            "Run `/library return` and the library will collect it, and any "
            "deposit you put up comes back with it.\n"
            "Other drafters are waiting on those cards.")


def _client() -> Any:
    """The bot, from the registry -- the same route notification_service and
    draft_setup_manager take. None outside a running bot (tests, migrations,
    the CLI), where there is nobody to DM and nothing to do.

    Fetched here rather than threaded in as a parameter: the callers of the
    assignment path have no business knowing that assigning a deck happens to
    send a DM, and notify_wallet already settled this question for the wallet.
    """
    from bot_registry import get_bot

    return get_bot()


async def _may_collect(library_id: Any, borrower_id: Any) -> bool:
    """Would /library borrow actually hand this over?

    Its own function so the tests can answer it without a library, and so the
    reason is in one place: a deck is assigned to every drafter with a pool, but
    on an invite-only library most of them would be refused -- and a DM is a
    worse place to make that promise than the shared board was, because it is
    addressed personally.
    """
    from services.library_access_service import may_borrow

    return await may_borrow(library_id, borrower_id)


async def announce_ready_decks(now: Optional[datetime] = None) -> int:
    """Tell every borrower who is owed a ready DM and has not had one.

    The retry that closes the gap between committing a loan and delivering its
    announcement. assign_drafted_decks sends the DM the moment it creates the
    loan -- a borrower waiting to start should not wait for a poll -- but the
    loan is committed first, and every later assignment pass skips a borrower
    who already has one. So a Discord blip, a restart, or a closed inbox that
    later opens used to mean they were never told at all.

    Three things end the obligation, and all three are expressed by the query
    plus _may_collect rather than by flags: they were successfully told
    (ready_dm_at), they already hold the cards, or the offer is gone.
    """
    from sqlalchemy import select

    from database.db_session import db_session
    from models.card_loan import CardLoan

    at = now or datetime.now()
    async with db_session() as session:
        owed = list((await session.scalars(
            select(CardLoan).where(
                CardLoan.state == ANNOUNCEABLE_STATE,
                CardLoan.ready_dm_at.is_(None)))).all())

    told = 0
    for loan in owed:
        if await announce_one(loan, at=at):
            told += 1
    if told:
        logger.info("library reminders: announced {} ready deck(s)", told)
    return told


async def announce_one(loan: Any, at: Optional[datetime] = None) -> bool:
    """Tell one borrower their deck is waiting, and record it only if it landed.

    Shared by the assignment path and the retry sweep so the rules about who
    gets told, and what counts as having been told, exist once.
    """
    from database.db_session import db_session
    from models.card_loan import CardLoan

    if not await _may_collect(loan.library_id, loan.borrower_id):
        logger.info("library reminders: {} is not on {}'s borrowing list, so "
                    "loan {} stays assigned but unannounced",
                    loan.borrower_id, loan.library_id, loan.id)
        return False

    collateral = 0
    try:
        from services.card_lending_service import collateral_for
        # Today's price is the right figure HERE, unlike in the return DM: they
        # have not paid yet, so what they would put up is what the library asks
        # now.
        collateral = await collateral_for(loan, loan.guild_id) or 0
    except Exception:
        logger.opt(exception=True).warning(
            "library reminders: could not read the terms behind loan {}; "
            "announcing without naming a deposit", loan.id)

    if not await notify_deck_ready(loan, collateral):
        return False

    async with db_session() as session:
        row = await session.get(CardLoan, loan.id)
        if row is not None:
            row.ready_dm_at = at or datetime.now()
            await session.commit()
    return True


async def notify_deck_ready(loan: Any, collateral: int = 0) -> bool:
    """DM one borrower that their deck is waiting. True if it went.

    Best-effort by contract: a borrower with closed DMs must not stop the rest
    of a draft's decks being assigned.
    """
    import notification_service

    bot = _client()
    if bot is None:
        return False
    try:
        # send_dm RETURNS False for a blocked inbox, an HTTP error or an
        # unparseable id -- it does not raise, by documented contract. Ignoring
        # that return is how an undelivered DM gets recorded as sent.
        return bool(await notification_service.send_dm(
            bot, loan.borrower_id, deck_ready_message(loan.cards, collateral),
            label=f"deck ready for {loan.borrower_id}"))
    except Exception:
        logger.opt(exception=True).warning(
            "library reminders: could not tell {} their deck is ready",
            loan.borrower_id)
        return False


# Formats whose pairings are all created the moment teams form, so "no
# unreported match left" really does mean the player has finished. Swiss is
# deliberately absent: utils.calculate_pairings builds its rounds one at a time
# (the `match_counter == 1` branch), so a Swiss player who has reported every
# match that EXISTS may still have rounds to come, and asking for their deck
# back then would take the cards they need for them.
FULLY_PAIRED_TYPES = ("random", "staked", "premade", "winston")

# A draft nobody will play any more of. 'abandoned' matters as much as
# 'completed': a draft that fell apart is exactly the one where nobody thinks
# to hand the cards back.
#
# The stage alone is NOT the finish line, which is why draft_is_over does not
# stop here: 2220 of 3283 finished drafts in production never advanced past
# 'pairings' and are identifiable only by a posted victory message. A stage-only
# check would miss two thirds of them -- and for Swiss, where the draft ending
# is the only return trigger, those borrowers would never be asked at all.
FINISHED_STAGES = ("completed", "abandoned")


async def players_done_playing(session_id: Any) -> "set[str]":
    """Who in this draft has no unreported match left.

    Empty for a progressively-paired format, where the question cannot be
    answered from the rows that exist yet -- see FULLY_PAIRED_TYPES.

    Keys on result_submitted_at rather than winner_id: a DRAW is reported and
    has no winner, so keying on the winner would leave that player permanently
    unfinished. MatchResult.find_unreported_for_user makes the other choice,
    which is why this does not reuse it.
    """
    from sqlalchemy import select

    from database.db_session import db_session
    from models.draft_session import DraftSession
    from models.match import MatchResult

    async with db_session() as session:
        stype = await session.scalar(
            select(DraftSession.session_type).where(
                DraftSession.session_id == str(session_id)))
        if stype not in FULLY_PAIRED_TYPES:
            return set()
        rows = (await session.execute(
            select(MatchResult.player1_id, MatchResult.player2_id,
                   MatchResult.result_submitted_at)
            .where(MatchResult.session_id == str(session_id)))).all()

    played: "set[str]" = set()
    outstanding: "set[str]" = set()
    for player1_id, player2_id, submitted_at in rows:
        for player_id in (player1_id, player2_id):
            if player_id is None:
                continue
            (played if submitted_at is not None else outstanding).add(str(player_id))
    return played - outstanding


async def draft_is_over(session_id: Any) -> bool:
    """Is this draft settled?

    Defers to helpers.stale_drafts.is_finished_draft, which reads the victory
    message as well as the stage -- the stage on its own is wrong for most
    finished drafts (see FINISHED_STAGES above). That means a draft counts as
    settled the moment a side clinches, with dead rubbers possibly unreported,
    which is deliberate: see reminder_due. 'abandoned' is checked here because
    is_finished_draft does not treat a collapsed draft as finished, and for
    getting cards back it plainly is.
    """
    from sqlalchemy import select

    from database.db_session import db_session
    from helpers.stale_drafts import is_finished_draft
    from models.draft_session import DraftSession

    async with db_session() as session:
        row = await session.scalar(
            select(DraftSession).where(
                DraftSession.session_id == str(session_id)))
    if row is None:
        return False
    return row.session_stage in FINISHED_STAGES or is_finished_draft(row)


async def send_due_reminders(now: Optional[datetime] = None) -> int:
    """Ask for every deck that is due back. Returns how many were asked.

    Called from the lending watchdog, so it must never raise: a DM problem for
    one borrower cannot stop the tick that also settles trades.

    The timestamp is written only after a DM actually goes. Stamping first would
    mean a borrower whose DMs were momentarily unreachable is never asked at
    all, which is the failure this whole mechanism exists to prevent.
    """
    from sqlalchemy import or_, select

    from database.db_session import db_session
    from models.card_loan import CardLoan

    at = now or datetime.now()
    async with db_session() as session:
        # The cooldown is ruled out in SQL, before any per-draft verdict work:
        # most ticks find nothing, and the ones that do should not first ask
        # "is this draft over" for every deck that was asked about an hour ago.
        # reminder_due still decides -- this only avoids work it would reject.
        loans = list((await session.scalars(
            select(CardLoan).where(
                CardLoan.state == RETURNABLE_STATE,
                or_(CardLoan.last_reminded_at.is_(None),
                    CardLoan.last_reminded_at <= at - REPEAT_AFTER)))).all())
    if not loans:
        return 0

    # One pair of reads per draft, not per loan: a pod shares its draft, so
    # eight borrowers would otherwise ask the same two questions eight times.
    # Two reads per DRAFT, not per loan: a pod shares its draft, so eight
    # borrowers would otherwise ask the same two questions eight times. The
    # finished-players SET is cached rather than a per-borrower answer, so the
    # cache serves every borrower in that pod.
    verdicts: "dict[str, tuple[set[str], bool]]" = {}
    due: "list[Any]" = []
    for loan in loans:
        session_id = session_of(loan)
        if session_id is None:
            continue
        if session_id not in verdicts:
            verdicts[session_id] = (await players_done_playing(session_id),
                                    await draft_is_over(session_id))
        finished, over = verdicts[session_id]
        if reminder_due(loan, done_playing=str(loan.borrower_id) in finished,
                        draft_over=over, now=at):
            due.append(loan)

    asked = 0
    for loan in due:
        # Re-read immediately before sending. The list was decided up front and
        # every DM is a network round trip, so a borrower can hand their deck
        # back while an earlier one is still in flight -- and asking them then
        # reads as the library having lost track of its own shelf.
        if not await _still_out(loan):
            continue
        if await _ask_for(loan):
            async with db_session() as session:
                row = await session.get(CardLoan, loan.id)
                if row is not None:
                    row.last_reminded_at = at
                    await session.commit()
            asked += 1
    if asked:
        logger.info("library reminders: asked {} borrower(s) to return a deck", asked)
    return asked


async def _still_out(loan: Any) -> bool:
    """Is this deck still in the borrower's hands, right now?"""
    from database.db_session import db_session
    from models.card_loan import CardLoan

    async with db_session() as session:
        row = await session.get(CardLoan, loan.id)
    return row is not None and row.state == RETURNABLE_STATE


async def _ask_for(loan: Any) -> bool:
    """DM one borrower. True only if it actually went."""
    import notification_service

    bot = _client()
    if bot is None:
        return False
    try:
        # The bool is the answer, not the absence of an exception -- see
        # notify_deck_ready. An unstamped failure is retried next tick; a
        # failure recorded as a send is a borrower nobody asks again for a day.
        return bool(await notification_service.send_dm(
            bot, loan.borrower_id, return_message(loan),
            label=f"return reminder for {loan.borrower_id}"))
    except Exception:
        logger.opt(exception=True).warning(
            "library reminders: could not ask {} to return a deck",
            loan.borrower_id)
        return False
