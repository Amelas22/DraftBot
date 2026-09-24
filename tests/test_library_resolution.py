"""Which library a server draws on, and on whose terms.

The partition is the LIBRARY, not the server. Somebody who deposits into Cube
Night lends to Cube Night's members wherever they play and to nobody else, so
every question about price, access and stock is asked of a library -- and a
server's only role is to say which one.

Absence is a refusal throughout. A server nobody bound has no library, and a
library nobody priced is not a free one: reading either as "lend for nothing"
is how a room that was never configured starts giving away somebody's cards.
"""
import pytest

from database.db_session import AsyncSessionLocal
from models.library import Library
from models.library_cube import LibraryCube
from models.library_server import LibraryServer
import services.library_service as svc

pytestmark = pytest.mark.asyncio

CUBE_NIGHT, LOUNGE = "cubenight", "lotuslounge"
SERVER_A, SERVER_B, UNBOUND = "g-a", "g-b", "g-none"


async def _library(library_id=LOUNGE, kind="rental", collateral=100):
    async with AsyncSessionLocal() as s:
        s.add(Library(id=library_id, name=library_id, kind=kind,
                      collateral_tix=collateral, created_by="test"))
        await s.commit()


async def _bind(guild, library_id):
    async with AsyncSessionLocal() as s:
        s.add(LibraryServer(guild_id=guild, library_id=library_id, bound_by="test"))
        await s.commit()


async def _offer(library_id, *cubes):
    async with AsyncSessionLocal() as s:
        for cube in cubes:
            s.add(LibraryCube(library_id=library_id, cube_id=cube, added_by="test"))
        await s.commit()


async def test_an_unbound_server_has_no_library(test_db):
    await _library()

    assert await svc.library_for(UNBOUND) is None
    assert await svc.library_id_for(UNBOUND) is None


async def test_a_bound_server_resolves_to_its_library(test_db):
    await _library()
    await _bind(SERVER_A, LOUNGE)

    assert (await svc.library_for(SERVER_A)).id == LOUNGE
    assert await svc.library_id_for(SERVER_A) == LOUNGE


async def test_one_library_serves_several_servers(test_db):
    """The direction that matters: a server draws on one library, but a
    library lends into as many rooms as it likes. That is what makes a
    contribution shared regardless of which server somebody plays in."""
    await _library()
    await _bind(SERVER_A, LOUNGE)
    await _bind(SERVER_B, LOUNGE)

    assert await svc.library_id_for(SERVER_A) == LOUNGE
    assert await svc.library_id_for(SERVER_B) == LOUNGE


async def test_a_binding_to_a_library_that_is_gone_lends_nothing(test_db):
    """A configuration fault, not an answer: the server looks set up and
    behaves as though it is not, so it must not fall through to lending."""
    await _bind(SERVER_A, "deleted-library")

    assert await svc.library_for(SERVER_A) is None


# --- terms ------------------------------------------------------------------

async def test_no_library_has_no_price_rather_than_a_free_one(test_db):
    assert svc.price_of(None) is None


async def test_a_library_that_charges_nothing_says_so(test_db):
    """Zero is a price somebody chose, and has to survive as one -- otherwise
    a communal library is indistinguishable from an unconfigured server."""
    await _library(CUBE_NIGHT, kind="communal", collateral=0)
    await _bind(SERVER_A, CUBE_NIGHT)

    assert svc.price_of(await svc.library_for(SERVER_A)) == 0


async def test_a_bound_server_reports_its_librarys_deposit(test_db):
    """The number belongs to the library, not to the cube or the server, so
    every cube it offers is offered on these terms."""
    await _library()
    await _bind(SERVER_A, LOUNGE)

    assert svc.price_of(await svc.library_for(SERVER_A)) == 100


async def test_only_a_communal_library_lists_what_it_is_given(test_db):
    await _library(CUBE_NIGHT, kind="communal")
    await _library(LOUNGE, kind="rental")

    assert svc.is_communal(await _get(CUBE_NIGHT)) is True
    assert svc.is_communal(await _get(LOUNGE)) is False
    assert svc.is_communal(None) is False, "an unconfigured server is not communal"


# --- which cubes ------------------------------------------------------------

async def test_a_cube_the_library_never_listed_is_not_offered(test_db):
    await _library()
    await _offer(LOUNGE, "powerlsv")

    assert await svc.offers(LOUNGE, "powerlsv") is True
    assert await svc.offers(LOUNGE, "somebody-elses-cube") is False


async def test_two_libraries_do_not_share_their_cube_lists(test_db):
    """The partition again: Cube Night offering a cube says nothing about
    whether the Lounge does."""
    await _library(CUBE_NIGHT, kind="communal")
    await _library(LOUNGE)
    await _offer(CUBE_NIGHT, "budget")

    assert await svc.offers(CUBE_NIGHT, "budget") is True
    assert await svc.offers(LOUNGE, "budget") is False


async def test_pricing_a_dropdown_names_only_the_offered_cubes(test_db):
    await _library()
    await _bind(SERVER_A, LOUNGE)
    await _offer(LOUNGE, "powerlsv", "vintage")

    prices = await svc.prices_for(["powerlsv", "vintage", "unrelated"], SERVER_A)

    assert prices == {"powerlsv": 100, "vintage": 100}
    assert "unrelated" not in prices, "left alone rather than marked free"


async def test_an_unbound_server_prices_nothing(test_db):
    await _library()
    await _offer(LOUNGE, "powerlsv")

    assert await svc.prices_for(["powerlsv"], UNBOUND) == {}


async def test_listing_a_cube_twice_is_not_an_error(test_db):
    await _library()

    assert await svc.offer_cube(LOUNGE, "powerlsv", "op") is True
    assert await svc.offer_cube(LOUNGE, "powerlsv", "op") is False


async def _get(library_id):
    async with AsyncSessionLocal() as s:
        return await s.get(Library, library_id)
