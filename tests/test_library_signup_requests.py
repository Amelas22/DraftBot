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
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from conftest import a_library
from database.db_session import AsyncSessionLocal
from models.draft_session import DraftSession
from services import debt_service, wallet_service
import services.library_request_service as svc

pytestmark = pytest.mark.asyncio

ALICE, BOB = "u1", "u2"
CUBE, LIB, GUILD, CHANNEL = "mycube", "lib", "g1", "chan1"


def _draft(requests=None, sign_ups=None):
    return SimpleNamespace(session_id="s1", cube=CUBE,
                           library_requests=requests, sign_ups=sign_ups)


# --- who the hold is held for -----------------------------------------------

async def test_a_requester_still_in_the_queue_is_active():
    draft = _draft(requests=[ALICE], sign_ups={ALICE: "Alice", BOB: "Bob"})
    assert svc.active_requesters(draft) == {ALICE}


async def test_a_requester_who_left_the_queue_is_not():
    """The release mechanism, in one assertion. library_requests still names
    Alice -- nothing cleared it -- and she holds nothing because she is gone."""
    draft = _draft(requests=[ALICE], sign_ups={BOB: "Bob"})
    assert svc.requested_ids(draft) == {ALICE}, "the record is kept"
    assert svc.active_requesters(draft) == set(), "and holds nothing"


async def test_an_id_that_was_never_in_the_queue_is_not_active():
    """A stale id from a retry or a repair holds nothing, because the question
    is asked of sign_ups rather than answered from the column."""
    assert svc.active_requesters(_draft(requests=["ghost"],
                                        sign_ups={ALICE: "Alice"})) == set()


async def test_ids_are_compared_as_strings():
    """sign_ups keys are strings and Discord hands out ints. Comparing them raw
    would make every request inactive the moment it was written."""
    draft = _draft(requests=[1234], sign_ups={"1234": "Alice"})
    assert svc.active_requesters(draft) == {"1234"}


async def test_a_draft_with_no_requests_at_all_reads_as_nobody():
    """The column is NULL on every draft that predates the migration, and on
    every draft nobody asked about -- which is most of them."""
    assert svc.requested_ids(_draft(sign_ups={ALICE: "Alice"})) == set()
    assert svc.active_requesters(_draft(sign_ups={ALICE: "Alice"})) == set()


# --- writing the list -------------------------------------------------------

async def test_adding_a_request_returns_a_fresh_list():
    """A fresh list, not a mutation: SQLAlchemy does not see an in-place change
    to a JSON column, so appending would write nothing and the hold would
    silently not exist."""
    draft = _draft(requests=[BOB])
    added = svc.add_request(draft, ALICE)
    assert added == sorted([BOB, ALICE])
    assert draft.library_requests == [BOB], "the row is left to the caller"


async def test_asking_twice_adds_one_request():
    assert svc.add_request(_draft(requests=[ALICE]), ALICE) == [ALICE]


async def test_dropping_a_request_leaves_the_others():
    assert svc.drop_request(_draft(requests=[ALICE, BOB]), ALICE) == [BOB]


async def test_dropping_a_request_nobody_made_is_not_an_error():
    assert svc.drop_request(_draft(requests=[BOB]), ALICE) == [BOB]


# --- can the shelf promise it? ----------------------------------------------

async def _stock(name="Swamp", qty=4):
    await debt_service.create_card_loan(
        guild_id=wallet_service.library_scope(LIB), lender_id="donor",
        borrower_id=wallet_service.HOUSE_LIBRARY, card_name=name,
        quantity=qty, created_by="test", source_id=f"seed-{name}-{qty}")


def _cubes(mapping):
    async def fetch(cube_id):
        return mapping.get(cube_id)
    return fetch


async def test_a_covered_cube_is_promised(test_db):
    await a_library(LIB, guild=GUILD, cubes=(CUBE,))
    await _stock()

    answer = await svc.coverage(_draft(), LIB,
                                fetch=_cubes({CUBE: [{"name": "Swamp", "qty": 3}]}))

    assert answer == {"ok": True, "short": 0}


