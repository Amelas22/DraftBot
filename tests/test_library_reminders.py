"""Telling a borrower their deck is ready, and asking for it back.

A whitelisted library says nothing on the shared board (see
test_signup_library_notice), so a DM is now the ONLY way a borrower learns a
deck is waiting. The asking-back half is driven by the lending watchdog, which
polls every ten minutes -- so "have I already asked" has to be recorded, or
every tick asks again.
"""
import re
from datetime import datetime, timedelta

import pytest

from conftest import seed_session
from services.library_reminders import (REPEAT_AFTER, deck_ready_message,
                                        reminder_due, return_message,
                                        session_of)

NOW = datetime(2026, 9, 26, 12, 0, 0)


def _loan(state="borrowed", last_reminded_at=None, source="draft:s1",
          cards=None):
    from types import SimpleNamespace
    return SimpleNamespace(
        id=1, borrower_id="p1", guild_id="g1", state=state, source=source,
        last_reminded_at=last_reminded_at,
        cards=cards if cards is not None else [{"name": "Swamp", "qty": 4}])


# ---- which draft a loan belongs to -----------------------------------------

def test_a_draft_loan_names_its_session():
    assert session_of(_loan(source="draft:abc-123")) == "abc-123"


def test_a_loan_from_no_draft_names_nothing():
    """Fixture loans exist for testing the library itself and belong to no
    draft, so neither trigger can apply to them."""
    assert session_of(_loan(source="fixture:someone")) is None
    assert session_of(_loan(source=None)) is None


# ---- when to ask for cards back --------------------------------------------

def test_a_player_who_has_finished_playing_is_asked():
    assert reminder_due(_loan(), done_playing=True, draft_over=False, now=NOW)


def test_a_player_still_mid_draft_is_not_asked():
    """The cards are how they play their remaining matches."""
    assert not reminder_due(_loan(), done_playing=False, draft_over=False, now=NOW)


def test_the_draft_ending_asks_everyone_still_holding():
    """The other arm of the trigger, and the only one that applies to a format
    whose pairings are not all known up front."""
    assert reminder_due(_loan(), done_playing=False, draft_over=True, now=NOW)


def test_somebody_already_asked_is_not_asked_again_immediately():
    """The watchdog polls every ten minutes. Without this it would ask every
    ten minutes."""
    loan = _loan(last_reminded_at=NOW - timedelta(hours=1))

    assert not reminder_due(loan, done_playing=True, draft_over=True, now=NOW)


def test_somebody_still_holding_a_day_later_is_asked_again():
    loan = _loan(last_reminded_at=NOW - REPEAT_AFTER - timedelta(minutes=1))

    assert reminder_due(loan, done_playing=True, draft_over=True, now=NOW)


def test_the_repeat_boundary_is_not_early():
    """Exactly at the interval is not yet past it -- otherwise a tick landing
    on the boundary double-asks."""
    loan = _loan(last_reminded_at=NOW - REPEAT_AFTER)

    assert not reminder_due(loan, done_playing=True, draft_over=True, now=NOW)


def test_a_deck_merely_on_offer_is_never_asked_for():
    """Nothing to return: an assigned loan is an offer they have not collected,
    and expire_stale_assignments retracts those on its own."""
    assert not reminder_due(_loan(state="assigned"), done_playing=True,
                            draft_over=True, now=NOW)


def test_a_returned_loan_is_never_asked_for():
    assert not reminder_due(_loan(state="returned"), done_playing=True,
                            draft_over=True, now=NOW)


def test_a_loan_belonging_to_no_draft_is_never_asked_for():
    """No draft means neither trigger can ever be true for it, so the watchdog
    must not decide it is overdue just because it is old."""
    assert not reminder_due(_loan(source="fixture:x"), done_playing=False,
                            draft_over=False, now=NOW)


# ---- what the DMs say -------------------------------------------------------

def test_the_ready_dm_says_how_to_collect():
    """The board no longer says anything, so this DM carries the whole signal:
    a deck exists, and this is the command that fetches it."""
    msg = deck_ready_message([{"name": "Swamp", "qty": 4}], collateral=0)

    assert "/library borrow" in msg


