"""Saying so when a library cube cannot actually cover a draft right now.

The badge answers "does the library stock this cube", which is stable and is
what somebody picking a cube wants to know. Availability is a different and
much more volatile thing: a draft in progress has its cards in players' hands,
so a cube the library stocks may still not be borrowable for the next twenty
minutes.

Putting the volatility in the badge would make it flicker on and off, which
reads as broken. It goes in a caveat beside the badge instead, so the badge
keeps meaning what a player thinks it means.
"""
import pytest
import services.card_library_inventory as inventory

from database.db_session import AsyncSessionLocal
from conftest import a_library
from cube_views.pack_options import mark_library_cubes

GUILD = "g1"
CUBE = "mycube"


async def _price(collateral=0, cube=CUBE):
    """Bind this server to a library that offers the cube, on these terms.

    The price belongs to the LIBRARY now: a server draws on one library and is
    bound to its terms, so there is no per-cube figure to set.
    """
    await a_library("lib", guild=GUILD, collateral=collateral, cubes=(cube,))


def _shelf(held, available):
    """Stand-ins for the two inventory reads, so no test needs a ledger.

    Both take a library now -- every inventory question is asked of one -- and
    both ignore it: which library is the subject of its own tests, and these
    are about what the badge says once the shelf is known.
    """
    async def _held(_library_id):
        return dict(held)

    async def _avail(_library_id, *_a, **_k):
        return dict(available)
    return _held, _avail


def _cubes(mapping):
    async def fetch(cube_id):
        return mapping.get(cube_id)
    return fetch


async def _mark(options, held, available, cube_cards, monkeypatch):
    import cube_views.pack_options as mod
    h, a = _shelf(held, available)
    monkeypatch.setattr(mod, "library_holdings", h)
    monkeypatch.setattr(mod, "library_available", a)
    monkeypatch.setattr(inventory, "fetch_cube", _cubes(cube_cards))
    return await mark_library_cubes(options, GUILD)


@pytest.mark.asyncio
async def test_a_cube_the_library_can_cover_carries_no_caveat(test_db, monkeypatch):
    await _price()
    marked = await _mark([{"label": CUBE, "value": CUBE}],
                         held={"Swamp": 4}, available={"Swamp": 4},
                         cube_cards={CUBE: [{"name": "Swamp", "qty": 4}]},
                         monkeypatch=monkeypatch)

    assert "free" in marked[0]["description"].lower()
    assert "out" not in marked[0]["description"].lower()


@pytest.mark.asyncio
async def test_a_cube_whose_cards_are_out_says_so(test_db, monkeypatch):
    """The specific thing this exists for: the library stocks it, but a draft
    in progress means it cannot cover another one right now."""
    await _price()
    marked = await _mark([{"label": CUBE, "value": CUBE}],
                         held={"Swamp": 4}, available={"Swamp": 1},
                         cube_cards={CUBE: [{"name": "Swamp", "qty": 4}]},
                         monkeypatch=monkeypatch)

    assert "3" in marked[0]["description"], "how many are missing, not just that some are"
    assert marked[0]["emoji"] == "⚠️", "the warning replaces the badge's emoji"


@pytest.mark.asyncio
async def test_the_badge_still_says_it_is_a_library_cube(test_db, monkeypatch):
    """A cube short today is still a library cube tomorrow. Dropping the badge
    entirely would tell a player to stop considering it."""
    await _price(collateral=5)
    marked = await _mark([{"label": CUBE, "value": CUBE}],
                         held={"Swamp": 4}, available={},
                         cube_cards={CUBE: [{"name": "Swamp", "qty": 4}]},
                         monkeypatch=monkeypatch)

    assert "5" in marked[0]["description"], "the price survives the caveat"


@pytest.mark.asyncio
async def test_a_cube_the_library_does_not_stock_is_marked_differently(
        test_db, monkeypatch):
    """Priced but never deposited is not the same as temporarily lent out, and
    telling somebody to come back later would be wrong."""
    await _price()
    marked = await _mark([{"label": CUBE, "value": CUBE}],
                         held={}, available={},
                         cube_cards={CUBE: [{"name": "Swamp", "qty": 4}]},
                         monkeypatch=monkeypatch)

    assert "stock" in marked[0]["description"].lower()


@pytest.mark.asyncio
async def test_a_cube_that_cannot_be_read_keeps_its_badge(test_db, monkeypatch):
    """CubeCobra being unreachable must not make a library cube look broken."""
    await _price()
    marked = await _mark([{"label": CUBE, "value": CUBE}],
                         held={"Swamp": 4}, available={"Swamp": 4},
                         cube_cards={}, monkeypatch=monkeypatch)

    assert "free" in marked[0]["description"].lower()


@pytest.mark.asyncio
async def test_an_unpriced_cube_is_never_fetched(test_db, monkeypatch):
    """The caveat costs a CubeCobra read per cube, so it is only paid for cubes
    the library can actually lend for -- not for every row in the dropdown."""
    import cube_views.pack_options as mod
    asked = []

    async def fetch(cube_id):
        asked.append(cube_id)
        return [{"name": "Swamp", "qty": 4}]

    h, a = _shelf({"Swamp": 4}, {"Swamp": 4})
    monkeypatch.setattr(mod, "library_holdings", h)
    monkeypatch.setattr(mod, "library_available", a)
    monkeypatch.setattr(inventory, "fetch_cube", fetch)

    await mark_library_cubes([{"label": "other", "value": "other"}], GUILD)

    assert asked == []
