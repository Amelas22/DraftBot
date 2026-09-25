"""Topping up the copy already in the library, rather than adding another.

Somebody who owns two copies of a cube deposits one, then the cube is updated
and they want the library's copy brought up to date. Offering the whole list
would take a SECOND copy of everything, because they physically have it -- the
binder cannot act as the filter when the cards really are there.

So the bot has to do the filtering: deposit what the library is SHORT, which
for an updated cube is exactly the cards that were added to it.
"""
import pytest

from database.db_session import AsyncSessionLocal
from services import debt_service, wallet_service
from cogs.library_commands import cards_to_deposit

OWNER = "u1"
LIB = "lib"


async def _library_holds(cards):
    for c in cards:
        await debt_service.create_card_loan(
            guild_id=wallet_service.library_scope(LIB), lender_id=OWNER,
            borrower_id=wallet_service.HOUSE_LIBRARY, card_name=c["name"],
            quantity=c["qty"], created_by="test", source_id=f"d:{c['name']}")


CUBE = [{"name": "Swamp", "qty": 1}, {"name": "Island", "qty": 1},
        {"name": "Mountain", "qty": 1}]


@pytest.mark.asyncio
async def test_a_full_copy_is_the_whole_list(test_db):
    """Opted into: offers the cube as it stands, whatever the library already
    has. Somebody adding a second copy means it, so they have to say so."""
    await _library_holds(CUBE)

    assert await cards_to_deposit(CUBE, LIB, full_copy=True) == CUBE


@pytest.mark.asyncio
async def test_topping_up_offers_only_what_was_added(test_db):
    """The reported case. One copy is already in; the cube gained a card; only
    that card should cross."""
    await _library_holds(CUBE)
    updated = CUBE + [{"name": "Forest", "qty": 1}]

    assert await cards_to_deposit(updated, LIB) == [
        {"name": "Forest", "qty": 1}]


@pytest.mark.asyncio
async def test_topping_up_an_empty_library_offers_everything(test_db):
    """A cube the library has none of is short all of it, so the two modes
    agree -- which is what makes topping up the safe default: seeding a new
    cube needs no flag and behaves identically."""
    assert await cards_to_deposit(CUBE, LIB) == CUBE


@pytest.mark.asyncio
async def test_topping_up_a_complete_copy_offers_nothing(test_db):
    """Nothing to do is not an error, and must not open an empty trade."""
    await _library_holds(CUBE)

    assert await cards_to_deposit(CUBE, LIB) == []


@pytest.mark.asyncio
async def test_a_card_held_from_another_cube_counts(test_db):
    """The library is an inventory, not a shelf per cube. One Lightning Bolt
    serves whichever cube needs a Bolt, so topping up must not ask for a second
    one just because this cube did not put it there."""
    await _library_holds([{"name": "Lightning Bolt", "qty": 1}])

    assert await cards_to_deposit([{"name": "Lightning Bolt", "qty": 1}, {"name": "Swamp", "qty": 1}], LIB) == [{"name": "Swamp", "qty": 1}]


@pytest.mark.asyncio
async def test_quantities_are_topped_up_not_replaced(test_db):
    """A cube running three of a card, with one in the library, needs two."""
    await _library_holds([{"name": "Swamp", "qty": 1}])

    assert await cards_to_deposit([{"name": "Swamp", "qty": 3}], LIB) == [
        {"name": "Swamp", "qty": 2}]


# --- enough to field this cube alongside others -----------------------------

@pytest.mark.asyncio
async def test_two_copies_ignores_what_another_cube_already_supplied(test_db):
    """Cube A and cube B share a card. With one on the shelf they cannot both
    fire, because whichever drafts first takes it. Asking for two copies tops
    the shelf up to a count that serves both."""
    await _library_holds([{"name": "Lightning Bolt", "qty": 1}])

    assert await cards_to_deposit([{"name": "Lightning Bolt", "qty": 1}], LIB, copies=2) == [
        {"name": "Lightning Bolt", "qty": 1}]


@pytest.mark.asyncio
async def test_asking_for_copies_is_idempotent(test_db):
    """The property a boolean could not give. Several people deposit toward
    one target: each gives what they own, the target does not move, and nobody
    has to coordinate who is covering what."""
    await _library_holds([{"name": "Swamp", "qty": 2}])

    assert await cards_to_deposit([{"name": "Swamp", "qty": 1}], LIB, copies=2) == []


@pytest.mark.asyncio
async def test_a_partial_donation_leaves_the_rest_for_somebody_else(test_db):
    """One donor covers some of the target; the next is asked for exactly the
    remainder rather than for the whole thing again."""
    await _library_holds([{"name": "Swamp", "qty": 3}])

    assert await cards_to_deposit([{"name": "Swamp", "qty": 2}], LIB, copies=3) == [
        {"name": "Swamp", "qty": 3}]


@pytest.mark.asyncio
async def test_one_copy_is_the_default_and_unchanged(test_db):
    await _library_holds(CUBE)

    assert await cards_to_deposit(CUBE, LIB) == []
    assert await cards_to_deposit(CUBE, LIB, copies=1) == []


@pytest.mark.asyncio
async def test_a_full_copy_ignores_the_target_entirely(test_db):
    """Still there for somebody who just wants to hand a cube over without
    the bot deciding what is needed."""
    await _library_holds(CUBE)

    assert await cards_to_deposit(CUBE, LIB, full_copy=True) == CUBE
