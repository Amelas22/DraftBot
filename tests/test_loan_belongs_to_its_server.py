"""A deck is collected in the server it was drafted in, and nowhere else.

The one-active-loan rule went global -- the library is a single MTGO account,
so a deck out in one server is out everywhere -- and active_loan correctly
stopped filtering by guild. Dispatch did not follow: it priced the borrow,
held the deposit and charged the fee in the server the COMMAND was run in,
while settlement reads loan.guild_id throughout. Run /borrow somewhere else
and the deposit is held in one server's ledger and released in another's, so
nothing ever gives it back.

The same mismatch bypasses the invite list, which the cog checks against the
command's server: an assigned Lotus Lounge deck could be collected from an
open server that happens to price the same cube.
"""
import pytest

from database.db_session import AsyncSessionLocal, db_session
from models.card_loan import CardLoan
from models.draft_session import DraftSession
from models.library_server import LibraryServer
from services import wallet_service
import services.card_lending_service as svc

pytestmark = pytest.mark.asyncio

HOME, ELSEWHERE, BORROWER = "g-home", "g-else", "u1"
LIBRARY = "lib"


@pytest.fixture
def serve(monkeypatch):
    from conftest import FakeLendingServe
    client = FakeLendingServe()
    monkeypatch.setattr(svc, "get_lending_client", lambda: client)

    async def handle(_):
        return "Borrower01"
    monkeypatch.setattr(svc, "_mtgo_handle", handle)
    monkeypatch.setattr(svc, "is_money_server", lambda gid: True)
    return client


async def _assigned_in(guild, collateral=100):
    """One library serving both servers, and a deck drafted in one of them.

    Both rooms draw on the SAME library on purpose: the refusal under test has
    to be about where the deck was drafted, not about the two servers happening
    to have different stock.
    """
    from conftest import a_library
    await a_library(LIBRARY, guild=HOME, kind="rental", collateral=collateral,
                    cubes=("lounge",), stock={"Swamp": 99})
    async with AsyncSessionLocal() as s:
        s.add(LibraryServer(guild_id=ELSEWHERE, library_id=LIBRARY, bound_by="test"))
        s.add(DraftSession(session_id="s1", guild_id=guild, cube="lounge"))
        loan = CardLoan(guild_id=guild, borrower_id=BORROWER, library_id=LIBRARY,
                        cards=[{"name": "Swamp", "qty": 4}], state="assigned",
                        source="draft:s1")
        s.add(loan)
        await s.commit()
        return loan.id


async def _balance(guild):
    async with db_session() as s:
        return await wallet_service.balance_in(s, guild, BORROWER)


async def test_a_deck_cannot_be_collected_from_another_server(test_db, serve):
    loan_id = await _assigned_in(HOME)
    async with db_session() as s:
        await wallet_service.transfer_in(s, ELSEWHERE, "system:test-seed", BORROWER,
                                         200, "seed:test", notes="opening")

    status, _ = await svc.start_borrow(ELSEWHERE, BORROWER)

    assert status == "wrong_server"
    assert not serve.lent, "no cards moved"
    assert await _balance(ELSEWHERE) == 200, "and no deposit was taken there"
    assert (await _loan(loan_id)).state == "assigned", "the deck still waits at home"


async def test_the_same_deck_collects_normally_at_home(test_db, serve):
    """The refusal must be about the mismatch, not about borrowing at all."""
    await _assigned_in(HOME)
    async with db_session() as s:
        await wallet_service.transfer_in(s, HOME, "system:test-seed", BORROWER,
                                         200, "seed:test", notes="opening")

    status, _ = await svc.start_borrow(HOME, BORROWER)

    assert status == "dispatched"
    assert serve.lent


async def test_a_deck_can_still_be_GIVEN_BACK_from_anywhere(test_db, serve):
    """The library is one MTGO account and cards sitting in somebody's
    collection help nobody. Returning takes no money and releases the deposit
    against the loan's own server, so there is nothing to go wrong -- and
    refusing here would strand the cards over a technicality."""
    from conftest import a_library
    await a_library(LIBRARY, guild=HOME, kind="rental", collateral=100,
                    stock={"Swamp": 99})
    async with AsyncSessionLocal() as s:
        s.add(LibraryServer(guild_id=ELSEWHERE, library_id=LIBRARY, bound_by="test"))
        s.add(CardLoan(guild_id=HOME, borrower_id=BORROWER, library_id=LIBRARY,
                       cards=[{"name": "Swamp", "qty": 4}], state="borrowed",
                       source="draft:s1"))
        await s.commit()

    status, _ = await svc.start_return(ELSEWHERE, BORROWER)

    assert status == "dispatched"


async def _loan(loan_id):
    async with AsyncSessionLocal() as s:
        return await s.get(CardLoan, loan_id)
