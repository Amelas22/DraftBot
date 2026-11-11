"""Telling a borrower their deck is ready, and asking for it back.

A whitelisted library says nothing on the shared board (see
test_signup_library_notice), so a DM is the only way a borrower learns a deck is
waiting. The asking-back half is driven by the lending watchdog, which polls
every ten minutes -- so "have I already asked" has to be recorded, or every tick
asks again.
"""
import re
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from conftest import seed_session
import services.library_reminders as mod
from services.library_reminders import (REPEAT_AFTER, deck_ready_message,
                                        reminder_due, return_message,
                                        session_of)

NOW = datetime(2026, 9, 26, 12, 0, 0)


def _loan(state="borrowed", last_reminded_at=None, source="draft:s1"):
    return SimpleNamespace(id=1, borrower_id="p1", guild_id="g1", state=state,
                           source=source, last_reminded_at=last_reminded_at,
                           cards=[{"name": "Swamp", "qty": 4}])


async def _a_loan(state="borrowed", *, last_reminded_at=None, ready_dm_at=None,
                  borrower="p1", session="s1", claimed_at=None,
                  on_behalf_of=None, guild="g", borrowed_at=None,
                  reminders_sent=None, reminders_failed=None):
    """One real CardLoan row. Returns its id."""
    from database.db_session import AsyncSessionLocal
    from models.card_loan import CardLoan

    async with AsyncSessionLocal() as s:
        loan = CardLoan(guild_id=guild, library_id="lib", borrower_id=borrower,
                        borrowed_at=borrowed_at, reminders_sent=reminders_sent,
                        reminders_failed=reminders_failed,
                        cards=[{"name": "Swamp", "qty": 4}], state=state,
                        source=f"draft:{session}", ready_dm_at=ready_dm_at,
                        last_reminded_at=last_reminded_at, claimed_at=claimed_at,
                        on_behalf_of=on_behalf_of)
        s.add(loan)
        await s.commit()
        return loan.id


async def _stamps(loan_id):
    from database.db_session import AsyncSessionLocal
    from models.card_loan import CardLoan

    async with AsyncSessionLocal() as s:
        row = await s.get(CardLoan, loan_id)
        return row.ready_dm_at, row.last_reminded_at


class _Recorder:
    """Stands in for notification_service.send_dm. Answers the way it does:
    True delivered, False not -- a stub returning None reads as a failure, which
    is how an ignored return value hid in the first place."""

    def __init__(self, delivers=True):
        self.sent = []
        self.delivers = delivers

    async def __call__(self, bot, user_id, message, view=None, label=None):
        self.sent.append((str(user_id), message, view))
        return self.delivers


@pytest.fixture
def dm(monkeypatch):
    """A registered bot and a recording send_dm. Without a bot the code
    correctly does nothing, which would make every assertion below pass for the
    wrong reason."""
    import bot_registry
    import notification_service

    recorder = _Recorder()
    monkeypatch.setattr(bot_registry, "get_bot", lambda: object())
    monkeypatch.setattr(notification_service, "send_dm", recorder)
    return recorder


# ---- which draft a loan belongs to -----------------------------------------

def test_a_draft_loan_names_its_session():
    assert session_of(_loan(source="draft:abc-123")) == "abc-123"


def test_a_loan_from_no_draft_names_nothing():
    """Fixture loans belong to no draft, so neither trigger can apply."""
    assert session_of(_loan(source="fixture:someone")) is None
    assert session_of(_loan(source=None)) is None


# ---- when to ask for cards back --------------------------------------------

def test_a_player_who_has_finished_playing_is_asked():
    """At once, with no hour's grace: subbing for a teammate means borrowing
    the absent player's deck, which they cannot do while they hold this one."""
    assert reminder_due(_loan(), done_playing=True, draft_settled=False, now=NOW)


def test_a_player_still_mid_draft_is_not_asked():
    """The cards are how they play their remaining matches."""
    assert not reminder_due(_loan(), done_playing=False, draft_settled=False, now=NOW)


def test_a_settled_draft_asks_everyone_still_holding():
    """The other arm, and the only one that applies to a format whose pairings
    are not all known up front."""
    assert reminder_due(_loan(), done_playing=False, draft_settled=True, now=NOW)


def test_somebody_already_asked_is_not_asked_again_immediately():
    """The watchdog polls every ten minutes."""
    loan = _loan(last_reminded_at=NOW - REPEAT_AFTER + timedelta(minutes=10))

    assert not reminder_due(loan, done_playing=True, draft_settled=True, now=NOW)


def test_somebody_still_holding_an_hour_later_is_asked_again():
    loan = _loan(last_reminded_at=NOW - REPEAT_AFTER - timedelta(minutes=1))

    assert reminder_due(loan, done_playing=True, draft_settled=True, now=NOW)


def test_the_repeat_comes_on_the_hour_not_a_tick_later():
    """Due an hour after the last ask means exactly that at the boundary:
    requiring strictly MORE than the interval would not ask at exactly an hour."""
    loan = _loan(last_reminded_at=NOW - REPEAT_AFTER)

    assert reminder_due(loan, done_playing=True, draft_settled=True, now=NOW)


def test_a_result_after_the_ask_in_a_decided_draft_asks_again():
    """A dead rubber means they still have the deck and just used it."""
    loan = _loan(last_reminded_at=NOW - timedelta(minutes=10))

    assert reminder_due(loan, done_playing=True, draft_settled=True,
                        played_since_ask=True, now=NOW)


def test_a_result_after_the_ask_waits_for_the_hour_while_the_draft_is_live():
    """Only a decided draft is chased on results. Undecided, the hourly repeat
    stands."""
    loan = _loan(last_reminded_at=NOW - timedelta(minutes=10))

    assert not reminder_due(loan, done_playing=True, draft_settled=False,
                            played_since_ask=True, now=NOW)


