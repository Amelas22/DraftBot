"""A borrower is charged the figure they were quoted, whichever tier they are in.

The figure used to be computed in two places: collateral_for, which is what
/library deck and the reminder DMs tell a player, and start_borrow, which read
library.collateral_tix off the row and is what the wallet actually moves. They
agreed because there was one figure to read.

A per-borrower figure breaks that agreement silently: quote the member's price
and take the stranger's, or the reverse, and the only sign is a wallet balance
nobody is watching. So both now come through collateral_for, and these tests
pin the two against each other rather than either on its own.
"""
import pytest

from conftest import FakeLendingServe, a_library
from database.db_session import AsyncSessionLocal, db_session
from models.card_loan import CardLoan
from models.draft_session import DraftSession
from models.player import PlayerStats
from services import wallet_service
import services.card_lending_service as svc
import services.library_access_service as access

pytestmark = pytest.mark.asyncio

GUILD, LIB, CUBE, SESSION = "g1", "lib", "mycube", "s1"
MEMBER, STRANGER = "u-member", "u-stranger"
DECK = [{"name": "Swamp", "qty": 4}]


@pytest.fixture
def serve(monkeypatch):
    """A library that charges 50 of a member and 100 of anybody else, stocked,
    with a serve that accepts every trade."""
    client = FakeLendingServe({"Swamp": 99}, job={"state": "done"})
    monkeypatch.setattr(svc, "get_lending_client", lambda: client)
    monkeypatch.setattr(svc, "is_money_server", lambda _: True)

    async def handle(_):
        return "Borrower01"
    monkeypatch.setattr(svc, "_mtgo_handle", handle)
    return client


async def _tiered_library():
    await a_library(LIB, guild=GUILD, kind="rental", collateral=50,
                    collateral_untrusted=100, cubes=(CUBE,),
                    stock={"Swamp": 99})


async def _drafter(player, member, funds):
    """Somebody with a deck waiting, on the member list or not, and `funds` to
    spend. Every drafter here has drafted plenty; the list is what differs."""
    async with AsyncSessionLocal() as session:
        if await session.get(DraftSession, 1) is None:
            session.add(DraftSession(session_id=SESSION, guild_id=GUILD, cube=CUBE))
        session.add(PlayerStats(player_id=player, guild_id=GUILD,
                                display_name=player, drafts_participated=50))
        session.add(CardLoan(guild_id=GUILD, borrower_id=player, library_id=LIB,
                            cards=DECK, state="assigned",
                            source=f"draft:{SESSION}"))
        await session.commit()
    if member:
        await access.invite(LIB, player, "test")
    async with db_session() as session:
        await wallet_service.transfer_in(session, GUILD, "system:test", player,
                                         funds, "fund")


async def _held_by_the_library(player):
    """What the collateral wallet is actually holding of this player's money."""
    async with db_session() as session:
        return await wallet_service.balance_in(
            session, GUILD, svc.collateral_holder(GUILD))


@pytest.mark.parametrize("member,expected,who", [
    (True, 50, "a member pays the lower figure"),
    (False, 100, "anybody else pays the higher one"),
])
async def test_the_wallet_moves_exactly_what_the_player_was_quoted(
        test_db, serve, member, expected, who):
    await _tiered_library()
    player = MEMBER if member else STRANGER
    await _drafter(player, member, funds=200)

    quoted = (await svc.hold_shortfall(GUILD, player))["hold"]
    status, _ = await svc.start_borrow(GUILD, player)

    assert status == "dispatched", who
    assert quoted == expected, who
    assert await _held_by_the_library(player) == expected, (
        f"{who}: quoted {quoted}, the library is holding "
        f"{await _held_by_the_library(player)}")


async def test_a_stranger_who_can_only_afford_the_member_price_is_refused(
        test_db, serve):
    """The failure the quote exists to prevent. 50 tix is enough to borrow at
    the member figure and is not enough here, so a mismatch between the two
    numbers would hand this player a deck against money nobody took."""
    await _tiered_library()
    await _drafter(STRANGER, False, funds=50)

    figures = await svc.hold_shortfall(GUILD, STRANGER)
    status, _ = await svc.start_borrow(GUILD, STRANGER)

    assert status == "short_funds"
    assert (figures["hold"], figures["short"]) == (100, 50)
    assert await _held_by_the_library(STRANGER) == 0, "nothing was taken"
    assert not serve.lent, "and no trade was opened"


async def test_the_refund_returns_what_was_taken_even_if_membership_changed(
        test_db, serve):
    """The refund is a TARGET of zero, so it gives back whatever is held rather
    than recomputing a figure. That is what makes it safe for a borrower who is
    invited while the deck is out, and it is why no figure is stored on the
    loan."""
    await _tiered_library()
    await _drafter(STRANGER, False, funds=200)
    assert (await svc.start_borrow(GUILD, STRANGER))[0] == "dispatched"
    await svc.settle_in_flight()
    assert await _held_by_the_library(STRANGER) == 100

    await access.invite(LIB, STRANGER, "test")

    serve.job_id = "return-1"
    assert (await svc.start_return(GUILD, STRANGER))[0] == "dispatched"
    await svc.settle_in_flight()

    assert await _held_by_the_library(STRANGER) == 0, "all 100 came back"
    async with db_session() as session:
        assert await wallet_service.balance_in(session, GUILD, STRANGER) == 200


async def test_a_hold_that_cannot_be_worked_out_never_lends_for_free(
        test_db, serve, monkeypatch):
    """No figure is not a figure of zero: the deck stays on the shelf."""
    from unittest.mock import AsyncMock
    await _tiered_library()
    await _drafter(STRANGER, False, funds=200)
    monkeypatch.setattr(svc, "hold_for", AsyncMock(return_value=None))

    status, _ = await svc.start_borrow(GUILD, STRANGER)

    assert status == "unavailable"
    assert not serve.lent, "no trade was opened"
    assert await _held_by_the_library(STRANGER) == 0
