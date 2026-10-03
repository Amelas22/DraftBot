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
from services.library_reminders import (REMIND_AFTER_FINISH, REPEAT_AFTER,
                                        deck_ready_message, reminder_due,
                                        return_message, session_of)

NOW = datetime(2026, 9, 26, 12, 0, 0)


def _loan(state="borrowed", last_reminded_at=None, source="draft:s1"):
    return SimpleNamespace(id=1, borrower_id="p1", guild_id="g1", state=state,
                           source=source, last_reminded_at=last_reminded_at,
                           cards=[{"name": "Swamp", "qty": 4}])


async def _a_loan(state="borrowed", *, last_reminded_at=None, ready_dm_at=None,
                  borrower="p1", session="s1"):
    """One real CardLoan row. Returns its id."""
    from database.db_session import AsyncSessionLocal
    from models.card_loan import CardLoan

    async with AsyncSessionLocal() as s:
        loan = CardLoan(guild_id="g", library_id="lib", borrower_id=borrower,
                        cards=[{"name": "Swamp", "qty": 4}], state=state,
                        source=f"draft:{session}", ready_dm_at=ready_dm_at,
                        last_reminded_at=last_reminded_at)
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

    async def __call__(self, bot, user_id, message, label=None):
        self.sent.append((str(user_id), message))
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
    """last_reminded_at is stamped at a watchdog tick, so the tick an interval
    later lands exactly on the boundary. Requiring strictly MORE than the
    interval slipped every repeat a whole tick: hourly became every 70 min."""
    loan = _loan(last_reminded_at=NOW - REPEAT_AFTER)

    assert reminder_due(loan, done_playing=True, draft_settled=True, now=NOW)


def test_nobody_is_asked_in_the_first_hour_after_finishing():
    """Room to hand the deck back unprompted, or to play a dead rubber."""
    finished = NOW - REMIND_AFTER_FINISH + timedelta(minutes=1)

    assert not reminder_due(_loan(), done_playing=True, draft_settled=False,
                            finished_at=finished, now=NOW)


def test_the_first_ask_comes_an_hour_after_finishing():
    finished = NOW - REMIND_AFTER_FINISH

    assert reminder_due(_loan(), done_playing=True, draft_settled=False,
                        finished_at=finished, now=NOW)


def test_the_hourly_repeat_holds_after_the_first_ask():
    """Once asked, the finish time no longer matters -- only the last ask."""
    loan = _loan(last_reminded_at=NOW - REPEAT_AFTER - timedelta(minutes=1))

    assert reminder_due(loan, done_playing=True, draft_settled=False,
                        finished_at=NOW - timedelta(hours=5), now=NOW)


def test_a_result_reported_after_the_first_ask_does_not_postpone_the_next():
    """A dead rubber reported after the first ask moves the draft's latest
    result later. Only the first ask waits on the finish time."""
    loan = _loan(last_reminded_at=NOW - REPEAT_AFTER)

    assert reminder_due(loan, done_playing=False, draft_settled=True,
                        finished_at=NOW - timedelta(minutes=10), now=NOW)


def test_an_unknown_finish_time_asks_at_once():
    """A draft settled with nothing reported (abandoned) has no finish time to
    wait from, and holding the cards an extra hour helps nobody."""
    assert reminder_due(_loan(), done_playing=False, draft_settled=True,
                        finished_at=None, now=NOW)


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
    assert "/library borrow" in deck_ready_message([{"name": "Swamp", "qty": 4}], 0)


def test_the_ready_dm_names_the_deposit_when_there_is_one():
    """What it costs decides whether they can take it, so it cannot be a
    surprise discovered at /library borrow."""
    assert "25" in deck_ready_message([{"name": "Swamp", "qty": 4}], 25)


def test_the_ready_dm_does_not_invent_a_deposit_when_there_is_none():
    msg = deck_ready_message([{"name": "Swamp", "qty": 4}], 0)

    assert "deposit" not in msg.lower(), msg


def test_the_return_dm_says_how_to_return():
    assert "/library return" in return_message(_loan())


def test_the_return_dm_does_not_quote_a_deposit_figure():
    """It used to name the library's CURRENT price, which is not necessarily
    what this borrower put up. /library return states the real figure when it
    cannot be stale."""
    msg = return_message(_loan())

    assert not re.search(r"\d+ tix", msg), f"quoted a figure it cannot vouch for: {msg}"


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
async def test_a_draft_was_decided_at_its_latest_result(test_db):
    await seed_session("s1", stype="swiss", stage="completed", matches=[
        ("p1", "p2", "p1", NOW - timedelta(hours=2)),
        ("p3", "p4", "p3", NOW - timedelta(minutes=20))])

    assert (await mod.draft_state("s1")).last_result == NOW - timedelta(minutes=20)


@pytest.mark.asyncio
async def test_a_draft_with_nothing_reported_has_no_finish_time(test_db):
    await seed_session("s1", stype="random", stage="abandoned")

    assert (await mod.draft_state("s1")).last_result is None


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


# ---- the watchdog pass ------------------------------------------------------

@pytest.mark.asyncio
async def test_a_finished_player_is_asked_once_not_once_per_tick(test_db, dm):
    """The watchdog runs every ten minutes for as long as the cards are out, so
    this stands between a reminder and a pestering."""
    await seed_session("s1", stype="random", stage="pairings",
                       matches=[("p1", "p2", "p1", NOW)])
    await _a_loan()
    due = NOW + REMIND_AFTER_FINISH

    first = await mod.send_due_reminders(now=due)
    second = await mod.send_due_reminders(now=due + timedelta(minutes=10))

    assert first == 1 and second == 0, dm.sent
    assert [uid for uid, _ in dm.sent] == ["p1"]


@pytest.mark.asyncio
async def test_cards_still_out_an_hour_later_are_asked_for_again(test_db, dm):
    await seed_session("s1", stype="random", stage="completed")
    await _a_loan()

    await mod.send_due_reminders(now=NOW)

    assert await mod.send_due_reminders(now=NOW + timedelta(minutes=61)) == 1, dm.sent


@pytest.mark.asyncio
async def test_the_watchdog_waits_an_hour_after_the_draft_is_decided(test_db, dm):
    """Then asks, then asks hourly while the cards stay out -- and not between."""
    await seed_session("s1", stype="swiss", stage="completed",
                       matches=[("p1", "p2", "p1", NOW)])
    await _a_loan()

    asks = [await mod.send_due_reminders(now=NOW + timedelta(minutes=m))
            for m in (10, 50, 60, 70, 110, 120, 130)]

    assert asks == [0, 0, 1, 0, 0, 1, 0], dm.sent


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

    assert await mod.send_due_reminders(now=NOW + REMIND_AFTER_FINISH) == 1, dm.sent


@pytest.mark.asyncio
async def test_a_failed_dm_is_not_recorded_as_sent(test_db, dm):
    """send_dm RETURNS False for a blocked inbox -- it does not raise. A wrapper
    ignoring that return stamps last_reminded_at for a DM that never arrived,
    and the borrower goes unasked for a day."""
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

    async def _return_the_other(bot, user_id, message, label=None):
        if str(user_id) == "p1":
            async with AsyncSessionLocal() as s:
                row = await s.get(CardLoan, second)
                row.state = "returned"
                await s.commit()
        return await original(bot, user_id, message, label)

    import notification_service
    notification_service.send_dm = _return_the_other

    asked = await mod.send_due_reminders(now=NOW)

    assert asked == 1, f"the returned deck was chased anyway: {dm.sent}"
    assert [uid for uid, _ in dm.sent] == ["p1"]
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