def test_a_deck_merely_on_offer_is_never_asked_for():
    """Nothing to return: an assigned loan is an offer nobody collected, and
    expire_stale_assignments retracts those on its own."""
    assert not reminder_due(_loan(state="assigned"), done_playing=True,
                            draft_settled=True, now=NOW)


def test_a_loan_belonging_to_no_draft_is_never_asked_for():
    assert not reminder_due(_loan(source="fixture:x"), done_playing=False,
                            draft_settled=False, now=NOW)


# ---- what the DMs say -------------------------------------------------------

def test_the_ready_dm_says_how_to_collect():
    """The board says nothing now, so this DM carries the whole signal."""
    msg = deck_ready_message([{"name": "Swamp", "qty": 4}], 0)
    assert "**Borrow my deck** below" in msg and "/library borrow" in msg


def test_the_ready_dm_names_the_hold_when_there_is_one():
    """What it costs decides whether they can take it, so it cannot be a
    surprise discovered at /library borrow."""
    assert "25" in deck_ready_message([{"name": "Swamp", "qty": 4}], 25)


def test_the_ready_dm_does_not_invent_a_hold_when_there_is_none():
    """Checks for a FIGURE, not for a word.

    This asserted `"deposit" not in msg`, which stopped testing anything the
    moment the wording became "hold" -- and a vacuous assertion is worse than no
    assertion, because the surface reads as covered. A free library must quote no
    number, whatever the sentence around it ends up saying.
    """
    msg = deck_ready_message([{"name": "Swamp", "qty": 4}], 0)

    assert "hold" not in msg.lower(), msg
    assert not re.search(r"\d+\s*tix", msg), msg


def test_the_return_dm_says_how_to_return():
    msg = return_message(_loan())
    assert "**Return my deck** below" in msg and "/library return" in msg


def test_the_return_dm_does_not_quote_a_deposit_figure():
    """It used to name the library's CURRENT price, which is not necessarily
    what this borrower put up. /library return states the real figure when it
    cannot be stale."""
    msg = return_message(_loan())

    assert not re.search(r"\d+ tix", msg), f"quoted a figure it cannot vouch for: {msg}"


def test_the_ask_in_a_decided_draft_says_why_it_came():
    assert return_message(_loan(), decided=True).startswith("Your draft is decided")


def test_the_ask_for_a_finished_player_does_not_claim_a_decision():
    assert "decided" not in return_message(_loan())


# ---- who has finished playing ----------------------------------------------

@pytest.mark.asyncio
async def test_a_player_with_every_match_reported_has_finished(test_db):
    await seed_session("s1", stype="random", stage="pairings", matches=[
        ("p1", "p2", "p1", NOW), ("p1", "p3", "p1", NOW), ("p1", "p4", "p4", NOW),
        ("p5", "p2", None, None)])

    assert "p1" in (await mod.draft_state("s1")).done_at


@pytest.mark.asyncio
async def test_a_player_with_a_match_outstanding_has_not(test_db):
    await seed_session("s1", stype="random", stage="pairings", matches=[
        ("p1", "p2", "p1", NOW), ("p1", "p3", None, None)])

    assert "p1" not in (await mod.draft_state("s1")).done_at


@pytest.mark.asyncio
async def test_a_drawn_match_counts_as_played(test_db):
    """A draw is reported but has no winner. Keying on the winner would read it
    as outstanding and never let that player finish -- which is why this keys on
    result_submitted_at, not MatchResult.winner_id."""
    await seed_session("s1", stype="random", stage="pairings",
                       matches=[("p1", "p2", None, NOW)])

    assert "p1" in (await mod.draft_state("s1")).done_at


@pytest.mark.asyncio
async def test_a_player_finished_when_they_reported_their_last_match(test_db):
    """The first ask is timed from here."""
    await seed_session("s1", stype="random", stage="pairings", matches=[
        ("p1", "p2", "p1", NOW - timedelta(hours=2)),
        ("p1", "p3", "p1", NOW - timedelta(minutes=30))])

    assert (await mod.draft_state("s1")).done_at["p1"] == NOW - timedelta(minutes=30)


@pytest.mark.asyncio
async def test_a_swiss_draft_never_reports_anybody_as_finished(test_db):
    """Swiss pairs one round at a time, so a player who reported every match
    that EXISTS may still have rounds to come. Asking then would take the cards
    they need. Swiss waits for the draft to settle."""
    await seed_session("s1", stype="swiss", stage="pairings",
                       matches=[("p1", "p2", "p1", NOW)])

    assert (await mod.draft_state("s1")).done_at == {}


# ---- when a draft counts as settled ----------------------------------------

@pytest.mark.asyncio
async def test_a_completed_draft_is_settled(test_db):
    await seed_session("s1", stype="random", stage="completed")

    assert (await mod.draft_state("s1")).settled is True


@pytest.mark.asyncio
async def test_an_abandoned_draft_is_settled_too(test_db):
    """Cards must come back from a draft that fell apart, not only one that
    finished -- abandoned is exactly when nobody thinks to return."""
    await seed_session("s1", stype="random", stage="abandoned")

    assert (await mod.draft_state("s1")).settled is True


@pytest.mark.asyncio
async def test_a_draft_still_running_is_not_settled(test_db):
    await seed_session("s1", stype="random", stage="pairings")

    assert (await mod.draft_state("s1")).settled is False


@pytest.mark.asyncio
async def test_a_clinched_draft_is_settled_even_at_the_pairings_stage(test_db):
    """The stage is not a finish line: most finished drafts never advance past
    'pairings' and are identifiable only by a posted victory message. Trusting
    the stage would miss two thirds of them -- and for Swiss, where settling is
    the only trigger, those borrowers would never be asked at all."""
    await seed_session("s1", stype="random", stage="pairings", victory=12345)

    assert (await mod.draft_state("s1")).settled is True


