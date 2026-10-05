"""Asking the library for a cube at sign-up, and being told then.

The feature exists because of when the old answer arrived. A player signed up,
drafted for forty-five minutes, built a deck, ran /library borrow -- and only
then learned the shelf could not cover it. Nothing earlier had asked the
question, so the answer came after the only point at which it was useful.

Three things are being tested here, and the first two are where the design
actually lives:

* RELEASE IS DERIVED. An active requester is in library_requests AND still in
  sign_ups. Nothing listens for a player leaving; they drop out of sign_ups by
  the ordinary cancel path and the hold stops being computed. So there is no
  leave path that can be missed, and no hold that can be left behind.

* A REFUSAL IS NOT FINAL, and says so. The shelf frees up constantly, the queue
  is still filling, and asking again is the whole recovery mechanism -- which is
  why nothing has to remember that a player was told no.

What the hold does to availability is tested next door, in
test_library_inventory.py.
"""
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import services.card_library_inventory as inv
from cogs.library_commands import LibraryCommands
from conftest import a_library, cube_lists, library_ctx, sent_to_invoker
from database.db_session import AsyncSessionLocal
from models.draft_session import DraftSession
import services.library_request_service as svc

pytestmark = pytest.mark.asyncio

ALICE, BOB = "u1", "u2"
CUBE, LIB, GUILD, CHANNEL = "mycube", "lib", "g1", "chan1"


def _draft(requests=None, sign_ups=None):
    return SimpleNamespace(session_id="s1", cube=CUBE,
                           library_requests=requests, sign_ups=sign_ups)


# --- who the hold is held for -----------------------------------------------
#
# The table is the point: what `library_requests` says on its own never decides
# anything, only its overlap with who is still in the queue does. (These ask
# pure functions and need no loop, but the module-wide asyncio mark applies to
# every test here, and a sync one under it only earns a warning.)

@pytest.mark.parametrize("requests,sign_ups,active,why", [
    ([ALICE], {ALICE: "Alice", BOB: "Bob"}, {ALICE}, "asked and still in"),
    ([ALICE], {BOB: "Bob"}, set(), "asked and left -- the release mechanism"),
    (["ghost"], {ALICE: "Alice"}, set(), "never in the draft: a stale id"),
    ([1234], {"1234": "Alice"}, {"1234"}, "ids compared as strings"),
    (None, {ALICE: "Alice"}, set(), "nobody asked (every pre-migration draft)"),
    ([ALICE, BOB], {ALICE: "Alice"}, {ALICE}, "one left, one stayed"),
])
async def test_only_requesters_still_in_the_queue_hold_the_cube(
        requests, sign_ups, active, why):
    assert svc.active_requesters(_draft(requests, sign_ups)) == active, why


async def test_the_record_of_who_asked_outlives_their_signup():
    """Nothing clears library_requests when a player leaves -- that is what
    makes the release derived rather than evented. The record is kept and
    simply stops counting."""
    draft = _draft(requests=[ALICE], sign_ups={BOB: "Bob"})

    assert svc.requested_ids(draft) == {ALICE}
    assert svc.active_requesters(draft) == set()


# --- writing the list ------------------------------------------------------
#
# Written through the service rather than by hand, so the JSON column's one
# rule is covered where it is enforced: SQLAlchemy does not see an in-place
# change, so a write that appends instead of replacing stores nothing and the
# hold silently does not exist.

async def test_a_request_is_stored_and_answers_who_holds_it(test_db):
    await _queue(requests=[BOB], sign_ups={"1234": "Alice", BOB: "Bob"})
    draft = await DraftSession.get_filling_draft_for_user(CHANNEL, "1234")

    holders = await svc.record_request(draft, "1234")

    assert holders == {"1234", BOB}, "both, and said without a re-read"
    assert await _requests_on() == ["1234", BOB], "stored sorted"


async def test_asking_twice_stores_one_request(test_db):
    await _queue(requests=["1234"])
    draft = await DraftSession.get_filling_draft_for_user(CHANNEL, "1234")

    assert await svc.record_request(draft, "1234") == {"1234"}
    assert await _requests_on() == ["1234"]


async def test_releasing_leaves_the_others_holding_it(test_db):
    await _queue(requests=["1234", BOB], sign_ups={"1234": "Alice", BOB: "Bob"})
    draft = await DraftSession.get_filling_draft_for_user(CHANNEL, "1234")

    assert await svc.record_release(draft, "1234") == {BOB}
    assert await _requests_on() == [BOB]