async def test_a_cube_the_library_does_not_stock_is_not_a_refusal(test_db):
    """None rather than ok=False, and the difference is the message. A player
    drafting a cube the library never offered should be told that, not told the
    shelf is busy -- the second reads as "try again later", and later will not
    help."""
    await a_library(LIB, guild=GUILD, cubes=())
    await _stock()

    assert await svc.coverage(
        _draft(), LIB, fetch=_cubes({CUBE: [{"name": "Swamp", "qty": 3}]})) is None


async def test_an_unreadable_cube_is_not_a_refusal_either(test_db):
    """CubeCobra being down means we could not ask, not that the answer was no."""
    await a_library(LIB, guild=GUILD, cubes=(CUBE,))
    await _stock()

    assert await svc.coverage(_draft(), LIB, fetch=_cubes({})) is None


async def test_a_cube_this_draft_already_holds_needs_no_asking(test_db):
    """A second player at the same table. The availability a request is measured
    against already has this draft's own hold subtracted, so without this they
    would be told the cube was in use -- by the draft they are sitting at."""
    await a_library(LIB, guild=GUILD, cubes=(CUBE,))
    await _stock("Swamp", 3)
    draft = _draft(requests=[BOB], sign_ups={ALICE: "Alice", BOB: "Bob"})

    answer = await svc.coverage(draft, LIB, fetch=_cubes({}))

    assert answer == {"ok": True, "short": 0}, \
        "answered without even reading the cube"


async def test_a_cube_another_draft_is_holding_is_refused_with_a_figure(test_db):
    """The point of asking at sign-up. One copy on the shelf cannot cover two
    drafts, so the second is told now -- and told HOW short, because "busy"
    with no figure reads as broken and gives the player nothing to judge."""
    await a_library(LIB, guild=GUILD, cubes=(CUBE,))
    await _stock("Swamp", 3)
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(session_id="theirs", guild_id=GUILD, cube=CUBE,
                           session_stage="signups", sign_ups={BOB: "Bob"},
                           library_requests=[BOB],
                           draft_start_time=datetime.now(), teams_start_time=None))
        await s.commit()

    answer = await svc.coverage(_draft(), LIB,
                                fetch=_cubes({CUBE: [{"name": "Swamp", "qty": 3}]}))

    assert answer["ok"] is False
    assert answer["short"] == 3, "copies, not distinct names"


async def test_a_short_shelf_is_refused_even_with_nobody_else_drafting(test_db):
    """Asked against availability, which is holdings when nothing is out. A
    cube the library has never been able to cover is refused the same way."""
    await a_library(LIB, guild=GUILD, cubes=(CUBE,))
    await _stock("Swamp", 1)

    answer = await svc.coverage(_draft(), LIB,
                                fetch=_cubes({CUBE: [{"name": "Swamp", "qty": 3}]}))

    assert (answer["ok"], answer["short"]) == (False, 2)


async def test_no_library_and_no_cube_are_both_nothing_to_promise(test_db):
    assert await svc.coverage(_draft(), None) is None
    assert await svc.coverage(SimpleNamespace(cube=None), LIB) is None


# --- what the player is told ------------------------------------------------
#
# Driven against real rows, because the write is half of what is being tested:
# a request that does not land in library_requests holds nothing, and the
# message would say it did.

import services.card_library_inventory as inv
from cogs.library_commands import LibraryCommands


def _ctx():
    ctx = SimpleNamespace()
    ctx.author = SimpleNamespace(id=1234)
    ctx.guild = SimpleNamespace(id=GUILD)
    ctx.guild_id = GUILD
    ctx.channel_id = CHANNEL
    ctx.defer = AsyncMock()
    ctx.followup = SimpleNamespace(send=AsyncMock())
    return ctx


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
                        _cubes({CUBE: cube_cards if cube_cards is not None
                                else [{"name": "Swamp", "qty": 3}]}))
    import cogs.library_commands as mod
    monkeypatch.setattr(mod, "library_gate", lambda ctx: None)
    cog = LibraryCommands(bot=SimpleNamespace())
    ctx = _ctx()
    await getattr(cog, command).callback(cog, ctx)
    return " ".join(str(c.args[0]) for c in ctx.followup.send.await_args_list
                    if c.args)


async def _requests_on(session_id="s1"):
    async with AsyncSessionLocal() as s:
        return (await s.scalar(select(DraftSession).where(
            DraftSession.session_id == session_id))).library_requests