@pytest.mark.asyncio
async def test_a_chat_closing_within_the_half_hour_counts_as_closing(test_db):
    await seed_session("s1", stype="random", stage="pairings",
                       deletion_time=NOW + timedelta(minutes=29))
    assert (await mod.draft_state("s1")).chat_closing(NOW) is True


@pytest.mark.asyncio
async def test_a_chat_closing_in_an_hour_is_open(test_db):
    await seed_session("s1", stype="random", stage="pairings",
                       deletion_time=NOW + timedelta(hours=1))
    assert (await mod.draft_state("s1")).chat_closing(NOW) is False


@pytest.mark.asyncio
async def test_a_chat_past_its_time_counts_as_closing(test_db):
    """A tournament draft is extended by the sweep only after its time has
    passed; in that gap it reads as closing, and the next press works."""
    await seed_session("s1", stype="random", stage="pairings",
                       deletion_time=NOW - timedelta(minutes=5))
    assert (await mod.draft_state("s1")).chat_closing(NOW) is True


@pytest.mark.asyncio
async def test_a_chat_with_no_closing_time_is_open(test_db):
    await seed_session("s1", stype="random", stage="pairings")
    state = await mod.draft_state("s1")
    assert state.chat_closes_at is None and state.chat_closing(NOW) is False


@pytest.mark.asyncio
async def test_an_abandoned_draft_is_closing_whatever_its_time(test_db):
    await seed_session("s1", stype="random", stage="abandoned",
                       deletion_time=NOW + timedelta(days=6))
    state = await mod.draft_state("s1")
    assert state.abandoned is True and state.chat_closing(NOW) is True


def test_a_missing_draft_is_not_closing():
    assert mod.DraftState({}, False, None, {}, None, False).chat_closing(NOW) is False


# ---- the watchdog pass ------------------------------------------------------

@pytest.mark.asyncio
async def test_a_finished_player_is_asked_once_not_once_per_tick(test_db, dm):
    """The watchdog runs every ten minutes for as long as the cards are out, so
    this stands between a reminder and a pestering."""
    await seed_session("s1", stype="random", stage="pairings",
                       matches=[("p1", "p2", "p1", NOW)])
    await _a_loan()

    first = await mod.send_due_reminders(now=NOW)
    second = await mod.send_due_reminders(now=NOW + timedelta(minutes=10))

    assert first == 1 and second == 0, dm.sent
    assert [sent[0] for sent in dm.sent] == ["p1"]


@pytest.mark.asyncio
async def test_cards_still_out_an_hour_later_are_asked_for_again(test_db, dm):
    await seed_session("s1", stype="random", stage="completed")
    await _a_loan()

    await mod.send_due_reminders(now=NOW)

    assert await mod.send_due_reminders(now=NOW + timedelta(minutes=61)) == 1, dm.sent


@pytest.mark.asyncio
async def test_the_watchdog_asks_as_soon_as_the_draft_is_decided(test_db, dm):
    """The next tick after the decision asks, then hourly while the cards stay
    out -- and not between."""
    await seed_session("s1", stype="swiss", stage="completed",
                       matches=[("p1", "p2", "p1", NOW)])
    await _a_loan()

    asks = [await mod.send_due_reminders(now=NOW + timedelta(minutes=m))
            for m in (10, 50, 60, 70, 110, 120, 130)]

    assert asks == [1, 0, 0, 1, 0, 0, 1], dm.sent
    assert dm.sent[0][1].startswith("Your draft is decided"), dm.sent[0][1]


async def _report(session_id, p1, p2, winner, at):
    from database.db_session import AsyncSessionLocal
    from models.match import MatchResult

    async with AsyncSessionLocal() as s:
        s.add(MatchResult(session_id=session_id, match_number=99, player1_id=p1,
                          player2_id=p2, player1_wins=2, player2_wins=0,
                          winner_id=winner, result_submitted_at=at))
        await s.commit()


@pytest.mark.asyncio
async def test_a_dead_rubber_reported_after_the_ask_asks_again(test_db, dm):
    """Either player reporting counts: it is the borrower's match either way."""
    await seed_session("s1", stype="random", stage="pairings", victory=1,
                       matches=[("p1", "p2", "p1", NOW)])
    await _a_loan()
    assert await mod.send_due_reminders(now=NOW + timedelta(minutes=10)) == 1

    await _report("s1", "p2", "p1", "p2", NOW + timedelta(minutes=15))

    again = await mod.send_due_reminders(now=NOW + timedelta(minutes=20))
    quiet = await mod.send_due_reminders(now=NOW + timedelta(minutes=30))
    assert (again, quiet) == (1, 0), dm.sent


@pytest.mark.asyncio
async def test_a_result_dated_ahead_of_the_clock_asks_once_not_every_tick(test_db, dm):
    """Naive local timestamps repeat an hour when the clocks go back, so a dead
    rubber can read as later than every ask stamped after it."""
    await seed_session("s1", stype="random", stage="pairings", victory=1,
                       matches=[("p1", "p2", "p1", NOW)])
    await _a_loan()
    await mod.send_due_reminders(now=NOW + timedelta(minutes=10))

    await _report("s1", "p2", "p1", "p2", NOW + timedelta(minutes=50))

    asks = [await mod.send_due_reminders(now=NOW + timedelta(minutes=m))
            for m in (20, 30, 40)]
    assert asks == [0, 0, 0], dm.sent


