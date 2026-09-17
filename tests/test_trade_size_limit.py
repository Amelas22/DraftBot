"""An order too big for one MTGO trade is refused, not split.

The serve will split a large order across several trades and run them one at a
time. Settling that correctly is a materially harder problem than settling one
trade -- each batch books its own cards, a scan may see only some of them, the
deposit must be released against what is really still owed -- and getting it
wrong costs a borrower their deposit or their cards. So the line is drawn
before anything moves: an order above the limit never becomes a trade.

Refusing costs a player a clear message. Splitting badly costs them tix.
"""
import pytest

from database.db_session import AsyncSessionLocal, db_session
from models.card_loan import CardLoan
from services import wallet_service
from services.mtgo_tradebot_client import max_cards_per_trade, too_large
import services.card_lending_service as svc
from conftest import FakeLendingServe

GUILD, BORROWER, HANDLE = "g1", "u1", "Borrower01"


def test_the_limit_is_the_last_size_that_fits():
    assert not too_large(max_cards_per_trade())
    assert too_large(max_cards_per_trade() + 1)


@pytest.fixture
def rig(monkeypatch):
    client = FakeLendingServe({"Swamp": 500})
    monkeypatch.setattr(svc, "get_lending_client", lambda: client)

    async def handle(_):
        return HANDLE
    monkeypatch.setattr(svc, "_mtgo_handle", handle)
    monkeypatch.setattr(svc, "card_library_collateral", lambda gid: 5)

    async def no_debt(*a, **k):
        return []
    monkeypatch.setattr(svc, "on_inflow", no_debt)
    return client


async def _seed(cards, state="assigned"):
    async with AsyncSessionLocal() as s:
        loan = CardLoan(guild_id=GUILD, borrower_id=BORROWER, cards=cards,
                        state=state, source="fixture")
        s.add(loan)
        await s.commit()
        return loan.id


async def _balances():
    async with db_session() as s:
        return (await wallet_service.balance_in(s, GUILD, BORROWER),
                await wallet_service.balance_in(s, GUILD, svc.collateral_holder(GUILD)))


@pytest.mark.asyncio
async def test_a_deck_too_big_for_one_trade_is_refused(test_db, rig):
    big = [{"name": "Swamp", "qty": max_cards_per_trade() + 5}]
    loan_id = await _seed(big)

    status, _ = await svc.start_borrow(GUILD, BORROWER)

    assert status == "too_large"
    assert rig.lent == [], "nothing may reach the serve"


@pytest.mark.asyncio
async def test_refusing_costs_the_borrower_nothing(test_db, rig):
    """The deposit is taken before the cards move, so a refusal that happened
    AFTER the hold would leave tix in the library against a loan that never
    left the shelf. The size check has to come first."""
    async with db_session() as s:
        await wallet_service.transfer_in(s, GUILD, "system:seed", BORROWER, 5,
                                         "seed", notes="opening")
    await _seed([{"name": "Swamp", "qty": max_cards_per_trade() + 5}])

    await svc.start_borrow(GUILD, BORROWER)

    assert await _balances() == (5, 0), "no deposit may be held for a refused order"


@pytest.mark.asyncio
async def test_a_deck_that_fits_still_goes(test_db, rig):
    async with db_session() as s:
        await wallet_service.transfer_in(s, GUILD, "system:seed", BORROWER, 5,
                                         "seed", notes="opening")
    await _seed([{"name": "Swamp", "qty": max_cards_per_trade()}])

    status, _ = await svc.start_borrow(GUILD, BORROWER)

    assert status == "dispatched"
    assert rig.lent, "an order at the limit is not too large"


def test_the_limit_follows_the_environment_not_the_import(monkeypatch):
    """Read when asked, never at import.

    A module-level `os.getenv` is evaluated by whichever import touches the
    module first, and a library cannot see who that is. bot.py loads the .env
    at line 41, but its line-7 import of database.message_management pulls the
    client in transitively before that — so the limit froze at its default
    while the environment said 300, and the bot refused a 13-card deck saying
    MTGO only moves 10. Setting it after import must still be seen.
    """
    monkeypatch.setenv("MTGO_MAX_CARDS_PER_TRADE", "300")
    assert max_cards_per_trade() == 300
    assert not too_large(42), "a real deck fits under a 300 limit"

    monkeypatch.setenv("MTGO_MAX_CARDS_PER_TRADE", "10")
    assert max_cards_per_trade() == 10, "and a later change is seen too"


def test_an_unreadable_limit_falls_back_rather_than_crashing(monkeypatch):
    """A typo in the .env should refuse large orders, not take the bot down on
    its first borrow."""
    monkeypatch.setenv("MTGO_MAX_CARDS_PER_TRADE", "three hundred")
    assert max_cards_per_trade() == 10
