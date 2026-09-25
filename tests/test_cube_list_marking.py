"""Showing, in the cube list, what borrowing from a cube costs.

A drafter has to know whether they can afford to borrow BEFORE they join,
which means the price belongs where cubes are chosen rather than somewhere they
would have to go looking. A cube the library cannot lend for is left exactly as
it was: most cubes have nothing to do with the library, and decorating them all
would make the marked ones invisible.
"""
import pytest
import services.card_library_inventory as inventory

from database.db_session import AsyncSessionLocal
from conftest import a_library
from cube_views.pack_options import mark_library_cubes

GUILD = "g1"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    _MADE.clear()
    """Nothing here is about CubeCobra, so nothing here talks to it.

    These tests used to make real HTTP requests for every cube they named. The
    failure path sets `cards = None`, so offline they waited out the 30-second
    timeout and then passed anyway -- with the shortfall branch silently never
    run. A test that goes green whether or not it reached the network is not
    testing the network, and is not testing the branch either.

    The availability figures are stubbed empty on purpose: this file is about
    the PRICE badge. The coverage caveat has its own file, with its own
    fixtures, where the numbers are the point.
    """
    import cube_views.pack_options as mod

    async def nothing(*a, **k):
        return {}
    monkeypatch.setattr(mod, "library_holdings", nothing)
    monkeypatch.setattr(mod, "library_available", nothing)

    async def no_cube(_cube):
        return None
    monkeypatch.setattr(inventory, "fetch_cube", no_cube)


_MADE: "set[str]" = set()


async def _price(cube, collateral, guild=GUILD):
    """Bind this server to a library that offers the cube, on these terms.

    The price belongs to the LIBRARY now: a server draws on one library and is
    bound to its terms, so there is no per-cube figure to set.
    """
    from database.db_session import AsyncSessionLocal
    from models.library_cube import LibraryCube
    library = f"lib-{guild}"
    if library in _MADE:
        # A second cube in the same server joins the library already bound to
        # it; making another would leave the server pointing at only one.
        async with AsyncSessionLocal() as s:
            s.add(LibraryCube(library_id=library, cube_id=cube, added_by="test"))
            await s.commit()
        return
    _MADE.add(library)
    await a_library(library, guild=guild, collateral=collateral, cubes=(cube,))


@pytest.mark.asyncio
async def test_a_free_cube_says_so(test_db):
    await _price("BryanBudgetCube", 0)

    marked = await mark_library_cubes(
        [{"label": "BryanBudgetCube", "value": "BryanBudgetCube"}], GUILD)

    assert "free" in marked[0]["description"].lower()
    assert marked[0]["label"] == "BryanBudgetCube", "the name itself is untouched"
    assert marked[0]["emoji"]


@pytest.mark.asyncio
async def test_a_priced_cube_names_the_price(test_db):
    """The number, not just that there is one -- "costs tix" leaves a player
    guessing whether they can afford it, which is the question."""
    await _price("PowerLSV", 25)

    marked = await mark_library_cubes(
        [{"label": "PowerLSV", "value": "PowerLSV"}], GUILD)

    assert "25" in marked[0]["description"]


@pytest.mark.asyncio
async def test_a_cube_the_library_cannot_lend_for_is_left_alone(test_db):
    """Most cubes have nothing to do with the library. Marking them all would
    make the marked ones invisible."""
    original = {"label": "SomeCube", "value": "SomeCube"}

    marked = await mark_library_cubes([dict(original)], GUILD)

    assert marked[0] == original


@pytest.mark.asyncio
async def test_a_price_in_another_server_does_not_mark_it_here(test_db):
    await _price("PowerLSV", 25, guild="g2")

    marked = await mark_library_cubes(
        [{"label": "PowerLSV", "value": "PowerLSV"}], GUILD)

    assert "description" not in marked[0]


@pytest.mark.asyncio
async def test_an_existing_description_is_not_thrown_away(test_db):
    """A guild may already describe its own cubes; the library's note is extra
    information about that cube, not a replacement for what it says."""
    await _price("PowerLSV", 5)

    marked = await mark_library_cubes(
        [{"label": "PowerLSV", "value": "PowerLSV",
          "description": "Powered, 540 cards"}], GUILD)

    assert "Powered, 540 cards" in marked[0]["description"]
    assert "5" in marked[0]["description"]


@pytest.mark.asyncio
async def test_the_note_fits_in_a_discord_description(test_db):
    """Discord caps a SelectOption description at 100 characters and REJECTS a
    longer one, so a wordy guild description plus the library note must not
    take the whole dropdown down with it."""
    await _price("PowerLSV", 5)

    marked = await mark_library_cubes(
        [{"label": "PowerLSV", "value": "PowerLSV", "description": "x" * 95}],
        GUILD)

    description = marked[0]["description"]
    assert len(description) <= 100
    # And the half that survives is the PRICE. A plain truncation cut from the
    # right, which is where the note sits, so a server with a wordy cube
    # description kept its prose and lost the number the note exists to show.
    assert "5" in description, f"the price was truncated away: {description!r}"


@pytest.mark.asyncio
async def test_the_original_list_is_not_mutated(test_db):
    """These dicts come from the guild config, which is cached and shared."""
    await _price("PowerLSV", 5)
    options = [{"label": "PowerLSV", "value": "PowerLSV"}]

    await mark_library_cubes(options, GUILD)

    assert options == [{"label": "PowerLSV", "value": "PowerLSV"}]
