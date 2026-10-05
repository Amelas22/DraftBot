"""What the library owns, and what of it can actually be lent right now.

Two different questions with two different answers, and conflating them is the
bug this exists to avoid. A donor asks "what does this cube still need?" and
means everything on the shelf. A drafter asks "can I borrow this?" and means
what is not already spoken for -- a cube being drafted right now has its cards
in players' hands, and may not support a second draft at the same time.

Three things are spoken for, and they are phase-disjoint by construction rather
than by arithmetic. A draft somebody asked the library for holds its whole cube
while it fills; once teams form it keeps holding the cube until it has loans;
from then on its loans speak for it. Every draft is in exactly one of those
phases, so the totals add rather than needing a union -- which is what the
boundary tests at the end of each section are checking.
"""
import pytest
from datetime import datetime, timedelta

from database.db_session import AsyncSessionLocal
from models.card_loan import CardLoan
from models.draft_session import DraftSession
from models.library_server import LibraryServer
from conftest import cube_lists
from services import debt_service, wallet_service
import services.card_library_inventory as inv

pytestmark = pytest.mark.asyncio

ALICE, BOB = "u1", "u2"
CUBE = "mycube"
# Every question the inventory answers is asked of a LIBRARY now, so these all
# name the one this file's shelf belongs to. Which library serves which server
# is the subject of test_library_resolution.py.
LIB = "lib"

# teams_start=None means STILL FILLING, so the default needs its own marker.
_UNSET = object()


async def _deposit(owner, name, qty, source):
    """Put cards on the shelf, the way a settled deposit does."""
    await debt_service.create_card_loan(
        guild_id=wallet_service.library_scope(LIB), lender_id=owner,
        borrower_id=wallet_service.HOUSE_LIBRARY, card_name=name,
        quantity=qty, created_by="test", source_id=source)


async def _loan(borrower, cards, state, source="draft:s1", guild="g1"):
    async with AsyncSessionLocal() as s:
        loan = CardLoan(guild_id=guild, borrower_id=borrower, cards=cards,
                        library_id=LIB, state=state, source=source)
        s.add(loan)
        await s.commit()
        return loan.id


async def _draft(session_id="s1", cube=CUBE, minutes_ago=5, stage="pairings",
                 sign_ups=None, requests=None, teams_start=_UNSET,
                 deletion_time=None, guild="g1"):
    """A draft in a server this library serves.

    By default one UNDERWAY: its teams formed `minutes_ago`, which is what makes
    it hold its cube. `teams_start=None` makes it one still FILLING, which holds
    its cube on the same terms for a different reason -- see _filling, which is
    this with the sign-up arguments named.

    The binding is what makes either count: a draft in a room drawing on a
    different library takes its cards off that library's shelf, so holding this
    one's cube against it would make a cube undraftable because an unrelated
    community happened to be playing it. `guild` is how the not-served case is
    expressed.
    """
    async with AsyncSessionLocal() as s:
        if await s.get(LibraryServer, "g1") is None:
            s.add(LibraryServer(guild_id="g1", library_id=LIB, bound_by="test"))
        s.add(DraftSession(
            session_id=session_id, guild_id=guild, cube=cube, session_stage=stage,
            sign_ups=sign_ups if sign_ups is not None else {ALICE: "Alice", BOB: "Bob"},
            library_requests=requests, deletion_time=deletion_time,
            draft_start_time=datetime.now() - timedelta(minutes=minutes_ago + 60),
            teams_start_time=(datetime.now() - timedelta(minutes=minutes_ago)
                              if teams_start is _UNSET else teams_start)))
        await s.commit()