async def test_releasing_a_request_nobody_made_is_not_an_error(test_db):
    await _queue(requests=[BOB], sign_ups={"1234": "Alice", BOB: "Bob"})
    draft = await DraftSession.get_filling_draft_for_user(CHANNEL, "1234")

    assert await svc.record_release(draft, "1234") == {BOB}


# --- can the shelf promise it? ----------------------------------------------

async def _shelf(swamps=4, offers=(CUBE,)):
    """The library these tests ask about: bound here, lending for CUBE, stocked.

    One call rather than a_library plus a separate booking, because every test
    below wants the same pair and conftest.a_library already books stock the way
    a settled deposit does.
    """
    await a_library(LIB, guild=GUILD, cubes=offers, stock={"Swamp": swamps})


async def test_a_covered_cube_is_promised(test_db):
    await _shelf()

    answer = await svc.coverage(_draft(), LIB,
                                fetch=cube_lists({CUBE: [{"name": "Swamp", "qty": 3}]}))

    assert (answer.ok, answer.cards_short) == (True, 0)


async def test_a_cube_the_library_does_not_stock_is_not_a_refusal(test_db):
    """None rather than ok=False, and the difference is the message. A player
    drafting a cube the library never offered should be told that, not told the
    shelf is busy -- the second reads as "try again later", and later will not
    help."""
    await _shelf(offers=())

    assert await svc.coverage(
        _draft(), LIB, fetch=cube_lists({CUBE: [{"name": "Swamp", "qty": 3}]})) is None


async def test_an_unreadable_cube_is_not_a_refusal_either(test_db):
    """CubeCobra being down means we could not ask, not that the answer was no."""
    await _shelf()

    assert await svc.coverage(_draft(), LIB, fetch=cube_lists({})) is None


async def test_a_cube_this_draft_already_holds_needs_no_asking(test_db):
    """A second player at the same table. The availability a request is measured
    against already has this draft's own hold subtracted, so without this they
    would be told the cube was in use -- by the draft they are sitting at."""
    await _shelf(3)
    draft = _draft(requests=[BOB], sign_ups={ALICE: "Alice", BOB: "Bob"})

    answer = await svc.coverage(draft, LIB, fetch=cube_lists({}))

    assert (answer.ok, answer.cards_short) == (True, 0), \
        "answered without even reading the cube"


async def test_a_cube_another_draft_is_holding_is_refused_with_a_figure(test_db):
    """The point of asking at sign-up. One copy on the shelf cannot cover two
    drafts, so the second is told now -- and told HOW short, because "busy"
    with no figure reads as broken and gives the player nothing to judge."""
    await _shelf(3)
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(session_id="theirs", guild_id=GUILD, cube=CUBE,
                           session_stage="signups", sign_ups={BOB: "Bob"},
                           library_requests=[BOB],
                           draft_start_time=datetime.now(), teams_start_time=None))
        await s.commit()

    answer = await svc.coverage(_draft(), LIB,
                                fetch=cube_lists({CUBE: [{"name": "Swamp", "qty": 3}]}))

    assert answer.ok is False
    assert answer.cards_short == 3, "copies, not distinct names"


async def test_a_short_shelf_is_refused_even_with_nobody_else_drafting(test_db):
    """Asked against availability, which is holdings when nothing is out. A
    cube the library has never been able to cover is refused the same way."""
    await _shelf(1)

    answer = await svc.coverage(_draft(), LIB,
                                fetch=cube_lists({CUBE: [{"name": "Swamp", "qty": 3}]}))

    assert (answer.ok, answer.cards_short) == (False, 2)


async def test_no_library_and_no_cube_are_both_nothing_to_promise(test_db):
    assert await svc.coverage(_draft(), None) is None
    assert await svc.coverage(SimpleNamespace(cube=None), LIB) is None


# --- what the player is told ------------------------------------------------
#
# Driven against real rows, because the write is half of what is being tested:
# a request that does not land in library_requests holds nothing, and the
# message would say it did.

async def _queue(session_id="s1", sign_ups=None, requests=None,
                 channel=CHANNEL, teams_start=None):
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(
            session_id=session_id, guild_id=GUILD, cube=CUBE,
            draft_channel_id=channel, session_stage=None,
            sign_ups=sign_ups if sign_ups is not None else {"1234": "Alice"},
            library_requests=requests,
            draft_start_time=datetime.now(), teams_start_time=teams_start))
        await s.commit()