def test_the_ready_dm_names_the_deposit_when_there_is_one():
    """What it costs decides whether they can take it, so it cannot be a
    surprise discovered at /library borrow."""
    msg = deck_ready_message([{"name": "Swamp", "qty": 4}], collateral=25)

    assert "25" in msg


def test_the_ready_dm_does_not_invent_a_deposit_when_there_is_none():
    msg = deck_ready_message([{"name": "Swamp", "qty": 4}], collateral=0)

    assert "deposit" not in msg.lower(), msg


def test_the_return_dm_says_how_to_return():
    assert "/library return" in return_message(_loan())


def test_the_return_dm_does_not_quote_a_deposit_figure():
    """It used to name the library's CURRENT price, which is not necessarily
    what this borrower put up -- a library that changed its terms mid-loan
    promised the wrong refund. /library return states the real figure at the
    moment it hands it back, where it cannot be stale."""
    msg = return_message(_loan())

    assert "deposit" in msg.lower(), msg
    assert not re.search(r"\d+ tix", msg), f"quoted a figure it cannot vouch for: {msg}"

# ---- who has finished playing (against the real tables) --------------------

@pytest.mark.asyncio
async def test_a_player_with_every_match_reported_has_finished(test_db):
    """A team draft creates all three rounds up front, so "no unreported match
    left" genuinely means they are done."""
    from services.library_reminders import players_done_playing

    await seed_session("s1", stype="random", stage="pairings", matches=[
        ("p1", "p2", "p1", NOW), ("p1", "p3", "p1", NOW), ("p1", "p4", "p4", NOW),
        ("p5", "p2", None, None)])

    assert "p1" in await players_done_playing("s1")


@pytest.mark.asyncio
async def test_a_player_with_a_match_outstanding_has_not(test_db):
    from services.library_reminders import players_done_playing

    await seed_session("s1", stype="random", stage="pairings", matches=[
        ("p1", "p2", "p1", NOW), ("p1", "p3", None, None)])

    assert "p1" not in await players_done_playing("s1")


@pytest.mark.asyncio
async def test_a_drawn_match_counts_as_played(test_db):
    """A draw is reported but has no winner. Keying on the winner would read it
    as still outstanding and never let that player finish -- which is why this
    keys on result_submitted_at instead of MatchResult.winner_id."""
    from services.library_reminders import players_done_playing

    await seed_session("s1", stype="random", stage="pairings",
                       matches=[("p1", "p2", None, NOW)])

    assert "p1" in await players_done_playing("s1")


@pytest.mark.asyncio
async def test_a_swiss_draft_never_reports_anybody_as_finished(test_db):
    """Swiss pairs one round at a time, so a player who has reported every
    match that EXISTS may still have rounds to come. Asking for their deck back
    then would take the cards they need. Swiss waits for the draft to end."""
    from services.library_reminders import players_done_playing

    await seed_session("s1", stype="swiss", stage="pairings",
                       matches=[("p1", "p2", "p1", NOW)])

    assert await players_done_playing("s1") == set()


@pytest.mark.asyncio
async def test_a_finished_draft_is_over(test_db):
    from services.library_reminders import draft_is_over

    await seed_session("s1", stype="random", stage="completed")

    assert await draft_is_over("s1") is True


@pytest.mark.asyncio
async def test_an_abandoned_draft_is_over_too(test_db):
    """Cards must come back from a draft that fell apart, not only one that
    finished -- an abandoned draft is exactly when nobody thinks to return."""
    from services.library_reminders import draft_is_over

    await seed_session("s1", stype="random", stage="abandoned")

    assert await draft_is_over("s1") is True


@pytest.mark.asyncio
async def test_a_draft_still_running_is_not_over(test_db):
    from services.library_reminders import draft_is_over

    await seed_session("s1", stype="random", stage="pairings")

    assert await draft_is_over("s1") is False


# ---- the watchdog pass ------------------------------------------------------

async def _a_borrowed_deck(borrower="p1", session="s1", state="borrowed",
                           last_reminded_at=None, collateral=0):
    from database.db_session import AsyncSessionLocal as SessionLocal
    from models.card_loan import CardLoan

    async with SessionLocal() as s:
        loan = CardLoan(guild_id="g", library_id="lib", borrower_id=borrower,
                        cards=[{"name": "Swamp", "qty": 4}], state=state,
                        source=f"draft:{session}",
                        last_reminded_at=last_reminded_at)
        s.add(loan)
        await s.commit()
        return loan.id


