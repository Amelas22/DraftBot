"""A borrower is charged the figure they were quoted, whichever tier they are in.

The figure used to be computed in two places: collateral_for, which is what
/library deck and the reminder DMs tell a player, and start_borrow, which read
library.collateral_tix off the row and is what the wallet actually moves. They
agreed because there was one figure to read.

A per-borrower figure breaks that agreement silently: quote the regular's price
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
import services.library_trust_service as trust

pytestmark = pytest.mark.asyncio

GUILD, LIB, CUBE, SESSION = "g1", "lib", "mycube", "s1"
REGULAR, STRANGER = "u-regular", "u-stranger"
DECK = [{"name": "Swamp", "qty": 4}]


@pytest.fixture
def serve(monkeypatch):
    """A library that charges 50 of a regular and 100 of a stranger, stocked,
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


async def _drafter(player, drafts, funds):
    """Somebody with a deck waiting, `drafts` behind them and `funds` to spend."""
    async with AsyncSessionLocal() as session:
        if await session.get(DraftSession, 1) is None:
            session.add(DraftSession(session_id=SESSION, guild_id=GUILD, cube=CUBE))
        session.add(PlayerStats(player_id=player, guild_id=GUILD,
                                display_name=player, drafts_participated=drafts))
        session.add(CardLoan(guild_id=GUILD, borrower_id=player, library_id=LIB,
                            cards=DECK, state="assigned",
                            source=f"draft:{SESSION}"))
        await session.commit()
    async with db_session() as session:
        await wallet_service.transfer_in(session, GUILD, "system:test", player,
                                         funds, "fund")


async def _held_by_the_library(player):
    """What the collateral wallet is actually holding of this player's money."""
    async with db_session() as session:
        return await wallet_service.balance_in(
            session, GUILD, svc.collateral_holder(GUILD))


@pytest.mark.parametrize("drafts,expected,who", [
    (trust.TRUSTED_DRAFTS, 50, "a regular pays the lower figure"),
    (0, 100, "a stranger pays the higher one"),
])
async def test_the_wallet_moves_exactly_what_the_player_was_quoted(
        test_db, serve, drafts, expected, who):
    await _tiered_library()
    player = REGULAR if drafts else STRANGER
    await _drafter(player, drafts, funds=200)

    quoted = (await svc.hold_shortfall(GUILD, player))["hold"]
    status, _ = await svc.start_borrow(GUILD, player)

    assert status == "dispatched", who
    assert quoted == expected, who
    assert await _held_by_the_library(player) == expected, (
        f"{who}: quoted {quoted}, the library is holding "
        f"{await _held_by_the_library(player)}")


async def test_a_stranger_who_can_only_afford_the_regular_price_is_refused(
        test_db, serve):
    """The failure the quote exists to prevent. 50 tix is enough to borrow at
    the trusted figure and is not enough here, so a mismatch between the two
    numbers would hand this player a deck against money nobody took."""
    await _tiered_library()
    await _drafter(STRANGER, 0, funds=50)

    figures = await svc.hold_shortfall(GUILD, STRANGER)
    status, _ = await svc.start_borrow(GUILD, STRANGER)

    assert status == "short_funds"
    assert (figures["hold"], figures["short"]) == (100, 50)
    assert await _held_by_the_library(STRANGER) == 0, "nothing was taken"
    assert not serve.lent, "and no trade was opened"


async def test_the_refund_returns_what_was_taken_even_if_tenure_changed(
        test_db, serve):
    """The refund is a TARGET of zero, so it gives back whatever is held rather
    than recomputing a figure. That is what makes it safe for a borrower whose
    tenure crosses the threshold while the deck is out -- they drafted the very
    draft they borrowed for -- and it is why no figure is stored on the loan."""
    await _tiered_library()
    await _drafter(STRANGER, 0, funds=200)
    assert (await svc.start_borrow(GUILD, STRANGER))[0] == "dispatched"
    await svc.settle_in_flight()
    assert await _held_by_the_library(STRANGER) == 100

    async with AsyncSessionLocal() as session:
        stats = await session.get(PlayerStats, (STRANGER, GUILD))
        stats.drafts_participated = trust.TRUSTED_DRAFTS + 5
        await session.commit()
    assert await trust.is_trusted(LIB, STRANGER) is True, "now a regular"

    serve.job_id = "return-1"
    assert (await svc.start_return(GUILD, STRANGER))[0] == "dispatched"
    await svc.settle_in_flight()

    assert await _held_by_the_library(STRANGER) == 0, "all 100 came back"
    async with db_session() as session:
        assert await wallet_service.balance_in(session, GUILD, STRANGER) == 200