@pytest.mark.asyncio
async def test_somebody_elses_dead_rubber_does_not_ask_again(test_db, dm):
    await seed_session("s1", stype="random", stage="pairings", victory=1,
                       matches=[("p1", "p2", "p1", NOW)])
    await _a_loan()
    await mod.send_due_reminders(now=NOW + timedelta(minutes=10))

    await _report("s1", "p3", "p4", "p3", NOW + timedelta(minutes=15))

    assert await mod.send_due_reminders(now=NOW + timedelta(minutes=20)) == 0, dm.sent


@pytest.mark.asyncio
async def test_the_watchdog_asks_as_soon_as_a_borrower_has_finished(test_db, dm):
    """p1 has reported every match of theirs while p3 vs p4 keeps the draft
    live. The next tick asks p1, and nobody else."""
    await seed_session("s1", stype="random", stage="pairings", matches=[
        ("p1", "p2", "p1", NOW),
        ("p3", "p4", None, None)])
    await _a_loan()
    await _a_loan(borrower="p3")

    assert await mod.send_due_reminders(now=NOW) == 1
    assert [sent[0] for sent in dm.sent] == ["p1"]


@pytest.mark.asyncio
async def test_a_player_mid_draft_is_left_alone(test_db, dm):
    await seed_session("s1", stype="random", stage="pairings",
                       matches=[("p1", "p2", "p1", NOW), ("p1", "p3", None, None)])
    await _a_loan()

    assert await mod.send_due_reminders(now=NOW) == 0, dm.sent


@pytest.mark.asyncio
async def test_a_deck_never_collected_is_not_chased(test_db, dm):
    await seed_session("s1", stype="random", stage="completed")
    await _a_loan(state="assigned")

    assert await mod.send_due_reminders(now=NOW) == 0, dm.sent


@pytest.mark.asyncio
async def test_a_swiss_draft_that_settled_still_gets_chased(test_db, dm):
    """The case a stage-only check dropped: Swiss has no per-player trigger, so
    if the draft never looks settled nobody is ever asked."""
    await seed_session("s1", stype="swiss", stage="pairings", victory=999,
                       matches=[("p1", "p2", "p1", NOW)])
    await _a_loan()

    assert await mod.send_due_reminders(now=NOW) == 1, dm.sent


@pytest.mark.asyncio
async def test_a_failed_dm_is_not_recorded_as_sent(test_db, dm):
    """send_dm RETURNS False for a blocked inbox -- it does not raise. A wrapper
    ignoring that return stamps last_reminded_at for a DM that never arrived,
    and the borrower goes unasked until the cooldown would have lapsed."""
    await seed_session("s1", stype="random", stage="completed")
    loan_id = await _a_loan()
    dm.delivers = False

    assert await mod.send_due_reminders(now=NOW) == 0
    assert (await _stamps(loan_id))[1] is None


@pytest.mark.asyncio
async def test_a_deck_returned_mid_scan_is_not_chased(test_db, dm):
    """The scan decides the list up front, then sends one DM at a time. A second
    borrower can hand their deck back while the first DM is in flight, and
    asking them afterwards reads as the library losing track of its shelf.

    Two decks, and the first DM returns the second -- so _still_out is the only
    thing that can keep the count at one.
    """
    from database.db_session import AsyncSessionLocal
    from models.card_loan import CardLoan

    await seed_session("s1", stype="random", stage="completed")
    first = await _a_loan(borrower="p1")
    second = await _a_loan(borrower="p2")
    original = dm.__call__

    async def _return_the_other(bot, user_id, message, view=None, label=None):
        if str(user_id) == "p1":
            async with AsyncSessionLocal() as s:
                row = await s.get(CardLoan, second)
                row.state = "returned"
                await s.commit()
        return await original(bot, user_id, message, view, label)

    import notification_service
    notification_service.send_dm = _return_the_other

    asked = await mod.send_due_reminders(now=NOW)

    assert asked == 1, f"the returned deck was chased anyway: {dm.sent}"
    assert [sent[0] for sent in dm.sent] == ["p1"]
    assert (await _stamps(second))[1] is None
    assert first != second


# ---- announcing a ready deck, and retrying when that fails -----------------

@pytest.mark.asyncio
async def test_a_deck_nobody_has_been_told_about_is_announced(test_db, dm):
    """The retry that closes the gap: the loan is committed before the DM is
    attempted, and later assignment passes skip a borrower who already has a
    loan -- so without this a Discord blip meant they were never told."""
    loan_id = await _a_loan(state="assigned")

    assert await mod.announce_ready_decks(now=NOW) == 1, dm.sent
    assert (await _stamps(loan_id))[0] == NOW


@pytest.mark.asyncio
async def test_a_borrower_already_told_is_not_told_again(test_db, dm):
    await _a_loan(state="assigned", ready_dm_at=NOW - timedelta(days=3))

    assert await mod.announce_ready_decks(now=NOW) == 0, dm.sent


@pytest.mark.asyncio
async def test_a_borrower_who_already_has_the_cards_is_not_told(test_db, dm):
    """They collected it without needing the DM. Telling them a deck is ready to
    borrow, when it is already in their account, is noise."""
    await _a_loan(state="borrowed")

    assert await mod.announce_ready_decks(now=NOW) == 0, dm.sent


@pytest.mark.asyncio
async def test_a_deck_nobody_needs_any_more_is_not_announced(test_db, dm):
    """expire_stale_assignments retracts an offer once its draft is over.
    Announcing it then invites somebody to run a command with nothing to hand
    them."""
    await _a_loan(state="expired")

    assert await mod.announce_ready_decks(now=NOW) == 0, dm.sent


@pytest.mark.asyncio
async def test_an_undelivered_announcement_is_retried_next_tick(test_db, dm):
    """The whole point: send_dm returning False must leave the loan unstamped."""
    loan_id = await _a_loan(state="assigned")
    dm.delivers = False

    assert await mod.announce_ready_decks(now=NOW) == 0
    assert (await _stamps(loan_id))[0] is None

    dm.delivers = True
    assert await mod.announce_ready_decks(now=NOW) == 1, "the retry never came"
    assert (await _stamps(loan_id))[0] == NOW