def _with_a_client(monkeypatch):
    """Stand a bot up for the registry. Tests run with none registered, and
    send_due_reminders correctly does nothing then -- which would make every
    assertion below pass for the wrong reason."""
    import services.library_reminders as mod

    monkeypatch.setattr(mod, "_client", lambda: object())


class _Recorder:
    """Stands in for notification_service.send_dm and remembers who it told."""

    def __init__(self):
        self.sent = []

    async def __call__(self, bot, user_id, message, label=None):
        self.sent.append((str(user_id), message))
        return True      # send_dm's contract: True delivered, False not


@pytest.mark.asyncio
async def test_a_finished_player_is_asked_once_not_once_per_tick(test_db, monkeypatch):
    """The watchdog runs every ten minutes for as long as the cards are out, so
    this is the test that stands between a reminder and a pestering."""
    import notification_service
    from services.library_reminders import send_due_reminders

    await seed_session("s1", stype="random", stage="pairings",
                       matches=[("p1", "p2", "p1", NOW)])
    await _a_borrowed_deck()
    recorder = _Recorder()
    monkeypatch.setattr(notification_service, "send_dm", recorder)
    _with_a_client(monkeypatch)

    first = await send_due_reminders(now=NOW)
    second = await send_due_reminders(now=NOW + timedelta(minutes=10))

    assert first == 1, "the first pass should have asked once"
    assert second == 0, f"the second pass asked again: {recorder.sent}"
    assert [uid for uid, _ in recorder.sent] == ["p1"]


@pytest.mark.asyncio
async def test_cards_still_out_a_day_later_are_asked_for_again(test_db, monkeypatch):
    import notification_service
    from services.library_reminders import send_due_reminders

    await seed_session("s1", stype="random", stage="completed")
    await _a_borrowed_deck()
    recorder = _Recorder()
    monkeypatch.setattr(notification_service, "send_dm", recorder)
    _with_a_client(monkeypatch)

    await send_due_reminders(now=NOW)
    again = await send_due_reminders(now=NOW + timedelta(hours=25))

    assert again == 1, f"a day-old loan was not chased: {recorder.sent}"


@pytest.mark.asyncio
async def test_a_player_mid_draft_is_left_alone(test_db, monkeypatch):
    import notification_service
    from services.library_reminders import send_due_reminders

    await seed_session("s1", stype="random", stage="pairings",
                       matches=[("p1", "p2", "p1", NOW), ("p1", "p3", None, None)])
    await _a_borrowed_deck()
    recorder = _Recorder()
    monkeypatch.setattr(notification_service, "send_dm", recorder)
    _with_a_client(monkeypatch)

    assert await send_due_reminders(now=NOW) == 0, recorder.sent


@pytest.mark.asyncio
async def test_a_deck_never_collected_is_not_chased(test_db, monkeypatch):
    """An assigned loan is an offer, not a loan out. expire_stale_assignments
    retracts those; chasing somebody for cards they never received is worse
    than saying nothing."""
    import notification_service
    from services.library_reminders import send_due_reminders

    await seed_session("s1", stype="random", stage="completed")
    await _a_borrowed_deck(state="assigned")
    recorder = _Recorder()
    monkeypatch.setattr(notification_service, "send_dm", recorder)
    _with_a_client(monkeypatch)

    assert await send_due_reminders(now=NOW) == 0, recorder.sent


@pytest.mark.asyncio
async def test_a_failed_dm_is_not_recorded_as_sent(test_db, monkeypatch):
    """send_dm RETURNS False for a blocked inbox, an HTTP error or a bad id --
    it does not raise, by documented contract. A wrapper that ignores the
    return stamps last_reminded_at for a DM that never arrived, and the
    borrower is then not asked again for a day. Mocking an exception here
    tested a failure mode the real function cannot produce.
    """
    import notification_service
    from services.library_reminders import send_due_reminders

    await seed_session("s1", stype="random", stage="completed")
    loan_id = await _a_borrowed_deck()

    async def _undeliverable(*a, **k):
        return False

    monkeypatch.setattr(notification_service, "send_dm", _undeliverable)
    _with_a_client(monkeypatch)

    assert await send_due_reminders(now=NOW) == 0

    from database.db_session import AsyncSessionLocal as SessionLocal
    from models.card_loan import CardLoan
    async with SessionLocal() as s:
        assert (await s.get(CardLoan, loan_id)).last_reminded_at is None