async def _filling(session_id="s2", stage="signups", requests=None,
                   sign_ups=None, **kw):
    """A draft still taking sign-ups, with somebody asking for library cards.

    `requests` is library_requests -- everybody who has ASKED. `sign_ups` is who
    is still IN the queue. They are separate because the gap between them is the
    release mechanism: a requester missing from sign_ups has left, and the hold
    is simply no longer computed for them.
    """
    await _draft(session_id=session_id, stage=stage, teams_start=None,
                 requests=[ALICE] if requests is None else requests,
                 sign_ups={ALICE: "Alice"} if sign_ups is None else sign_ups,
                 **kw)


# --- what the library owns --------------------------------------------------

async def test_holdings_sum_across_every_donor(test_db):
    """One shelf. Two people donating the same card makes two copies of it,
    and a cube needing two is supported by them jointly."""
    await _deposit(ALICE, "Swamp", 1, "d1")
    await _deposit(BOB, "Swamp", 1, "d2")
    await _deposit(ALICE, "Island", 3, "d3")

    assert await inv.library_holdings(LIB) == {"Swamp": 2, "Island": 3}


async def test_holdings_ignore_loans_against_the_house(test_db):
    """Custody faces house:library; a loan faces house:mtgo. Counting both
    would have a borrowed deck read as stock the library owns."""
    await _deposit(ALICE, "Swamp", 2, "d1")
    await debt_service.create_card_loan(
        guild_id="g1", lender_id=wallet_service.HOUSE_MTGO, borrower_id=BOB,
        card_name="Mountain", quantity=4, created_by="test", source_id="loan-1")

    assert await inv.library_holdings(LIB) == {"Swamp": 2}


async def test_a_withdrawn_card_is_no_longer_held(test_db):
    await _deposit(ALICE, "Swamp", 2, "d1")
    await debt_service.create_card_return(
        guild_id=wallet_service.library_scope(LIB),
        returner_id=wallet_service.HOUSE_LIBRARY, owner_id=ALICE,
        card_name="Swamp", quantity=2, created_by="test", source_id="w1")

    assert await inv.library_holdings(LIB) == {}


# --- what can be lent right now ---------------------------------------------

async def test_nothing_spoken_for_means_everything_is_available(test_db):
    await _deposit(ALICE, "Swamp", 4, "d1")

    assert await inv.library_available(LIB, fetch=cube_lists({})) == {"Swamp": 4}


async def test_cards_out_on_loan_are_not_available(test_db):
    await _deposit(ALICE, "Swamp", 4, "d1")
    await _loan(BOB, [{"name": "Swamp", "qty": 3}], "borrowed")

    assert await inv.library_available(LIB, fetch=cube_lists({})) == {"Swamp": 1}


async def test_an_assigned_deck_is_a_reservation(test_db):
    """The drafter is about to collect it. Treating it as available would let a
    second draft be told it can have cards that are already promised."""
    await _deposit(ALICE, "Swamp", 4, "d1")
    await _loan(BOB, [{"name": "Swamp", "qty": 3}], "assigned")

    assert await inv.library_available(LIB, fetch=cube_lists({})) == {"Swamp": 1}


async def test_a_draft_underway_holds_its_WHOLE_cube(test_db):
    """Between teams forming and decks being assigned, nobody knows which cards
    went to whom -- the packs are dealt but the pools are not recorded. So the
    whole cube is at risk, not the part that happens to have been borrowed."""
    await _deposit(ALICE, "Swamp", 4, "d1")
    await _deposit(ALICE, "Island", 2, "d2")
    await _draft()

    available = await inv.library_available(
        LIB,
        fetch=cube_lists({CUBE: [{"name": "Swamp", "qty": 3}]}))

    assert available == {"Swamp": 1, "Island": 2}


async def test_the_whole_cube_hold_ends_once_decks_are_assigned(test_db):
    """The assignment is the precise answer, so it replaces the blunt one. The
    two never overlap: loans only exist after assignment, which is what lets
    these be added rather than unioned."""
    await _deposit(ALICE, "Swamp", 4, "d1")
    await _draft()
    await _loan(BOB, [{"name": "Swamp", "qty": 1}], "assigned", source="draft:s1")

    available = await inv.library_available(
        LIB,
        fetch=cube_lists({CUBE: [{"name": "Swamp", "qty": 3}]}))

    assert available == {"Swamp": 3}, "the loan, not the cube"


