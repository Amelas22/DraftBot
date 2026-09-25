"""What the library owns, and what of it can actually be lent right now.

Two different questions with two different answers, and conflating them is the
bug this exists to avoid. A donor asks "what does this cube still need?" and
means everything on the shelf. A drafter asks "can I borrow this?" and means
what is not already spoken for -- a cube being drafted right now has its cards
in players' hands, and may not support a second draft at the same time.

Three things are spoken for, and they are phase-disjoint by construction rather
than by arithmetic: a draft holds its WHOLE cube only while it has no loans yet,
and loans only exist once decks are assigned. So the two never overlap and the
totals add rather than needing a union.
"""
import pytest
from datetime import datetime, timedelta

from database.db_session import AsyncSessionLocal
from models.card_loan import CardLoan
from models.draft_session import DraftSession
from models.library_server import LibraryServer
from services import debt_service, wallet_service
import services.card_library_inventory as inv

pytestmark = pytest.mark.asyncio

ALICE, BOB = "u1", "u2"
CUBE = "mycube"
# Every question the inventory answers is asked of a LIBRARY now, so these all
# name the one this file's shelf belongs to. Which library serves which server
# is the subject of test_library_resolution.py.
LIB = "lib"


async def _deposit(owner, name, qty, source):
    """Put cards on the shelf, the way a settled deposit does."""
    await debt_service.create_card_loan(
        guild_id=wallet_service.library_scope(LIB), lender_id=owner,
        borrower_id=wallet_service.HOUSE_LIBRARY, card_name=name,
        quantity=qty, created_by="test", source_id=source)


async def _loan(borrower, cards, state, source="draft:s1", guild="g1"):
    async with AsyncSessionLocal() as s:
        s.add(CardLoan(guild_id=guild, borrower_id=borrower, cards=cards,
                       library_id=LIB, state=state, source=source))
        await s.commit()


async def _draft(session_id="s1", cube=CUBE, minutes_ago=5, stage="pairings"):
    """A draft underway in a server this library serves.

    The binding is what makes it count: a draft in a room drawing on a
    different library takes its cards off that library's shelf, so holding
    this one's cube against it would make a cube undraftable because an
    unrelated community happened to be playing it.
    """
    async with AsyncSessionLocal() as s:
        if await s.get(LibraryServer, "g1") is None:
            s.add(LibraryServer(guild_id="g1", library_id=LIB, bound_by="test"))
        s.add(DraftSession(
            session_id=session_id, guild_id="g1", cube=cube, session_stage=stage,
            draft_start_time=datetime.now() - timedelta(minutes=minutes_ago + 60),
            teams_start_time=datetime.now() - timedelta(minutes=minutes_ago)))
        await s.commit()


def _cubes(mapping):
    """Stand-in for the CubeCobra fetch, so no test reaches the network."""
    async def fetch(cube_id):
        return mapping.get(cube_id)
    return fetch


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

    assert await inv.library_available(LIB, fetch=_cubes({})) == {"Swamp": 4}


async def test_cards_out_on_loan_are_not_available(test_db):
    await _deposit(ALICE, "Swamp", 4, "d1")
    await _loan(BOB, [{"name": "Swamp", "qty": 3}], "borrowed")

    assert await inv.library_available(LIB, fetch=_cubes({})) == {"Swamp": 1}


async def test_an_assigned_deck_is_a_reservation(test_db):
    """The drafter is about to collect it. Treating it as available would let a
    second draft be told it can have cards that are already promised."""
    await _deposit(ALICE, "Swamp", 4, "d1")
    await _loan(BOB, [{"name": "Swamp", "qty": 3}], "assigned")

    assert await inv.library_available(LIB, fetch=_cubes({})) == {"Swamp": 1}


async def test_a_draft_underway_holds_its_WHOLE_cube(test_db):
    """Between teams forming and decks being assigned, nobody knows which cards
    went to whom -- the packs are dealt but the pools are not recorded. So the
    whole cube is at risk, not the part that happens to have been borrowed."""
    await _deposit(ALICE, "Swamp", 4, "d1")
    await _deposit(ALICE, "Island", 2, "d2")
    await _draft()

    available = await inv.library_available(
        LIB,
        fetch=_cubes({CUBE: [{"name": "Swamp", "qty": 3}]}))

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
        fetch=_cubes({CUBE: [{"name": "Swamp", "qty": 3}]}))

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
        fetch=_cubes({CUBE: [{"name": "Swamp", "qty": 3}]}))

    assert available == {"Swamp": 4}


async def test_a_draft_that_has_not_started_holds_nothing(test_db):
    """Anchored on teams forming, not on signups opening. A draft sitting open
    waiting to fill would otherwise block its cube for hours -- and forever if
    it never fills."""
    await _deposit(ALICE, "Swamp", 4, "d1")
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(session_id="s9", guild_id="g1", cube=CUBE,
                           session_stage="teams", draft_start_time=datetime.now(),
                           teams_start_time=None))
        await s.commit()

    available = await inv.library_available(
        LIB,
        fetch=_cubes({CUBE: [{"name": "Swamp", "qty": 3}]}))

    assert available == {"Swamp": 4}


async def test_a_finished_draft_holds_nothing(test_db):
    await _deposit(ALICE, "Swamp", 4, "d1")
    await _draft(stage="completed")

    available = await inv.library_available(
        LIB,
        fetch=_cubes({CUBE: [{"name": "Swamp", "qty": 3}]}))

    assert available == {"Swamp": 4}


async def test_availability_never_reads_negative(test_db):
    """More can be out than the ledger shows held -- a seeded loan, a repair,
    a cube listing a card nobody donated. A negative would subtract from the
    next card in a sum and silently understate the shelf."""
    await _deposit(ALICE, "Swamp", 1, "d1")
    await _loan(BOB, [{"name": "Swamp", "qty": 3}], "borrowed")

    assert await inv.library_available(LIB, fetch=_cubes({})) == {}


async def test_a_cube_that_cannot_be_read_holds_nothing_rather_than_everything(
        test_db):
    """CubeCobra being down must not make the library look empty. Failing
    closed here would refuse every borrow while the shelf is full."""
    await _deposit(ALICE, "Swamp", 4, "d1")
    await _draft()

    assert await inv.library_available(LIB, fetch=_cubes({})) == {"Swamp": 4}

