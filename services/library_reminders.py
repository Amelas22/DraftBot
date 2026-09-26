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
    draft is over -- whichever comes first. A borrower mid-draft is never
    asked: the cards are how they play the rest of it.

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


def return_message(loan: Any, collateral: int = 0) -> str:
    """The ask. Says what it is for, because a borrower who has finished a draft
    has no other reason to think about the cards again.

    The refund is named only when a deposit was actually taken: telling somebody
    who put nothing up that they will be refunded sends them looking for tix
    that were never theirs.
    """
    refund = (f" Your **{collateral} tix** deposit comes back when it lands."
              if collateral else "")
    return (f"📦 Please return your borrowed deck — **{_deck_summary(loan.cards)}**.\n"
            f"Run `/library return` and the library will collect it.{refund}\n"
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
        await notification_service.send_dm(
            bot, loan.borrower_id, deck_ready_message(loan.cards, collateral),
            label=f"deck ready for {loan.borrower_id}")
        return True
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
    """Will nobody play any more of this draft?

    Defers to helpers.stale_drafts.is_finished_draft for "was it played out",
    which reads the victory message as well as the stage -- the stage on its own
    is wrong for most finished drafts (see FINISHED_STAGES above). 'abandoned'
    is checked here because is_finished_draft does not treat a collapsed draft
    as finished, and for returning cards it plainly is.
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
    from sqlalchemy import select

    from database.db_session import db_session
    from models.card_loan import CardLoan

    at = now or datetime.now()
    async with db_session() as session:
        loans = list((await session.scalars(
            select(CardLoan).where(CardLoan.state == RETURNABLE_STATE))).all())
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
        collateral = 0
        try:
            from services.card_lending_service import collateral_for
            # The loan's OWN terms, not the server's current binding: a deck
            # assigned before the server was re-bound keeps the price it went
            # out under, and quoting today's would promise the wrong refund.
            collateral = await collateral_for(loan, loan.guild_id) or 0
        except Exception:
            logger.opt(exception=True).warning(
                "library reminders: could not read the terms behind loan {}; "
                "asking without naming a deposit", loan.id)
        if await _ask_for(loan, collateral):
            async with db_session() as session:
                row = await session.get(CardLoan, loan.id)
                if row is not None:
                    row.last_reminded_at = at
                    await session.commit()
            asked += 1
    if asked:
        logger.info("library reminders: asked {} borrower(s) to return a deck", asked)
    return asked


async def _ask_for(loan: Any, collateral: int) -> bool:
    """DM one borrower. True only if it actually went."""
    import notification_service

    bot = _client()
    if bot is None:
        return False
    try:
        await notification_service.send_dm(
            bot, loan.borrower_id, return_message(loan, collateral),
            label=f"return reminder for {loan.borrower_id}")
        return True
    except Exception:
        logger.opt(exception=True).warning(
            "library reminders: could not ask {} to return a deck",
            loan.borrower_id)
        return False