@pytest.mark.asyncio
async def test_a_played_out_draft_is_over_even_at_the_pairings_stage(test_db):
    """The stage column is not a reliable finish line: 2220 of 3283 finished
    drafts in production never advanced past 'pairings', and are identifiable
    only by a posted victory message (see helpers.stale_drafts.is_finished_draft).

    Trusting the stage alone would miss two thirds of finished drafts -- and for
    Swiss, where the draft ending is the ONLY return trigger, those borrowers
    would never be asked for their cards back at all.
    """
    from services.library_reminders import draft_is_over

    await seed_session("s1", stype="random", stage="pairings", victory=12345)

    assert await draft_is_over("s1") is True


@pytest.mark.asyncio
async def test_a_swiss_draft_that_was_played_out_still_gets_chased(test_db, monkeypatch):
    """The case the stage-only check silently dropped, end to end: Swiss has no
    per-player trigger, so if the draft never looks over nobody is ever asked."""
    import notification_service
    from services.library_reminders import send_due_reminders

    await seed_session("s1", stype="swiss", stage="pairings", victory=999,
                       matches=[("p1", "p2", "p1", NOW)])
    await _a_borrowed_deck()
    recorder = _Recorder()
    monkeypatch.setattr(notification_service, "send_dm", recorder)
    _with_a_client(monkeypatch)

    assert await send_due_reminders(now=NOW) == 1, recorder.sent


@pytest.mark.asyncio
async def test_a_deck_returned_mid_scan_is_not_chased(test_db, monkeypatch):
    """The scan reads the loans, then sends DMs one at a time. A borrower can
    hand their deck back while an earlier DM is still in flight, and asking
    them afterwards -- possibly promising a refund they have already had --
    reads as the library losing track."""
    import notification_service
    import services.library_reminders as mod
    from database.db_session import AsyncSessionLocal as SessionLocal
    from models.card_loan import CardLoan

    await seed_session("s1", stype="random", stage="completed")
    loan_id = await _a_borrowed_deck()
    recorder = _Recorder()
    _with_a_client(monkeypatch)

    async def _return_it_first(*a, **k):
        async with SessionLocal() as s:
            row = await s.get(CardLoan, loan_id)
            row.state = "returned"
            await s.commit()
        return await recorder(*a, **k)

    # Hand the deck back at the moment the scan decides to send.
    monkeypatch.setattr(notification_service, "send_dm", _return_it_first)
    monkeypatch.setattr(mod, "_still_out", _returned_already(loan_id))

    assert await mod.send_due_reminders(now=NOW) == 0, recorder.sent


def _returned_already(loan_id):
    """Stands in for the freshness check: the loan moved on before dispatch."""
    async def _check(loan):
        return False
    return _check


# ---- announcing a ready deck, and retrying when that fails -----------------

async def _an_assigned_deck(borrower="p1", session="s1", state="assigned",
                            ready_dm_at=None):
    from database.db_session import AsyncSessionLocal as SessionLocal
    from models.card_loan import CardLoan

    async with SessionLocal() as s:
        loan = CardLoan(guild_id="g", library_id="lib", borrower_id=borrower,
                        cards=[{"name": "Swamp", "qty": 4}], state=state,
                        source=f"draft:{session}", ready_dm_at=ready_dm_at)
        s.add(loan)
        await s.commit()
        return loan.id


async def _ready_stamp(loan_id):
    from database.db_session import AsyncSessionLocal as SessionLocal
    from models.card_loan import CardLoan

    async with SessionLocal() as s:
        return (await s.get(CardLoan, loan_id)).ready_dm_at