# -- who may ask for a deck again ---------------------------------------------


def _state(done_at=None, settled=False, closes_at=None, abandoned=False):
    return mod.DraftState(done_at or {}, settled, None, {}, closes_at, abandoned)


def test_a_player_with_a_match_left_may_request_after_the_decision():
    """Settlement ends the reservation, not the request: dead rubbers get played."""
    state = _state(done_at={"p2": NOW}, settled=True, closes_at=NOW + timedelta(hours=2))
    assert mod.may_still_request(state, "p1", NOW) is None


def test_a_player_with_every_match_reported_is_over():
    state = _state(done_at={"p1": NOW}, closes_at=NOW + timedelta(hours=2))
    assert mod.may_still_request(state, "p1", NOW) == "over"


def test_a_chat_about_to_close_refuses_everyone():
    state = _state(closes_at=NOW + timedelta(minutes=10))
    assert mod.may_still_request(state, "p1", NOW) == "closed"


def test_closing_outranks_over():
    state = _state(done_at={"p1": NOW}, closes_at=NOW)
    assert mod.may_still_request(state, "p1", NOW) == "closed"


def test_a_swiss_draft_gates_only_on_the_chat():
    """done_at is empty for a progressively-paired format; the chat is the horizon."""
    state = _state(settled=True, closes_at=NOW + timedelta(hours=1))
    assert mod.may_still_request(state, "p1", NOW) is None


def test_the_offer_window_does_not_end_a_request():
    """who_can_still_use says nobody an hour after the end; a request does not care."""
    state = mod.DraftState({}, False, NOW - timedelta(hours=3), {},
                           NOW + timedelta(days=6), False)
    assert mod.who_can_still_use(state, ["p1"], NOW) == set()
    assert mod.may_still_request(state, "p1", NOW) is None


@pytest.mark.asyncio
async def test_a_deck_claimed_late_is_not_chased_on_the_decision(test_db, dm):
    """Borrowed for a dead rubber after a side clinched: they are playing it."""
    await seed_session("s1", stype="random", stage="completed",
                       matches=[("p1", "p2", None, None)],
                       deletion_time=NOW + timedelta(hours=2))
    await _a_loan(claimed_at=NOW - timedelta(minutes=5))

    assert await mod.send_due_reminders(now=NOW) == 0


@pytest.mark.asyncio
async def test_a_deck_claimed_late_is_chased_once_its_chat_is_closing(test_db, dm):
    await seed_session("s1", stype="random", stage="completed",
                       matches=[("p1", "p2", None, None)],
                       deletion_time=NOW + timedelta(minutes=20))
    await _a_loan(claimed_at=NOW - timedelta(hours=1))

    assert await mod.send_due_reminders(now=NOW) == 1
    assert dm.sent[0][1].startswith("Your draft is decided")


@pytest.mark.asyncio
async def test_a_deck_claimed_late_is_chased_once_they_have_reported(test_db, dm):
    await seed_session("s1", stype="random", stage="completed",
                       matches=[("p1", "p2", "p1", NOW - timedelta(minutes=1))],
                       deletion_time=NOW + timedelta(hours=2))
    await _a_loan(claimed_at=NOW - timedelta(hours=2))

    assert await mod.send_due_reminders(now=NOW) == 1
    assert not dm.sent[0][1].startswith("Your draft is decided")


@pytest.mark.asyncio
async def test_a_first_time_loan_is_still_chased_on_the_decision(test_db, dm):
    await seed_session("s1", stype="random", stage="completed",
                       matches=[("p1", "p2", None, None)],
                       deletion_time=NOW + timedelta(hours=2))
    await _a_loan()

    assert await mod.send_due_reminders(now=NOW) == 1


@pytest.mark.asyncio
async def test_the_reminder_carries_a_return_button(test_db, dm):
    await seed_session("s1", stype="swiss", stage="completed",
                       matches=[("p1", "p2", "p1", NOW)])
    await _a_loan()

    assert await mod.send_due_reminders(now=NOW) == 1
    view = dm.sent[0][2]
    assert [i.custom_id for i in view.children] == ["library_dm:return"]


@pytest.mark.asyncio
async def test_the_ready_dm_carries_a_borrow_button(test_db, dm):
    loan_id = await _a_loan(state="assigned")

    assert await mod.announce_one(loan_id, at=NOW) is True
    view = dm.sent[0][2]
    assert [i.custom_id for i in view.children] == ["library_dm:borrow"]


@pytest.mark.asyncio
async def test_an_undelivered_dm_with_a_button_is_still_not_recorded(test_db, monkeypatch):
    import bot_registry, notification_service
    monkeypatch.setattr(bot_registry, "get_bot", lambda: object())
    monkeypatch.setattr(notification_service, "send_dm", _Recorder(delivers=False))
    loan_id = await _a_loan(state="assigned")

    assert await mod.announce_one(loan_id, at=NOW) is False
    assert (await _stamps(loan_id))[0] is None


@pytest.mark.asyncio
async def test_a_sub_loan_is_timed_by_the_absent_players_matches(test_db, dm):
    await seed_session("s1", stype="random", stage="pairings",
                       sign_ups={"p1": "Ali", "p2": "Bo", "p3": "Cy", "p4": "Di"},
                       matches=[("p2", "p3", "p2", NOW),            # Bo's last match, reported
                                ("p1", "p4", None, None)])          # Ali's own, unreported
    await _a_loan(borrower="p1", claimed_at=NOW, on_behalf_of="p2")

    assert await mod.send_due_reminders(now=NOW + timedelta(minutes=60)) == 1
    assert "the deck you're holding for **Bo**" in dm.sent[0][1]