async def _run(monkeypatch, command, cube_cards=None):
    monkeypatch.setattr(inv, "fetch_cube",
                        cube_lists({CUBE: cube_cards if cube_cards is not None
                                else [{"name": "Swamp", "qty": 3}]}))
    import cogs.library_commands as mod
    monkeypatch.setattr(mod, "library_gate", lambda ctx: None)
    cog = LibraryCommands(bot=SimpleNamespace())
    ctx = library_ctx(guild=GUILD, channel=CHANNEL)
    await getattr(cog, command).callback(cog, ctx)
    return sent_to_invoker(ctx)


async def _requests_on(session_id="s1"):
    async with AsyncSessionLocal() as s:
        return (await s.scalar(select(DraftSession).where(
            DraftSession.session_id == session_id))).library_requests


async def test_a_granted_request_is_written_and_said(test_db, monkeypatch):
    await _shelf()
    await _queue()

    said = await _run(monkeypatch, "request")

    assert await _requests_on() == ["1234"]
    assert CUBE in said and "held" in said.lower()


async def test_a_refusal_names_the_shortfall_and_says_to_ask_again(
        test_db, monkeypatch):
    """Both halves matter. The figure is what lets a player decide whether to
    wait; "ask again" is the entire recovery mechanism, and a refusal that does
    not mention it reads as a permanent no."""
    await _shelf(1)
    await _queue()

    said = await _run(monkeypatch, "request")

    assert await _requests_on() is None, "nothing is held, so nothing is written"
    assert "2" in said, "how short"
    assert "again" in said.lower()


async def test_a_cube_the_library_does_not_stock_says_so(test_db, monkeypatch):
    await _shelf(offers=())
    await _queue()

    said = await _run(monkeypatch, "request")

    assert "doesn't stock" in said
    assert await _requests_on() is None


async def test_asking_twice_says_it_is_already_held(test_db, monkeypatch):
    await _shelf()
    await _queue(requests=["1234"])

    said = await _run(monkeypatch, "request")

    assert "already" in said.lower()
    assert await _requests_on() == ["1234"]


async def test_somebody_not_in_the_queue_is_told_where_to_run_it(
        test_db, monkeypatch):
    """The command needs a draft to hold the cube FOR, and the draft is the one
    in this channel that this player is in."""
    await _shelf()
    await _queue(sign_ups={"9999": "Someone else"})

    said = await _run(monkeypatch, "request")

    assert "signed up" in said and "/library borrow" in said


async def test_a_draft_that_has_already_started_is_sent_to_borrow(
        test_db, monkeypatch):
    """Past sign-up there is nothing left to promise: the packs are dealt and
    /library borrow gives them whatever the shelf can cover."""
    await _shelf()
    await _queue(teams_start=datetime.now())

    said = await _run(monkeypatch, "request")

    assert "/library borrow" in said
    assert await _requests_on() is None


async def test_a_second_requester_is_told_they_share_it(test_db, monkeypatch):
    """One copy, one hold, however many ask. Saying so is what stops the second
    player thinking they have reserved a cube of their own."""
    await _shelf()
    await _queue(sign_ups={"1234": "Alice", "9999": "Bob"}, requests=["9999"])

    said = await _run(monkeypatch, "request")

    assert "sharing" in said.lower()
    assert await _requests_on() == ["1234", "9999"]


async def test_unrequesting_releases_without_leaving_the_draft(
        test_db, monkeypatch):
    """Leaving the queue already releases the hold, so this exists only for the
    player who wants to keep drafting and hand the shelf back."""
    await _shelf()
    await _queue(requests=["1234"])

    said = await _run(monkeypatch, "unrequest")

    assert await _requests_on() == []
    assert "free again" in said


async def test_unrequesting_does_not_free_a_cube_somebody_else_still_wants(
        test_db, monkeypatch):
    await _shelf()
    await _queue(sign_ups={"1234": "Alice", "9999": "Bob"},
                 requests=["1234", "9999"])

    said = await _run(monkeypatch, "unrequest")

    assert await _requests_on() == ["9999"]
    assert "free again" not in said


async def test_unrequesting_without_having_asked_says_so(test_db, monkeypatch):
    await _shelf()
    await _queue()

    said = await _run(monkeypatch, "unrequest")

    assert "haven't asked" in said