async def test_a_draft_that_never_assigned_releases_its_cube_eventually(test_db):
    """Measured over 562 drafts, teams-formed to decks-assigned is 20 minutes
    median and never exceeded 31. A draft still holding its cube an hour later
    is not drafting; it broke, and holding the cube forever would make it
    undraftable by anyone."""
    await _deposit(ALICE, "Swamp", 4, "d1")
    await _draft(minutes_ago=90)

    available = await inv.library_available(
        LIB,
        fetch=cube_lists({CUBE: [{"name": "Swamp", "qty": 3}]}))

    assert available == {"Swamp": 4}


async def test_a_draft_nobody_asked_about_holds_nothing_while_it_fills(test_db):
    """A draft sitting open blocks nothing by existing. Holding every filling
    draft's cube would block it for hours -- and forever if it never fills --
    so the hold is something a player asks for, and this one nobody did."""
    await _deposit(ALICE, "Swamp", 4, "d1")
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(session_id="s9", guild_id="g1", cube=CUBE,
                           session_stage="teams", draft_start_time=datetime.now(),
                           teams_start_time=None))
        await s.commit()

    available = await inv.library_available(
        LIB,
        fetch=cube_lists({CUBE: [{"name": "Swamp", "qty": 3}]}))

    assert available == {"Swamp": 4}


async def test_a_finished_draft_holds_nothing(test_db):
    await _deposit(ALICE, "Swamp", 4, "d1")
    await _draft(stage="completed")

    available = await inv.library_available(
        LIB,
        fetch=cube_lists({CUBE: [{"name": "Swamp", "qty": 3}]}))

    assert available == {"Swamp": 4}


async def test_availability_never_reads_negative(test_db):
    """More can be out than the ledger shows held -- a seeded loan, a repair,
    a cube listing a card nobody donated. A negative would subtract from the
    next card in a sum and silently understate the shelf."""
    await _deposit(ALICE, "Swamp", 1, "d1")
    await _loan(BOB, [{"name": "Swamp", "qty": 3}], "borrowed")

    assert await inv.library_available(LIB, fetch=cube_lists({})) == {}


async def test_a_cube_that_cannot_be_read_holds_nothing_rather_than_everything(
        test_db):
    """CubeCobra being down must not make the library look empty. Failing
    closed here would refuse every borrow while the shelf is full."""
    await _deposit(ALICE, "Swamp", 4, "d1")
    await _draft()

    assert await inv.library_available(LIB, fetch=cube_lists({})) == {"Swamp": 4}



# --- two drafts of one cube: the earlier reservation wins -------------------
#
# A shelf holding one copy of each card cannot cover two drafts of the same
# cube, so one of them has to be told no. It is the one that reserved second.
# A draft reserves when its teams form, and the decks it assigns carry that
# reservation forward rather than starting a new one -- otherwise a draft that
# finished first would lose its cards to one that was still dealing packs.

async def test_a_later_drafts_whole_cube_hold_does_not_take_an_earlier_drafts_deck(
        test_db):
    """Measured on 2026-09-30: a PowerLSV draft assigned its decks while a
    second PowerLSV draft, whose teams had formed three minutes after the
    first's, was still holding its whole cube. Every card in the first draft's
    decks read 1 held - 1 held = 0, and nobody could collect anything."""
    await _deposit(ALICE, "Swamp", 1, "d1")
    await _draft("first", minutes_ago=20)
    mine = await _loan(BOB, [{"name": "Swamp", "qty": 1}], "assigned",
                       source="draft:first")
    await _draft("second", minutes_ago=15)

    available = await inv.library_available(
        LIB, fetch=cube_lists({CUBE: [{"name": "Swamp", "qty": 1}]}),
        exclude_loan_id=mine)

    assert available == {"Swamp": 1}


