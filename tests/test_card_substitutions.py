"""The serve tells us when it moved a card under another name; we remember.

CubeCobra lists a Universes Beyond card under its crossover name and MTGO
trades it under an in-universe one. The library books custody by name, so
unless the pairing is learned, a deposit lands under a name the serve will
refuse to hand back.
"""
import pytest

from services.card_substitution_service import (
    learn_substitutions, mtgo_names_for, to_mtgo_names,
)

pytestmark = pytest.mark.asyncio

# Exactly the shape the serve reported for job 9a9f62d38f9c on 2026-09-25.
REPORTED = [
    {"direction": "received", "got": "Ademi of the Silkchutes", "gotCatId": 143918,
     "qty": 1, "satisfies": "Spectacular Spider-Man"},
    {"direction": "received", "got": "Goben, Gene-Splice Savant", "gotCatId": 143978,
     "qty": 1, "satisfies": "Norman Osborn"},
]


async def test_a_reported_substitution_is_learned(test_db):
    await learn_substitutions(REPORTED, job_id="9a9f62d38f9c")

    assert await mtgo_names_for(["Norman Osborn"]) == {
        "Norman Osborn": "Goben, Gene-Splice Savant"}


async def test_learning_the_same_pair_twice_is_harmless(test_db):
    """Every settler that reads a finished job reports the same substitutions,
    and the watchdog re-reads jobs the poller already saw."""
    await learn_substitutions(REPORTED, job_id="9a9f62d38f9c")
    await learn_substitutions(REPORTED, job_id="9a9f62d38f9c")

    assert len(await mtgo_names_for([s["satisfies"] for s in REPORTED])) == 2


async def test_a_trade_with_no_substitutions_learns_nothing(test_db):
    await learn_substitutions([], job_id="plain")

    assert await mtgo_names_for(["Lightning Bolt"]) == {}


async def test_only_what_the_serve_actually_moved_is_recorded(test_db):
    """An entry missing either half is not a pairing. Recording a half would
    translate a cube name to nothing and drop the card from the order."""
    await learn_substitutions(
        [{"direction": "received", "satisfies": "Norman Osborn"},
         {"direction": "received", "got": "Goben, Gene-Splice Savant"}],
        job_id="malformed")

    assert await mtgo_names_for(["Norman Osborn"]) == {}


async def test_a_cube_list_is_translated_into_the_serves_vocabulary(test_db):
    """The whole point: what goes to the serve, and what custody is compared
    against, speak one language."""
    await learn_substitutions(REPORTED, job_id="9a9f62d38f9c")
    cube = [{"name": "Lightning Bolt", "qty": 1},
            {"name": "Norman Osborn", "qty": 1},
            {"name": "Spectacular Spider-Man", "qty": 2}]

    assert await to_mtgo_names(cube) == [
        {"name": "Lightning Bolt", "qty": 1},
        {"name": "Goben, Gene-Splice Savant", "qty": 1},
        {"name": "Ademi of the Silkchutes", "qty": 2}]


async def test_an_untranslated_cube_is_returned_unchanged(test_db):
    cube = [{"name": "Lightning Bolt", "qty": 1}]

    assert await to_mtgo_names(cube) == cube


async def test_settling_a_trade_learns_what_the_serve_substituted(test_db):
    """Learned where a finished job is READ, not at each settler.

    Two paths settle trades -- a loan's batches and a deposit's job row -- and
    both go through _items_moved. Learning at the call sites instead would be
    two places to keep in step, and the one that drifted would book custody
    under a name the serve cannot be asked for.
    """
    from services.card_lending_service import _items_moved

    job = {"receive": [{"name": "Norman Osborn", "qty": 1}],
           "receivedActual": [{"name": "Goben, Gene-Splice Savant", "qty": 1}],
           "substitutions": REPORTED}

    moved = await _items_moved(job, "card-deposit")

    # Booked under the name the serve actually moved...
    assert moved == [{"name": "Goben, Gene-Splice Savant", "qty": 1}]
    # ...and the pairing is now known, so a cube list can be translated to match.
    assert await mtgo_names_for(["Norman Osborn"]) == {
        "Norman Osborn": "Goben, Gene-Splice Savant"}