@pytest.mark.asyncio
async def test_a_deck_nobody_has_been_told_about_is_announced(test_db, monkeypatch):
    """The retry that closes the gap: the loan is committed before the DM is
    attempted, and every later assignment pass skips a borrower who already has
    a loan -- so without this sweep a Discord blip meant they were never told."""
    import notification_service
    import services.library_reminders as mod

    loan_id = await _an_assigned_deck()
    recorder = _Recorder()
    monkeypatch.setattr(notification_service, "send_dm", recorder)
    _with_a_client(monkeypatch)
    monkeypatch.setattr(mod, "_may_collect", _always(True))

    assert await mod.announce_ready_decks(now=NOW) == 1, recorder.sent
    assert await _ready_stamp(loan_id) == NOW


@pytest.mark.asyncio
async def test_a_borrower_already_told_is_not_told_again(test_db, monkeypatch):
    import notification_service
    import services.library_reminders as mod

    await _an_assigned_deck(ready_dm_at=NOW - timedelta(days=3))
    recorder = _Recorder()
    monkeypatch.setattr(notification_service, "send_dm", recorder)
    _with_a_client(monkeypatch)
    monkeypatch.setattr(mod, "_may_collect", _always(True))

    assert await mod.announce_ready_decks(now=NOW) == 0, recorder.sent


@pytest.mark.asyncio
async def test_a_borrower_who_already_has_the_cards_is_not_told(test_db, monkeypatch):
    """They collected it without needing the DM. Telling them their deck is
    ready to borrow, when it is already in their account, is noise."""
    import notification_service
    import services.library_reminders as mod

    await _an_assigned_deck(state="borrowed")
    recorder = _Recorder()
    monkeypatch.setattr(notification_service, "send_dm", recorder)
    _with_a_client(monkeypatch)
    monkeypatch.setattr(mod, "_may_collect", _always(True))

    assert await mod.announce_ready_decks(now=NOW) == 0, recorder.sent


@pytest.mark.asyncio
async def test_a_deck_nobody_needs_any_more_is_not_announced(test_db, monkeypatch):
    """expire_stale_assignments retracts an offer once its draft is over.
    Announcing it then invites somebody to run a command that has nothing to
    hand them."""
    import notification_service
    import services.library_reminders as mod

    await _an_assigned_deck(state="expired")
    recorder = _Recorder()
    monkeypatch.setattr(notification_service, "send_dm", recorder)
    _with_a_client(monkeypatch)
    monkeypatch.setattr(mod, "_may_collect", _always(True))

    assert await mod.announce_ready_decks(now=NOW) == 0, recorder.sent


@pytest.mark.asyncio
async def test_an_uninvited_borrower_is_not_announced_to_and_not_stamped(
        test_db, monkeypatch):
    """Not stamped, because nothing was sent: if they are added to the library's
    list later, the sweep should still tell them."""
    import notification_service
    import services.library_reminders as mod

    loan_id = await _an_assigned_deck()
    recorder = _Recorder()
    monkeypatch.setattr(notification_service, "send_dm", recorder)
    _with_a_client(monkeypatch)
    monkeypatch.setattr(mod, "_may_collect", _always(False))

    assert await mod.announce_ready_decks(now=NOW) == 0, recorder.sent
    assert await _ready_stamp(loan_id) is None


@pytest.mark.asyncio
async def test_an_undelivered_announcement_is_retried_next_tick(test_db, monkeypatch):
    """The whole point: send_dm returning False must leave the loan unstamped."""
    import notification_service
    import services.library_reminders as mod

    loan_id = await _an_assigned_deck()
    _with_a_client(monkeypatch)
    monkeypatch.setattr(mod, "_may_collect", _always(True))

    async def _undeliverable(*a, **k):
        return False

    monkeypatch.setattr(notification_service, "send_dm", _undeliverable)
    assert await mod.announce_ready_decks(now=NOW) == 0
    assert await _ready_stamp(loan_id) is None

    recorder = _Recorder()
    monkeypatch.setattr(notification_service, "send_dm", recorder)
    assert await mod.announce_ready_decks(now=NOW) == 1, "the retry never came"
    assert await _ready_stamp(loan_id) == NOW


def _always(answer):
    async def _check(*a, **k):
        return answer
    return _check