@pytest.mark.asyncio
async def test_the_ready_dm_for_a_sub_names_whose_deck_it_is(test_db, dm):
    await seed_session("s1", stype="random", stage="pairings",
                       sign_ups={"p1": "Ali", "p2": "Bo"}, matches=[("p1", "p2", None, None)])
    loan_id = await _a_loan(state="assigned", borrower="p1", claimed_at=NOW, on_behalf_of="p2")

    assert await mod.announce_one(loan_id, at=NOW) is True
    assert dm.sent[0][1].startswith("📚 **Bo**'s drafted deck is ready for you to borrow as their sub")


# ---- overdue decks go to the moderators -------------------------------------

GUILD = "4242"


def _out(hours, reminders, **over):
    return SimpleNamespace(id=1, borrower_id="p1", guild_id=GUILD,
                           state=over.get("state", "borrowed"),
                           borrowed_at=NOW - timedelta(hours=hours),
                           reminders_sent=reminders,
                           reminders_failed=over.get("failed"),
                           overdue_posted_at=over.get("overdue_posted_at"))


def test_a_deck_out_a_day_and_asked_for_twice_is_overdue():
    assert mod.overdue(_out(25, 2), NOW)


def test_a_deck_out_a_day_but_asked_for_once_is_not_yet_overdue():
    """One DM can be missed; the moderators hear about a pattern, not a blip."""
    assert not mod.overdue(_out(30, 1), NOW)
    assert not mod.overdue(_out(30, None), NOW)


def test_a_deck_asked_for_often_inside_the_day_is_not_overdue():
    assert not mod.overdue(_out(23, 9), NOW)


def test_overdue_means_more_than_a_day_not_a_day():
    assert not mod.overdue(_out(24, 5), NOW)


def test_a_deck_already_reported_is_not_overdue_again():
    assert not mod.overdue(_out(40, 5, overdue_posted_at=NOW), NOW)


def test_a_deck_on_its_way_back_is_not_overdue():
    assert not mod.overdue(_out(40, 5, state="return_pending"), NOW)


class _Channel:
    def __init__(self, name="mod-chat", fails=False):
        self.name = name
        self.fails = fails
        self.posts = []

    async def send(self, content, allowed_mentions=None):
        if self.fails:
            import discord
            raise discord.HTTPException(
                SimpleNamespace(status=500, reason="boom"), "boom")
        self.posts.append((content, allowed_mentions))


def _bot(*channels, guild=GUILD):
    home = SimpleNamespace(id=int(guild), text_channels=list(channels))
    return SimpleNamespace(
        get_guild=lambda gid: home if str(gid) == guild else None)


async def _overdue_stamp(loan_id):
    from database.db_session import AsyncSessionLocal
    from models.card_loan import CardLoan

    async with AsyncSessionLocal() as s:
        row = await s.get(CardLoan, loan_id)
        return row.overdue_posted_at, row.reminders_sent, row.reminders_failed


@pytest.mark.asyncio
async def test_each_delivered_reminder_is_counted(test_db, dm):
    await seed_session("s1", stype="random", stage="completed")
    loan_id = await _a_loan()

    await mod.send_due_reminders(now=NOW)
    await mod.send_due_reminders(now=NOW + REPEAT_AFTER)

    assert (await _overdue_stamp(loan_id))[1] == 2


@pytest.mark.asyncio
async def test_an_undelivered_reminder_is_not_counted(test_db, dm):
    """A borrower with closed DMs has not been asked, however often we tried."""
    dm.delivers = False
    await seed_session("s1", stype="random", stage="completed")
    loan_id = await _a_loan()

    await mod.send_due_reminders(now=NOW)

    assert (await _overdue_stamp(loan_id))[1] is None


@pytest.mark.asyncio
async def test_an_overdue_deck_is_reported_to_the_moderators_once(test_db):
    """The watchdog runs every ten minutes; the moderators want one notice."""
    loan_id = await _a_loan(guild=GUILD, borrowed_at=NOW - timedelta(hours=26),
                            reminders_sent=3)
    channel = _Channel()

    first = await mod.post_overdue_decks(_bot(channel), now=NOW)
    second = await mod.post_overdue_decks(_bot(channel),
                                          now=NOW + timedelta(minutes=10))

    assert (first, second) == (1, 0)
    [(content, allowed)] = channel.posts
    assert "<@p1>" in content and "out 26 hours" in content
    assert "3 DMs sent" in content and "failing" not in content
    assert (await _overdue_stamp(loan_id))[0] == NOW


@pytest.mark.asyncio
async def test_the_notice_names_the_borrower_without_pinging_them(test_db):
    import discord

    await _a_loan(guild=GUILD, borrowed_at=NOW - timedelta(hours=26),
                  reminders_sent=2)
    channel = _Channel()

    await mod.post_overdue_decks(_bot(channel), now=NOW)

    allowed = channel.posts[0][1]
    assert isinstance(allowed, discord.AllowedMentions)
    assert allowed.users is False and allowed.everyone is False


@pytest.mark.asyncio
async def test_a_deck_not_yet_overdue_is_not_reported(test_db):
    await _a_loan(guild=GUILD, borrowed_at=NOW - timedelta(hours=26),
                  reminders_sent=1)
    await _a_loan(guild=GUILD, borrower="p2",
                  borrowed_at=NOW - timedelta(hours=5), reminders_sent=4)
    channel = _Channel()

    assert await mod.post_overdue_decks(_bot(channel), now=NOW) == 0
    assert channel.posts == []


