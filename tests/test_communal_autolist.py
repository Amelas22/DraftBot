"""A communal library lists what it is given.

Cube Night's whole shape is that members bring cubes and everyone plays them.
Making somebody hand-price and hand-list each one puts the operator in the
middle of a thing that is supposed to need no operator -- so a communal
library lists a deposited cube itself.

That is a permission, not a default. Auto-listing means any member can make a
cube borrowable, which is right where the cards are the members' own and a
hole where they are a sponsor's. A LIBRARY is communal or it is not; the
server it serves does not get a say, because a server admin can write their
own config through the bot.

Nothing is priced here any more. What borrowing costs belongs to the library,
so listing a cube adds it to that library's offer on terms somebody already
set -- there is no per-cube number for a deposit to invent.
"""
import pytest

from conftest import a_library
from database.db_session import AsyncSessionLocal
from models.library_cube import LibraryCube
import services.library_service as svc

pytestmark = pytest.mark.asyncio

COMMUNAL, CURATED = "cubenight", "lounge"
CUBE = "someones-cube"


async def _get(library_id):
    async with AsyncSessionLocal() as s:
        from models.library import Library
        return await s.get(Library, library_id)


async def test_a_library_is_curated_unless_it_says_otherwise(test_db):
    """The safe default. A library nobody configured must not let its members
    make cubes borrowable."""
    await a_library(CURATED, guild="g-lounge", kind="rental")

    assert svc.is_communal(await _get(CURATED)) is False


async def test_a_communal_library_says_so(test_db):
    await a_library(COMMUNAL, guild="g-night", kind="communal")

    assert svc.is_communal(await _get(COMMUNAL)) is True


async def test_a_deposit_into_a_communal_library_lists_the_cube(test_db):
    await a_library(COMMUNAL, guild="g-night", kind="communal")

    assert await svc.offer_cube(COMMUNAL, CUBE, "communal:auto") is True
    assert await svc.offers(COMMUNAL, CUBE) is True


async def test_a_cube_already_listed_is_left_exactly_as_it_is(test_db):
    """Depositing into a cube the operator already listed must not rewrite who
    listed it -- the audit trail is the only record of why it is there."""
    await a_library(COMMUNAL, guild="g-night", kind="communal", cubes=(CUBE,))

    assert await svc.offer_cube(COMMUNAL, CUBE, "communal:auto") is False

    async with AsyncSessionLocal() as s:
        row = await s.get(LibraryCube, (COMMUNAL, CUBE))
    assert row.added_by == "test", "the original listing stands"


async def test_two_libraries_list_their_cubes_separately(test_db):
    """The partition. Cube Night listing a cube says nothing about whether the
    Lounge offers it, however much the two share an MTGO account."""
    await a_library(COMMUNAL, guild="g-night", kind="communal")
    await a_library(CURATED, guild="g-lounge", kind="rental")
    await svc.offer_cube(COMMUNAL, CUBE, "communal:auto")

    assert await svc.offers(CURATED, CUBE) is False


async def test_a_cube_the_library_already_covers_is_not_adopted_for_free(
        test_db, monkeypatch):
    """The hole: adoption ran before the deposit was even worked out.

    Topping up is the default, so a cube the library already stocks has
    nothing to hand over -- and the command returned having priced that cube
    free and listed it in the server, contributed nothing. In a communal
    server anyone can run /deposit, so that was a way to make ANY cube the
    shelf happens to cover borrowable for nothing, including one stocked with
    a sponsor's cards.
    """
    import cogs.card_deposit_commands as mod

    await a_library(COMMUNAL, guild="g1", kind="communal")
    listed = []
    monkeypatch.setattr(mod, "offer_cube", _record(listed))
    monkeypatch.setattr(mod, "list_cube_in_guild", lambda g, c: None)

    ctx = _ctx()
    monkeypatch.setattr(mod, "defer_if_usable", _returns(True))
    monkeypatch.setattr(mod, "fetch_cube", _returns([{"name": "Swamp", "qty": 1}]))
    monkeypatch.setattr(mod, "cards_to_deposit", _returns([]))

    await mod.CardDepositCommands(None).deposit.callback(
        mod.CardDepositCommands(None), ctx, "somecube")

    assert listed == [], "nothing was given, so nothing was listed"


def _record(into):
    async def _offer(library_id, cube, added_by):
        into.append((library_id, cube))
        return True
    return _offer


def _returns(value):
    async def _f(*a, **k):
        return value
    return _f


def _ctx():
    from unittest.mock import AsyncMock, MagicMock
    ctx = MagicMock()
    ctx.guild_id = "g1"
    ctx.author.id = "u1"
    ctx.followup.send = AsyncMock()
    return ctx
