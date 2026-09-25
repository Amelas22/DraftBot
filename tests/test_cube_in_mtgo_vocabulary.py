"""One doorway where a cube list enters the library.

A CubeCobra list arrives in paper vocabulary. Custody, orders and the ledger
all speak MTGO's, because MTGO is the party that has to recognise a name and it
refuses a whole order over one it does not know. Translating at each consumer
instead left the rule a call-site convention: `_being_drafted` never got it, so
a Universes Beyond card in a drafting cube reserved nothing and stayed lendable
to a second draft at the same time.
"""
import pytest

from services.card_library_inventory import cube_as_the_library_sees_it
from services.card_substitution_service import learn_substitutions

pytestmark = pytest.mark.asyncio

SUBS = [{"direction": "received", "got": "Goben, Gene-Splice Savant",
         "gotCatId": 143978, "qty": 1, "satisfies": "Norman Osborn"}]

CUBE = [{"name": "Lightning Bolt", "qty": 1},
        {"name": "Norman Osborn", "qty": 1},
        {"name": "Cogwork Librarian", "qty": 1}]


def _returns(cards):
    async def _fetch(_cube_id):
        return cards
    return _fetch


async def test_the_cube_arrives_in_the_serves_vocabulary(test_db):
    await learn_substitutions(SUBS, job_id="j1")

    seen = await cube_as_the_library_sees_it("c", fetch=_returns(CUBE))

    assert seen.cards == [{"name": "Lightning Bolt", "qty": 1},
                          {"name": "Goben, Gene-Splice Savant", "qty": 1}]


async def test_cards_mtgo_has_never_had_are_separated_not_dropped_silently(test_db):
    """The depositor is told, in the cube's own names -- those are the ones the
    cube's owner can act on."""
    seen = await cube_as_the_library_sees_it("c", fetch=_returns(CUBE))

    assert seen.not_on_mtgo == ["Cogwork Librarian"]
    assert all(c["name"] != "Cogwork Librarian" for c in seen.cards)


async def test_an_unlearned_card_keeps_its_cube_name(test_db):
    """Nothing has been deposited yet, so nothing is known. The cube passes
    through unchanged rather than guessing at a translation."""
    seen = await cube_as_the_library_sees_it("c", fetch=_returns(CUBE))

    assert {"name": "Norman Osborn", "qty": 1} in seen.cards


async def test_a_cube_that_cannot_be_read_is_reported_as_unreadable(test_db):
    """None and empty mean different things: one is a cube to fix, the other is
    CubeCobra to retry."""
    assert await cube_as_the_library_sees_it("c", fetch=_returns(None)) is None

    empty = await cube_as_the_library_sees_it("c", fetch=_returns([]))
    assert empty is not None and empty.cards == []


async def test_a_drafting_cube_reserves_cards_under_the_name_custody_uses(test_db):
    """The double-lending bug. `spoken_for` is subtracted from holdings keyed on
    MTGO names, so a cube list left in CubeCobra's vocabulary reserved nothing
    for a Universes Beyond card -- it stayed lendable to a second draft while
    the first had it on the table."""
    from conftest import a_library
    from models.draft_session import DraftSession
    from database.db_session import AsyncSessionLocal
    from datetime import datetime
    from services.card_library_inventory import _being_drafted

    await learn_substitutions(SUBS, job_id="j1")
    await a_library("lib", guild="g-draft")
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(session_id="s1", guild_id="g-draft", cube="c",
                           teams_start_time=datetime.now()))
        await s.commit()

    held = await _being_drafted(_returns(CUBE), "lib")

    assert held.get("Goben, Gene-Splice Savant") == 1, held
    assert "Norman Osborn" not in held


async def test_a_drafted_pool_is_named_the_way_custody_is(test_db):
    """The other entrance.

    A card list becomes a library obligation two ways: a cube deposit and a
    drafted pool. Custody is booked under the name that MOVED, so a pool left
    in Draftmancer's vocabulary names a Universes Beyond card the library
    cannot match -- the entitlement reads zero, _cap drops it, and the drafter
    silently loses a card the library is holding for them. Cards MTGO has never
    had come out for the same reason they do on the cube side: an order naming
    one is refused whole.
    """
    from services.draft_deck_assignment import _pool_the_library_can_lend

    await learn_substitutions(SUBS, job_id="j1")
    pool = [{"name": "Lightning Bolt", "qty": 1},
            {"name": "Norman Osborn", "qty": 1},
            {"name": "Cogwork Librarian", "qty": 1}]

    assert await _pool_the_library_can_lend(pool) == [
        {"name": "Lightning Bolt", "qty": 1},
        {"name": "Goben, Gene-Splice Savant", "qty": 1}]
