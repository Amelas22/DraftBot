"""One library's cards are not another's, though they sit in one account.

This is the claim the whole model rests on, so it is asserted directly rather
than left to follow from the pieces. Every library's stock lives commingled in
a single MTGO account: nothing physical keeps Cube Night's Swamps apart from
Lotus Lounge's. What keeps them apart is that a borrow may only draw against
the ledger rows attributed to its own library.

If that ever stops being true the failure is quiet and expensive -- a communal
borrow hands out a sponsor's Power, and the sponsor's own withdrawal fails
later for cards that were never theirs to lose.
"""
import pytest
import pytest_asyncio

from database.db_session import AsyncSessionLocal
from models.card_loan import CardLoan
from services import card_library_inventory as inv
from services import debt_service, wallet_service

pytestmark = pytest.mark.asyncio

ALPHA, BETA = "alpha", "beta"
GUILD_A, GUILD_B = "ga", "gb"


async def _donate(library_id, owner, name, qty):
    """Put cards on a library's shelf, the way a settled deposit does."""
    await debt_service.create_card_loan(
        guild_id=wallet_service.library_scope(library_id), lender_id=owner,
        borrower_id=wallet_service.HOUSE_LIBRARY, card_name=name,
        quantity=qty, created_by="test", source_id=f"seed:{library_id}:{owner}:{name}")


@pytest_asyncio.fixture(autouse=True)
async def _two_libraries(test_db):
    """Two libraries, one server each, stocked differently.

    Alpha holds Swamps and a Lotus somebody lent it. Beta holds Islands only --
    and crucially no Lotus, so anything Beta reports about a Lotus is Alpha's
    card leaking across.
    """
    from conftest import a_library
    await a_library(ALPHA, guild=GUILD_A, cubes=("cube",))
    await a_library(BETA, guild=GUILD_B, cubes=("cube",))
    await _donate(ALPHA, "sponsor", "Swamp", 20)
    await _donate(ALPHA, "sponsor", "Black Lotus", 1)
    await _donate(BETA, "other", "Island", 8)


async def test_a_librarys_holdings_are_only_its_own_donations():
    alpha = await inv.library_holdings(ALPHA)
    beta = await inv.library_holdings(BETA)

    assert alpha == {"Swamp": 20, "Black Lotus": 1}
    assert beta == {"Island": 8}
    assert "Black Lotus" not in beta, "a sponsor's Power is not everybody's"
    assert "Island" not in alpha


async def test_a_library_cannot_lend_what_the_other_was_given(monkeypatch):
    """The vault is generous on purpose: it reports the whole account, because
    that is all one MTGO account can say. The ledger is what refuses.

    Driven through lending's `lendable_now`, which is where the two answers
    meet -- it takes the lesser of what the shelf physically holds and what
    this library is owed.
    """
    import services.card_lending_service as lending
    plenty = {"Swamp": 20, "Black Lotus": 1, "Island": 8}
    monkeypatch.setattr(lending, "available_now", _stock(plenty))

    lendable = await lending.lendable_now(BETA)

    assert lendable.get("Black Lotus", 0) == 0, \
        "the vault holds one, but not Beta's one"
    assert lendable.get("Swamp", 0) == 0
    assert lendable["Island"] == 8


async def test_a_deck_out_of_one_library_does_not_deplete_the_other():
    """A loan subtracts from the shelf it came out of, and only that one."""
    async with AsyncSessionLocal() as s:
        s.add(CardLoan(guild_id=GUILD_A, library_id=ALPHA, borrower_id="u1",
                       cards=[{"name": "Swamp", "qty": 5}], state="borrowed",
                       source="fixture"))
        await s.commit()

    assert (await inv.library_available(ALPHA))["Swamp"] == 15
    assert (await inv.library_available(BETA))["Island"] == 8, \
        "Beta lent nobody anything"


async def test_an_unbound_server_draws_on_nothing():
    """Absence of a binding is not a default. A server nobody has pointed at a
    library lends nothing, rather than falling through to some library or to
    the account at large."""
    from services.library_service import library_for

    assert await library_for("unbound-guild") is None
    assert await inv.library_available(None) == {}


def _stock(cards):
    async def _now():
        return dict(cards)
    return _now


async def test_a_parked_loan_still_holds_its_cards():
    """A loan parked after a dispatch we could not account for may have a live
    MTGO trade against it -- the borrower may already be holding the deck.

    Every reservation list has to know that. The bug this pins is the two
    lists disagreeing: `dispatch_unknown` was added to the loan's active
    states and not to the shelf's, so the library went on offering the same
    cards to the next borrower while the first may have had them.
    """
    async with AsyncSessionLocal() as s:
        s.add(CardLoan(guild_id=GUILD_A, library_id=ALPHA, borrower_id="u1",
                       cards=[{"name": "Swamp", "qty": 6}],
                       state="dispatch_unknown", source="fixture"))
        await s.commit()

    assert (await inv.library_available(ALPHA))["Swamp"] == 14, \
        "the parked deck's cards are not free to lend again"


async def test_every_unfinished_loan_state_holds_its_cards():
    """The lists are the same list, so a state added to one cannot go missing
    from the other. Asserted directly because that divergence is silent."""
    from models.card_loan import ACTIVE_STATES

    assert inv.SPOKEN_FOR == ACTIVE_STATES
