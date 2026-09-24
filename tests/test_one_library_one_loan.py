"""One library means one deck out at a time, in every server at once.

The library is a single MTGO account. A borrower holding a deck in one server
is holding the library's only copies of those cards, so being in another server
does not entitle them to a second deck -- but every loan lookup used to filter
by guild, so server B could not SEE the loan server A had written, told the
borrower they had nothing outstanding, and let them take the same cards again.

Two halves, and both had to move: the queries that read a borrower's loan, and
the unique index that was supposed to stop a second one being written.
"""
import pytest

from database.db_session import AsyncSessionLocal
from models.card_loan import CardLoan
import services.card_lending_service as svc

pytestmark = pytest.mark.asyncio

HOME, AWAY = "g1", "g2"
BORROWER = "u1"
DECK = [{"name": "Swamp", "qty": 4}]


async def _loan(guild, state="borrowed", cards=None):
    async with AsyncSessionLocal() as s:
        loan = CardLoan(guild_id=guild, borrower_id=BORROWER,
                        cards=cards or DECK, state=state, source="test")
        s.add(loan)
        await s.commit()
        return loan.id


async def test_a_deck_out_in_one_server_is_visible_in_another(test_db):
    """The lookup that /borrow and /mydeck use. Filtering by guild told a
    borrower in another server that they had nothing out, which is how they
    were offered a second deck."""
    await _loan(HOME)

    assert await svc.active_loan(BORROWER) is not None


async def test_a_returned_deck_is_not_outstanding_anywhere(test_db):
    await _loan(HOME, state="returned")

    assert await svc.active_loan(BORROWER) is None


async def test_the_database_refuses_a_second_deck_from_another_server(test_db):
    """The index is the backstop under the lookup. Even if a check-then-write
    races, or a caller forgets to look, the same person cannot have two decks
    out of one library."""
    from sqlalchemy.exc import IntegrityError

    await _loan(HOME)

    with pytest.raises(IntegrityError):
        await _loan(AWAY)


async def test_a_borrower_may_take_another_deck_once_the_first_is_back(test_db):
    """Partial on purpose: finished loans have to accumulate, or a player could
    borrow exactly once, ever."""
    await _loan(HOME, state="returned")
    await _loan(AWAY, state="returned")

    assert await _loan(HOME) is not None


async def test_two_different_borrowers_do_not_block_each_other(test_db):
    await _loan(HOME)
    async with AsyncSessionLocal() as s:
        s.add(CardLoan(guild_id=AWAY, borrower_id="u2", cards=DECK,
                       state="borrowed", source="test"))
        await s.commit()

    assert await svc.active_loan("u2") is not None


async def test_cards_promised_in_one_server_are_not_offered_in_another(test_db,
                                                                      monkeypatch):
    """available_now discounts decks that have been OFFERED but not yet
    accepted -- those cards are still physically on the shelf. Counting only
    this server's offers would let two people in different servers each be
    offered a trade for the same stack."""
    class _Serve:
        enabled = True

        async def vault(self):
            return {"available": True, "custodian": "Team01",
                    "top": [{"name": "Swamp", "qty": 4}]}

    monkeypatch.setattr(svc, "get_lending_client", lambda: _Serve())
    await _loan(AWAY, state="out_pending")

    assert (await svc.available_now())["Swamp"] == 0