@pytest.mark.asyncio
async def test_a_notice_that_did_not_post_is_tried_again(test_db):
    loan_id = await _a_loan(guild=GUILD, borrowed_at=NOW - timedelta(hours=26),
                            reminders_sent=2)

    failed = await mod.post_overdue_decks(_bot(_Channel(fails=True)), now=NOW)
    working = _Channel()
    retried = await mod.post_overdue_decks(_bot(working),
                                           now=NOW + timedelta(minutes=10))

    assert (failed, retried) == (0, 1)
    assert len(working.posts) == 1
    assert (await _overdue_stamp(loan_id))[0] == NOW + timedelta(minutes=10)


@pytest.mark.asyncio
async def test_a_guild_with_no_mod_channel_reports_nothing_and_keeps_the_deck_owed(test_db):
    """Not stamped: the notice is still owed if the channel appears later."""
    loan_id = await _a_loan(guild=GUILD, borrowed_at=NOW - timedelta(hours=26),
                            reminders_sent=2)

    assert await mod.post_overdue_decks(_bot(_Channel(name="general")), now=NOW) == 0
    assert (await _overdue_stamp(loan_id))[0] is None


@pytest.mark.asyncio
async def test_the_mod_channel_is_the_one_the_guild_configured(test_db, monkeypatch):
    import config

    monkeypatch.setattr(config, "get_config", lambda guild_id: {
        "activity_tracking": {"enabled": False, "mod_chat_channel": "staff"}})
    await _a_loan(guild=GUILD, borrowed_at=NOW - timedelta(hours=26),
                  reminders_sent=2)
    staff, other = _Channel(name="staff"), _Channel(name="mod-chat")

    assert await mod.post_overdue_decks(_bot(other, staff), now=NOW) == 1
    assert len(staff.posts) == 1 and other.posts == []


@pytest.mark.asyncio
async def test_many_overdue_decks_are_split_under_discords_limit(test_db):
    """A post over 2000 characters is refused, and a refused notice would be
    retried every tick without ever landing."""
    for n in range(40):
        await _a_loan(guild=GUILD, borrower=f"1234567890123456{n:02d}",
                      borrowed_at=NOW - timedelta(hours=30), reminders_sent=6)
    channel = _Channel()

    assert await mod.post_overdue_decks(_bot(channel), now=NOW) == 40
    assert len(channel.posts) > 1
    assert all(len(content) <= 2000 for content, _ in channel.posts)
    assert sum(content.count("<@") for content, _ in channel.posts) == 40


@pytest.mark.asyncio
async def test_a_deck_handed_back_mid_scan_is_not_reported(test_db, monkeypatch):
    """The scan reads its list up front; a deck returned since is not out."""
    from database.db_session import AsyncSessionLocal
    from models.card_loan import CardLoan
    import services.card_lending_service as lending

    loan_id = await _a_loan(guild=GUILD, borrowed_at=NOW - timedelta(hours=26),
                            reminders_sent=2)
    await _a_loan(guild=GUILD, borrower="p2",
                  borrowed_at=NOW - timedelta(hours=25), reminders_sent=2)
    real = lending.held_cards

    async def returns_the_other_deck(loan):
        async with AsyncSessionLocal() as s:
            (await s.get(CardLoan, loan_id + 1)).state = "return_pending"
            await s.commit()
        return await real(loan)

    monkeypatch.setattr(lending, "held_cards", returns_the_other_deck)
    channel = _Channel()

    assert await mod.post_overdue_decks(_bot(channel), now=NOW) == 1
    [(content, _)] = channel.posts
    assert "<@p1>" in content and "<@p2>" not in content


@pytest.mark.asyncio
async def test_a_guild_without_a_mod_channel_does_not_cost_another_its_notice(test_db):
    await _a_loan(guild="777", borrowed_at=NOW - timedelta(hours=30),
                  reminders_sent=2)
    await _a_loan(guild=GUILD, borrower="p2",
                  borrowed_at=NOW - timedelta(hours=26), reminders_sent=2)
    channel = _Channel()

    assert await mod.post_overdue_decks(_bot(channel), now=NOW) == 1
    assert "<@p2>" in channel.posts[0][0]


@pytest.mark.asyncio
async def test_a_channel_the_bot_may_not_post_in_keeps_the_deck_owed(test_db):
    import discord

    class _Locked(_Channel):
        async def send(self, content, allowed_mentions=None):
            raise discord.Forbidden(
                SimpleNamespace(status=403, reason="no"), "no")

    loan_id = await _a_loan(guild=GUILD, borrowed_at=NOW - timedelta(hours=26),
                            reminders_sent=2)

    warnings = []
    handle = mod.logger.add(lambda m: warnings.append(str(m)), level="WARNING")
    try:
        locked = _bot(_Locked())
        assert await mod.post_overdue_decks(locked, now=NOW) == 0
        assert await mod.post_overdue_decks(locked, now=NOW) == 0
    finally:
        mod.logger.remove(handle)

    assert (await _overdue_stamp(loan_id))[0] is None
    assert len([w for w in warnings if "not allowed to post" in w]) == 1, \
        "said once, not on every ten-minute tick"


# ---- a borrower the DMs cannot reach ----------------------------------------

def test_a_deck_out_a_day_whose_dms_keep_failing_is_overdue():
    """They can never be asked twice, and they are who a moderator must chase."""
    assert mod.overdue(_out(25, None, failed=2), NOW)
    assert not mod.overdue(_out(25, None, failed=1), NOW)
    assert not mod.overdue(_out(23, None, failed=9), NOW)