async def test_an_earlier_drafts_whole_cube_hold_still_takes_a_later_drafts_deck(
        test_db):
    """The same rule from the other side: a draft still dealing packs reserved
    first, so a deck from a draft that started after it must wait."""
    await _deposit(ALICE, "Swamp", 1, "d1")
    await _draft("first", minutes_ago=20)
    await _draft("second", minutes_ago=15)
    mine = await _loan(BOB, [{"name": "Swamp", "qty": 1}], "assigned",
                       source="draft:second")

    available = await inv.library_available(
        LIB, fetch=cube_lists({CUBE: [{"name": "Swamp", "qty": 1}]}),
        exclude_loan_id=mine)

    assert available == {}


async def test_a_later_drafts_uncollected_deck_does_not_take_an_earlier_drafts_deck(
        test_db):
    """Once the later draft assigns its own decks its hold becomes those decks
    -- still a reservation made second, so it still yields."""
    await _deposit(ALICE, "Swamp", 1, "d1")
    await _draft("first", minutes_ago=20)
    await _draft("second", minutes_ago=15)
    mine = await _loan(BOB, [{"name": "Swamp", "qty": 1}], "assigned",
                       source="draft:first")
    await _loan(ALICE, [{"name": "Swamp", "qty": 1}], "assigned",
                source="draft:second")

    available = await inv.library_available(
        LIB, fetch=cube_lists({}), exclude_loan_id=mine)

    assert available == {"Swamp": 1}


async def test_a_collected_deck_counts_whichever_draft_it_came_from(test_db):
    """Priority settles who may take a card off the shelf, not who has to give
    one back. A card somebody is holding is gone, however late they reserved."""
    await _deposit(ALICE, "Swamp", 1, "d1")
    await _draft("first", minutes_ago=20)
    await _draft("second", minutes_ago=15)
    mine = await _loan(BOB, [{"name": "Swamp", "qty": 1}], "assigned",
                       source="draft:first")
    await _loan(ALICE, [{"name": "Swamp", "qty": 1}], "borrowed",
                source="draft:second")

    available = await inv.library_available(
        LIB, fetch=cube_lists({}), exclude_loan_id=mine)

    assert available == {}


# --- a draft holds the cube only if somebody in it could borrow -------------

async def test_a_draft_nobody_in_it_may_borrow_from_holds_nothing(test_db):
    """In an invite-only library a draft of uninvited players will never
    collect a deck, so reserving the whole cube for it only blocks the people
    who can."""
    from services.library_access_service import invite
    await _deposit(ALICE, "Swamp", 4, "d1")
    await invite(LIB, ALICE, added_by="test")
    await _draft(sign_ups={BOB: "Bob", "u3": "Carol"})

    available = await inv.library_available(
        LIB, fetch=cube_lists({CUBE: [{"name": "Swamp", "qty": 3}]}))

    assert available == {"Swamp": 4}


async def test_one_invited_drafter_is_enough_to_hold_the_cube(test_db):
    """Which cards they will end up with is unknown until decks are assigned,
    so one possible borrower holds the whole cube just as eight would."""
    from services.library_access_service import invite
    await _deposit(ALICE, "Swamp", 4, "d1")
    await invite(LIB, ALICE, added_by="test")
    await _draft(sign_ups={ALICE: "Alice", "u3": "Carol"})

    available = await inv.library_available(
        LIB, fetch=cube_lists({CUBE: [{"name": "Swamp", "qty": 3}]}))

    assert available == {"Swamp": 1}


# --- a draft holds its cube from sign-up, if somebody asked for it ----------
#
# The hold a player asks for with /library request. It exists because the
# alternative answer -- "the shelf cannot cover your deck" -- used to arrive
# after forty-five minutes of drafting, which is after the only point at which
# it was still useful.