async def test_a_granted_request_is_written_and_said(test_db, monkeypatch):
    await a_library(LIB, guild=GUILD, cubes=(CUBE,))
    await _stock()
    await _queue()

    said = await _run(monkeypatch, "request")

    assert await _requests_on() == ["1234"]
    assert CUBE in said and "held" in said.lower()


async def test_a_refusal_names_the_shortfall_and_says_to_ask_again(
        test_db, monkeypatch):
    """Both halves matter. The figure is what lets a player decide whether to
    wait; "ask again" is the entire recovery mechanism, and a refusal that does
    not mention it reads as a permanent no."""
    await a_library(LIB, guild=GUILD, cubes=(CUBE,))
    await _stock("Swamp", 1)
    await _queue()

    said = await _run(monkeypatch, "request")

    assert await _requests_on() is None, "nothing is held, so nothing is written"
    assert "2" in said, "how short"
    assert "again" in said.lower()


async def test_a_cube_the_library_does_not_stock_says_so(test_db, monkeypatch):
    await a_library(LIB, guild=GUILD, cubes=())
    await _stock()
    await _queue()

    said = await _run(monkeypatch, "request")

    assert "doesn't stock" in said
    assert await _requests_on() is None


async def test_asking_twice_says_it_is_already_held(test_db, monkeypatch):
    await a_library(LIB, guild=GUILD, cubes=(CUBE,))
    await _stock()
    await _queue(requests=["1234"])

    said = await _run(monkeypatch, "request")

    assert "already" in said.lower()
    assert await _requests_on() == ["1234"]


async def test_somebody_not_in_the_queue_is_told_where_to_run_it(
        test_db, monkeypatch):
    """The command needs a draft to hold the cube FOR, and the draft is the one
    in this channel that this player is in."""
    await a_library(LIB, guild=GUILD, cubes=(CUBE,))
    await _stock()
    await _queue(sign_ups={"9999": "Someone else"})

    said = await _run(monkeypatch, "request")

    assert "signed up" in said and "/library borrow" in said


async def test_a_draft_that_has_already_started_is_sent_to_borrow(
        test_db, monkeypatch):
    """Past sign-up there is nothing left to promise: the packs are dealt and
    /library borrow gives them whatever the shelf can cover."""
    await a_library(LIB, guild=GUILD, cubes=(CUBE,))
    await _stock()
    await _queue(teams_start=datetime.now())

    said = await _run(monkeypatch, "request")

    assert "/library borrow" in said
    assert await _requests_on() is None


async def test_a_second_requester_is_told_they_share_it(test_db, monkeypatch):
    """One copy, one hold, however many ask. Saying so is what stops the second
    player thinking they have reserved a cube of their own."""
    await a_library(LIB, guild=GUILD, cubes=(CUBE,))
    await _stock()
    await _queue(sign_ups={"1234": "Alice", "9999": "Bob"}, requests=["9999"])

    said = await _run(monkeypatch, "request")

    assert "sharing" in said.lower()
    assert await _requests_on() == ["1234", "9999"]


async def test_unrequesting_releases_without_leaving_the_draft(
        test_db, monkeypatch):
    """Leaving the queue already releases the hold, so this exists only for the
    player who wants to keep drafting and hand the shelf back."""
    await a_library(LIB, guild=GUILD, cubes=(CUBE,))
    await _stock()
    await _queue(requests=["1234"])

    said = await _run(monkeypatch, "unrequest")

    assert await _requests_on() == []
    assert "free again" in said


async def test_unrequesting_does_not_free_a_cube_somebody_else_still_wants(
        test_db, monkeypatch):
    await a_library(LIB, guild=GUILD, cubes=(CUBE,))
    await _stock()
    await _queue(sign_ups={"1234": "Alice", "9999": "Bob"},
                 requests=["1234", "9999"])

    said = await _run(monkeypatch, "unrequest")

    assert await _requests_on() == ["9999"]
    assert "free again" not in said


async def test_unrequesting_without_having_asked_says_so(test_db, monkeypatch):
    await a_library(LIB, guild=GUILD, cubes=(CUBE,))
    await _stock()
    await _queue()

    said = await _run(monkeypatch, "unrequest")

    assert "haven't asked" in said
