"""What a borrower puts up, and why two of them put up different amounts.

A rental library stocks somebody else's cards, so one figure for everyone has
to be set for the worst borrower it might meet -- which prices the people the
library already trusts as though nobody knew them. Its member list separates the
two: the people somebody chose to trust pay the lower figure, and everybody else
it lends to pays the higher.
"""
import pytest

from conftest import a_library
from database.db_session import AsyncSessionLocal
from models.player import PlayerStats
import services.library_access_service as access
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

async def test_a_member_is_known_whatever_their_draft_count(test_db):
    await a_library(LIB, guild=GUILD)
    await access.invite(LIB, PLAYER, "test")

    assert await trust.is_trusted(LIB, PLAYER) is True


async def test_drafting_a_lot_does_not_make_a_non_member_known(test_db):
    """The draft count lets somebody borrow; it does not lower their hold. That
    is the line between the two figures, so it must not drift into tenure."""
    await a_library(LIB, guild=GUILD)
    await access.invite(LIB, "somebody-else", "test")
    await _drafted(500)

    assert await trust.is_trusted(LIB, PLAYER) is False


# --- what each of them puts up ----------------------------------------------

async def test_an_untiered_library_asks_everyone_the_same(test_db):
    """The default, and what makes the column additive: until somebody sets the
    second figure, every library charges exactly what it charged before."""
    library = await _library(collateral=50, untrusted=None)
    await _drafted(0)

    assert await trust.hold_for(library, PLAYER) == 50


async def test_a_tiered_library_asks_a_non_member_for_more(test_db):
    library = await _library(collateral=50, untrusted=100)
    await _drafted(300)

    assert await trust.hold_for(library, PLAYER) == 100


async def test_a_tiered_library_asks_a_member_for_the_lower_figure(test_db):
    library = await _library(collateral=50, untrusted=100)
    await access.invite(LIB, PLAYER, "test")

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