@pytest.mark.asyncio
async def test_failed_reminders_are_counted_until_one_lands(test_db, dm):
    await seed_session("s1", stype="random", stage="completed")
    loan_id = await _a_loan()

    dm.delivers = False
    await mod.send_due_reminders(now=NOW)
    await mod.send_due_reminders(now=NOW + timedelta(minutes=10))
    assert (await _overdue_stamp(loan_id))[1:] == (None, 2)

    dm.delivers = True
    await mod.send_due_reminders(now=NOW + timedelta(minutes=20))
    assert (await _overdue_stamp(loan_id))[1:] == (1, 0)


@pytest.mark.asyncio
async def test_the_notice_says_when_dms_are_failing(test_db):
    await _a_loan(guild=GUILD, borrowed_at=NOW - timedelta(hours=26),
                  reminders_sent=None, reminders_failed=7)
    await _a_loan(guild=GUILD, borrower="p2",
                  borrowed_at=NOW - timedelta(hours=25),
                  reminders_sent=4, reminders_failed=3)
    channel = _Channel()

    assert await mod.post_overdue_decks(_bot(channel), now=NOW) == 2
    unreachable, went_quiet = channel.posts[0][0].splitlines()[-2:]
    assert "<@p1>" in unreachable and "0 DMs sent — **DMs are failing**" in unreachable
    assert "<@p2>" in went_quiet and "4 DMs sent — **DMs are failing**" in went_quiet


# ---- a deck held past the limit is asked for whatever the draft is doing ----

def _held(hours, **over):
    return SimpleNamespace(id=1, borrower_id="p1", guild_id="g1",
                           state=over.get("state", "borrowed"), source="draft:s1",
                           last_reminded_at=over.get("last_reminded_at"),
                           borrowed_at=NOW - timedelta(hours=hours),
                           cards=[{"name": "Swamp", "qty": 4}])


def test_a_deck_held_past_the_limit_is_asked_for_mid_draft():
    assert reminder_due(_held(4), done_playing=False, draft_settled=False, now=NOW)


def test_a_deck_inside_the_limit_is_left_alone_mid_draft():
    assert not reminder_due(_held(2), done_playing=False, draft_settled=False, now=NOW)
    assert not reminder_due(_held(3), done_playing=False, draft_settled=False, now=NOW)


def test_a_deck_held_past_the_limit_is_still_asked_only_hourly():
    asked = _held(5, last_reminded_at=NOW - timedelta(minutes=20))
    assert not reminder_due(asked, done_playing=False, draft_settled=False, now=NOW)


def test_the_ask_for_an_overstayed_deck_says_why_and_that_it_can_be_borrowed_again():
    msg = return_message(_held(4), overstayed=True, may_reborrow=True)
    assert msg.startswith("You have had this deck for more than 3 hours.")
    assert "borrow it again" in msg


def test_the_ask_does_not_offer_a_reborrow_unless_told_one_is_possible():
    assert "borrow it again" not in return_message(_held(4))
    assert "borrow it again" not in return_message(_held(4), overstayed=True)


@pytest.mark.asyncio
async def test_the_watchdog_asks_for_a_deck_held_past_the_limit_mid_draft(test_db, dm):
    """An unreported match used to keep the cards out, unasked-for, overnight."""
    await seed_session("s1", stype="random", stage="pairings",
                       matches=[("p1", "p2", None, None)])
    early = await _a_loan(borrowed_at=NOW - timedelta(hours=2))
    assert await mod.send_due_reminders(now=NOW) == 0

    assert await mod.send_due_reminders(now=NOW + timedelta(hours=2)) == 1
    assert dm.sent[0][1].startswith("You have had this deck for more than 3 hours.")
    assert "borrow it again" in dm.sent[0][1]
    assert (await _stamps(early))[1] == NOW + timedelta(hours=2)


@pytest.mark.asyncio
async def test_no_reborrow_is_promised_once_the_draft_chat_is_closing(test_db, dm):
    """A request is refused once the chat is closing, so the DM must not offer one."""
    await seed_session("s1", stype="random", stage="pairings",
                       matches=[("p1", "p2", None, None)],
                       deletion_time=NOW + timedelta(minutes=10))
    await _a_loan(borrowed_at=NOW - timedelta(hours=4))

    assert await mod.send_due_reminders(now=NOW) == 1
    assert dm.sent[0][1].startswith("You have had this deck for more than 3 hours.")
    assert "borrow it again" not in dm.sent[0][1]


@pytest.mark.asyncio
async def test_a_sub_is_not_promised_a_reborrow(test_db, dm):
    """Handing a subbed deck back ends the sub's turn; there is nothing to re-borrow."""
    await seed_session("s1", stype="random", stage="pairings",
                       matches=[("p9", "p2", None, None)],
                       sign_ups={"p9": "Absent"})
    await _a_loan(borrowed_at=NOW - timedelta(hours=4), on_behalf_of="p9")

    assert await mod.send_due_reminders(now=NOW) == 1
    assert "holding for **Absent**" in dm.sent[0][1]
    assert "borrow it again" not in dm.sent[0][1]


@pytest.mark.asyncio
async def test_a_loan_that_cannot_be_described_does_not_cost_the_others(test_db, monkeypatch):
    import services.card_lending_service as lending

    broken = await _a_loan(guild=GUILD, borrowed_at=NOW - timedelta(hours=30),
                           reminders_sent=2)
    await _a_loan(guild=GUILD, borrower="p2",
                  borrowed_at=NOW - timedelta(hours=26), reminders_sent=2)
    real = lending.held_cards

    async def fails_for_one(loan):
        if loan.id == broken:
            raise RuntimeError("unreadable ledger")
        return await real(loan)

    monkeypatch.setattr(lending, "held_cards", fails_for_one)
    channel = _Channel()

    assert await mod.post_overdue_decks(_bot(channel), now=NOW) == 1
    assert "<@p2>" in channel.posts[0][0] and "<@p1>" not in channel.posts[0][0]
    assert (await _overdue_stamp(broken))[0] is None