async def test_a_requested_draft_holds_its_whole_cube_while_it_fills(test_db):
    """The whole cube, for the reason the underway hold holds the whole cube:
    which cards a requester ends up with is unknowable until the packs are
    dealt, so anything less is a promise the shelf cannot keep."""
    await _deposit(ALICE, "Swamp", 4, "d1")
    await _deposit(ALICE, "Island", 2, "d2")
    await _filling()

    available = await inv.library_available(
        LIB, fetch=cube_lists({CUBE: [{"name": "Swamp", "qty": 3}]}))

    assert available == {"Swamp": 1, "Island": 2}


async def test_one_requester_holds_as_much_as_several(test_db):
    """They draft from the same copy, so the hold is the cube either way. A
    per-player share would promise eight players an eighth of a cube each."""
    await _deposit(ALICE, "Swamp", 4, "d1")
    await _filling(requests=[ALICE, BOB], sign_ups={ALICE: "Alice", BOB: "Bob"})

    available = await inv.library_available(
        LIB, fetch=cube_lists({CUBE: [{"name": "Swamp", "qty": 3}]}))

    assert available == {"Swamp": 1}


@pytest.mark.parametrize("requests,sign_ups,why", [
    ([ALICE], {BOB: "Bob"}, "the last requester left"),
    (["ghost"], {ALICE: "Alice"}, "a stale id that was never in the draft"),
])
async def test_a_hold_nobody_active_is_asking_for_releases(
        test_db, requests, sign_ups, why):
    """Release is DERIVED, not evented. Nothing clears library_requests when a
    player leaves -- they drop out of sign_ups by the ordinary cancel path, the
    intersection empties, and the hold stops being computed. So there is no
    leave path that can be missed, and a stale id holds nothing either."""
    await _deposit(ALICE, "Swamp", 4, "d1")
    await _filling(requests=requests, sign_ups=sign_ups)

    available = await inv.library_available(
        LIB, fetch=cube_lists({CUBE: [{"name": "Swamp", "qty": 3}]}))

    assert available == {"Swamp": 4}, why


async def test_one_requester_leaving_does_not_release_the_other(test_db):
    """The same mechanism from the other side: the hold is for whoever is still
    in the queue, and one of them is enough."""
    await _deposit(ALICE, "Swamp", 4, "d1")
    await _filling(requests=[ALICE, BOB], sign_ups={BOB: "Bob"})

    available = await inv.library_available(
        LIB, fetch=cube_lists({CUBE: [{"name": "Swamp", "qty": 3}]}))

    assert available == {"Swamp": 1}


async def test_the_signup_hold_hands_over_to_the_underway_hold(test_db):
    """The boundary that keeps the two from being counted twice.

    Both hold the whole cube, so a draft counted by both would subtract its
    cube twice -- 4 held less 3 less 3 reads as 0 available, and every borrow
    in the guild is refused while the shelf is full. They are keyed on the same
    fact from opposite sides: this one on teams_start_time being unset, that one
    on it being set.
    """
    await _deposit(ALICE, "Swamp", 4, "d1")
    await _draft(requests=[ALICE], sign_ups={ALICE: "Alice"})

    available = await inv.library_available(
        LIB, fetch=cube_lists({CUBE: [{"name": "Swamp", "qty": 3}]}))

    assert available == {"Swamp": 1}, "held once, by the underway hold"


async def test_a_finished_draft_releases_its_requested_cube(test_db):
    """A draft that was cancelled without its teams ever forming keeps its
    requests forever. Without the stage check it would hold the cube forever
    too, and the queue-filling hold is the one place that can happen."""
    await _deposit(ALICE, "Swamp", 4, "d1")
    await _filling(stage="completed")

    available = await inv.library_available(
        LIB, fetch=cube_lists({CUBE: [{"name": "Swamp", "qty": 3}]}))

    assert available == {"Swamp": 4}