# Job 2b592c4534f8 on 2026-10-31. The cube's "Spider-Woman, Stunning Savior" had
# already been learned as MTGO's "Makdee and Itla, Skysnarers", so the order asked
# for Makdee and Itla -- and the depositor held only the other printing, which MTGO
# names Spider-Woman. The serve took it as Makdee and Itla's twin.
FIRST_COPY = [{"direction": "received", "got": "Makdee and Itla, Skysnarers",
               "gotCatId": 144216, "qty": 1, "satisfies": "Spider-Woman, Stunning Savior"}]
TWIN = [{"direction": "received", "got": "Spider-Woman, Stunning Savior",
         "gotCatId": 153547, "qty": 1, "satisfies": "Makdee and Itla, Skysnarers"}]


async def test_a_twin_printing_is_booked_under_the_name_that_was_asked_for(test_db):
    """The cube counts its copies under the translated name. Booking the twin under
    its own name left the second copy on the shelf where no count could see it."""
    from services.card_lending_service import _items_moved
    await learn_substitutions(FIRST_COPY, job_id="912b891fd663")

    moved = await _items_moved(
        {"receive": [{"name": "Makdee and Itla, Skysnarers", "qty": 1}],
         "receivedActual": [{"name": "Spider-Woman, Stunning Savior", "qty": 1}],
         "substitutions": TWIN},
        "card-deposit")

    assert moved == [{"name": "Makdee and Itla, Skysnarers", "qty": 1}]


async def test_a_lent_twin_is_booked_under_the_name_availability_subtracts(test_db):
    """Same on the way out: a borrow filled with the twin must be on loan under the
    name the shelf counts, or the copy reads as still available and is lent twice."""
    from services.card_lending_service import _items_moved
    await learn_substitutions(FIRST_COPY, job_id="912b891fd663")

    moved = await _items_moved(
        {"give": [{"name": "Makdee and Itla, Skysnarers", "qty": 2}],
         "gaveActual": [{"name": "Makdee and Itla, Skysnarers", "qty": 1},
                        {"name": "Spider-Woman, Stunning Savior", "qty": 1}],
         "substitutions": [{**TWIN[0], "direction": "gave"}]},
        "borrow")

    assert moved == [{"name": "Makdee and Itla, Skysnarers", "qty": 2}]


async def test_only_the_copies_a_twin_substitution_covers_are_renamed(test_db):
    """An order can ask for both names. Here one Spider-Woman was asked for as
    itself and one stood in for Makdee and Itla: renaming both would retire Makdee
    and Itla twice and leave the Spider-Woman claim open for good."""
    from services.card_lending_service import _items_moved
    await learn_substitutions(FIRST_COPY, job_id="912b891fd663")

    moved = await _items_moved(
        {"give": [{"name": "Makdee and Itla, Skysnarers", "qty": 1},
                  {"name": "Spider-Woman, Stunning Savior", "qty": 1}],
         "gaveActual": [{"name": "Spider-Woman, Stunning Savior", "qty": 2}],
         "substitutions": [{**TWIN[0], "direction": "gave"}]},
        "card-withdraw")

    assert sorted(moved, key=lambda i: i["name"]) == [
        {"name": "Makdee and Itla, Skysnarers", "qty": 1},
        {"name": "Spider-Woman, Stunning Savior", "qty": 1}]


async def test_a_twin_is_not_learned_as_a_translation(test_db):
    """The asked-for name was already MTGO's, not a cube's. Learning the pair would
    map an MTGO name onto its own twin -- a loop, not a translation."""
    await learn_substitutions(FIRST_COPY, job_id="912b891fd663")

    assert await learn_substitutions(TWIN, job_id="2b592c4534f8") == 0
    assert await mtgo_names_for(["Makdee and Itla, Skysnarers"]) == {}
    assert await mtgo_names_for(["Spider-Woman, Stunning Savior"]) == {
        "Spider-Woman, Stunning Savior": "Makdee and Itla, Skysnarers"}
