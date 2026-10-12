"""What a borrower puts up, and why two of them put up different amounts.

A rental library stocks somebody else's cards, so one figure for everyone has
to be set for the worst borrower it might meet -- which prices a player who has
drafted here for years as though nobody knew them. Tenure separates the two: how
many drafts this player has taken part in where the library lends.

Measured on 2026-10-08 in the Lounge, the only rental library in production: of
the nine players invited to borrow, eight have 21 to 386 drafts and the ninth
has never played a rated draft there. So the threshold is a line through an
empty gap, not a tuned number -- which is why these tests pin the BEHAVIOUR at
the boundary and the untiered default, and not the figure 10.
"""
import pytest

from conftest import a_library
from database.db_session import AsyncSessionLocal
from models.library_server import LibraryServer
from models.player import PlayerStats
import services.library_trust_service as trust

pytestmark = pytest.mark.asyncio

LIB, GUILD, PLAYER = "lib", "g1", "u1"


async def _drafted(n, player=PLAYER, guild=GUILD):
    """A player with `n` drafts behind them in this server."""
    async with AsyncSessionLocal() as session:
        session.add(PlayerStats(player_id=player, guild_id=guild,
                                display_name=player, drafts_participated=n))
        await session.commit()


# --- who the library knows --------------------------------------------------

@pytest.mark.parametrize("drafts,trusted,why", [
    (trust.TRUSTED_DRAFTS, True, "exactly enough is enough"),
    (trust.TRUSTED_DRAFTS - 1, False, "one short is one short"),
    (0, False, "a row that says they have played nothing"),
    (500, True, "a fixture of the place"),
])
async def test_tenure_decides_who_the_library_knows(test_db, drafts, trusted, why):
    await a_library(LIB, guild=GUILD)
    await _drafted(drafts)

    assert await trust.is_trusted(LIB, PLAYER) is trusted, why


async def test_somebody_with_no_history_here_is_a_stranger(test_db):
    """The case the tier exists for, and it is a real one: the ninth player
    invited to the Lounge library has no PlayerStats row at all. No row and a
    row saying zero must read the same, or a stranger would be trusted for
    want of a record."""
    await a_library(LIB, guild=GUILD)

    assert await trust.drafts_played(LIB, PLAYER) == 0
    assert await trust.is_trusted(LIB, PLAYER) is False


async def test_tenure_is_summed_across_the_librarys_servers(test_db):
    """A library can lend into several rooms, and somebody established in any
    of them is established as far as it is concerned -- the same scoping every
    other question in this feature uses."""
    await a_library(LIB, guild=GUILD)
    async with AsyncSessionLocal() as session:
        session.add(LibraryServer(guild_id="g2", library_id=LIB, bound_by="test"))
        await session.commit()
    await _drafted(6, guild=GUILD)
    await _drafted(6, guild="g2")

    assert await trust.drafts_played(LIB, PLAYER) == 12
    assert await trust.is_trusted(LIB, PLAYER) is True


async def test_drafts_in_a_room_this_library_does_not_serve_do_not_count(test_db):
    """Tenure somewhere else is somebody else's reason to trust them."""
    await a_library(LIB, guild=GUILD)
    await _drafted(500, guild="g-elsewhere")

    assert await trust.drafts_played(LIB, PLAYER) == 0


# --- what each of them puts up ----------------------------------------------

async def test_an_untiered_library_asks_everyone_the_same(test_db):
    """The default, and what makes the column additive: until somebody sets the
    second figure, every library charges exactly what it charged before."""
    library = await _library(collateral=50, untrusted=None)
    await _drafted(0)

    assert await trust.hold_for(library, PLAYER) == 50


async def test_a_tiered_library_asks_a_stranger_for_more(test_db):
    library = await _library(collateral=50, untrusted=100)
    await _drafted(0)

    assert await trust.hold_for(library, PLAYER) == 100


async def test_a_tiered_library_asks_a_regular_for_the_lower_figure(test_db):
    library = await _library(collateral=50, untrusted=100)
    await _drafted(trust.TRUSTED_DRAFTS)

    assert await trust.hold_for(library, PLAYER) == 50


async def test_no_library_is_a_refusal_rather_than_a_free_borrow(test_db):
    """None, not 0 -- the distinction price_of already makes. Zero is a price
    somebody chose; an unconfigured server must not read as free."""
    assert await trust.hold_for(None, PLAYER) is None


async def _library(*, collateral, untrusted):
    from services.library_service import library_for
    await a_library(LIB, guild=GUILD, kind="rental", collateral=collateral,
                    collateral_untrusted=untrusted)
    return await library_for(GUILD)


# --- what a whole room is told ----------------------------------------------
#
# The signup board and the cube dropdown are each ONE message for everybody
# reading, so neither can know which tier applies to the reader -- the same
# reason neither can address only the people who may borrow. They must not name
# the lower figure alone: that reads as a promise to exactly the borrowers about
# to be asked for twice it.

def _shelf(monkeypatch, cards):
    """Fix what the shelf holds and what the cube lists.

    Same shape as test_signup_library_notice's: these tests are about the
    FIGURE the board names once coverage is established, not about how coverage
    is counted.
    """
    import cube_views.pack_options as mod
    import services.card_library_inventory as inventory

    async def _held(_library_id):
        return {c["name"]: c["qty"] for c in cards}

    async def _avail(_library_id, *_a, **_k):
        return {c["name"]: c["qty"] for c in cards}

    async def _fetch(_cube_id):
        return cards
    monkeypatch.setattr(mod, "library_holdings", _held)
    monkeypatch.setattr(mod, "library_available", _avail)
    monkeypatch.setattr(inventory, "fetch_cube", _fetch)


async def test_the_signup_board_names_both_figures(test_db, monkeypatch):
    from cube_views.pack_options import library_signup_note

    await a_library(LIB, guild=GUILD, kind="rental", collateral=50,
                    collateral_untrusted=100, cubes=("mycube",))
    _shelf(monkeypatch, [{"name": "Swamp", "qty": 4}])

    note = await library_signup_note("mycube", GUILD)

    assert "50" in note and "100" in note, note
    assert "refunded" in note, "the part that decides whether somebody can play"


async def test_an_untiered_board_still_names_one_figure(test_db, monkeypatch):
    """The common case must not start reading like a range."""
    from cube_views.pack_options import library_signup_note

    await a_library(LIB, guild=GUILD, kind="rental", collateral=50,
                    cubes=("mycube",))
    _shelf(monkeypatch, [{"name": "Swamp", "qty": 4}])

    note = await library_signup_note("mycube", GUILD)

    assert "**50 tix**" in note and "–" not in note, note


async def test_the_cube_dropdown_quotes_the_higher_figure(test_db):
    """One short description per cube and no room for a range, so it names the
    most anyone might be asked. Understating it would surprise the borrowers
    who can least afford the surprise."""
    from services.library_service import prices_for

    await a_library(LIB, guild=GUILD, kind="rental", collateral=50,
                    collateral_untrusted=100, cubes=("mycube",))

    assert await prices_for(["mycube"], GUILD) == {"mycube": 100}