async def test_a_requested_cube_that_cannot_be_read_holds_nothing(test_db):
    """CubeCobra being down must not empty the shelf -- the same call
    _being_drafted makes, for the same reason."""
    await _deposit(ALICE, "Swamp", 4, "d1")
    await _filling()

    assert await inv.library_available(LIB, fetch=cube_lists({})) == {"Swamp": 4}


async def test_a_requester_the_library_would_refuse_holds_nothing(test_db):
    """In an invite-only library an uninvited requester will never collect a
    deck, so holding the cube for them only blocks the people who can."""
    from services.library_access_service import invite
    await _deposit(ALICE, "Swamp", 4, "d1")
    await invite(LIB, ALICE, added_by="test")
    await _filling(requests=[BOB], sign_ups={ALICE: "Alice", BOB: "Bob"})

    available = await inv.library_available(
        LIB, fetch=cube_lists({CUBE: [{"name": "Swamp", "qty": 3}]}))

    assert available == {"Swamp": 4}, "Alice is invited but did not ask"


async def test_a_request_in_a_room_this_library_does_not_serve_holds_nothing(
        test_db):
    """The binding is what makes a hold count, here as everywhere else: a draft
    drawing on another library's shelf must not take cards off this one."""
    await _deposit(ALICE, "Swamp", 4, "d1")
    await _filling(session_id="elsewhere", guild="g-other")

    available = await inv.library_available(
        LIB, fetch=cube_lists({CUBE: [{"name": "Swamp", "qty": 3}]}))

    assert available == {"Swamp": 4}


async def test_a_queue_already_due_for_cancellation_holds_nothing(test_db):
    """The one hold that would otherwise have no end.

    A requester does not leave a dead queue, they stop coming back -- so the
    intersection that releases every other hold never empties here. What ends it
    is the queue's own inactivity clock: every sign-up pushes deletion_time
    back, so a row past it has been quiet for three hours and the next cleanup
    pass deletes it. In a cleanup-exempt guild the row never goes at all, and
    without this the cube would be held for good.
    """
    await _deposit(ALICE, "Swamp", 4, "d1")
    await _filling(deletion_time=datetime.now() - timedelta(minutes=1))

    available = await inv.library_available(
        LIB, fetch=cube_lists({CUBE: [{"name": "Swamp", "qty": 3}]}))

    assert available == {"Swamp": 4}


async def test_a_queue_still_within_its_inactivity_window_holds_its_cube(test_db):
    """The other side of the same clock: a live queue is one somebody has
    signed up to recently, and that is exactly what pushes the clock out."""
    await _deposit(ALICE, "Swamp", 4, "d1")
    await _filling(deletion_time=datetime.now() + timedelta(hours=3))

    available = await inv.library_available(
        LIB, fetch=cube_lists({CUBE: [{"name": "Swamp", "qty": 3}]}))

    assert available == {"Swamp": 1}


async def test_a_borrow_is_not_blocked_by_a_queue_that_has_not_fired(test_db):
    """The priority rule for the two in one sentence: a draft handing out decks
    never waits on a sign-up hold.

    Its players have already drafted, so refusing them is the harm the hold
    exists to prevent, while the queue can be told no and ask again -- which is
    what its refusal says to do, and it hears that while still filling rather
    than after a wasted evening.
    """
    await _deposit(ALICE, "Swamp", 1, "d1")
    await _draft("underway", minutes_ago=5)
    mine = await _loan(BOB, [{"name": "Swamp", "qty": 1}], "assigned",
                       source="draft:underway")
    await _filling("queue")

    asked_for_the_loan = await inv.library_available(
        LIB, fetch=cube_lists({CUBE: [{"name": "Swamp", "qty": 1}]}),
        exclude_loan_id=mine)
    asked_in_general = await inv.library_available(
        LIB, fetch=cube_lists({CUBE: [{"name": "Swamp", "qty": 1}]}))

    assert asked_for_the_loan == {"Swamp": 1}, "the deck can be collected"
    assert asked_in_general == {}, "and the queue still holds it against others"
