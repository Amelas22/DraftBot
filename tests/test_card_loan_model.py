"""One borrower, one loan at a time -- enforced by the database.

The invariant is in the schema rather than only in the service because it is the
one rule that makes the rest tractable: with at most one active loan per
borrower, "what does this person owe the library" has a single answer, and
/borrow and /return never have to ask which deck the caller means. A check that
lives only in Python holds for callers who remember to go through it.
"""
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from database.db_session import AsyncSessionLocal
from models.card_loan import ACTIVE_STATES, CardLoan

pytestmark = pytest.mark.asyncio

DECK = [{"name": "Swamp", "qty": 7}, {"name": "Ghostly Wings", "qty": 1}]


async def _add(session, borrower="u1", guild="g1", state="assigned", cards=None):
    loan = CardLoan(guild_id=guild, borrower_id=borrower, state=state,
                    cards=cards or DECK, source="fixture:test")
    session.add(loan)
    await session.commit()
    return loan


async def test_a_deck_survives_the_round_trip_with_its_quantities(test_db):
    async with AsyncSessionLocal() as s:
        await _add(s)
        loan = await s.scalar(select(CardLoan))

    assert loan.cards == DECK, "per-card quantities are the deck"
    assert loan.state == "assigned"


@pytest.mark.parametrize("second_state", ACTIVE_STATES)
async def test_a_borrower_cannot_hold_two_active_loans(test_db, second_state):
    """Whatever stage the first loan is at, a second one is refused."""
    async with AsyncSessionLocal() as s:
        await _add(s, state="assigned")

    async with AsyncSessionLocal() as s:
        with pytest.raises(IntegrityError):
            await _add(s, state=second_state)


async def test_a_returned_loan_frees_the_borrower_for_the_next_one(test_db):
    """Otherwise a player could borrow exactly once, ever."""
    async with AsyncSessionLocal() as s:
        await _add(s, state="returned")
        await _add(s, state="assigned")
        active = (await s.scalars(
            select(CardLoan).where(CardLoan.state.in_(ACTIVE_STATES)))).all()

    assert len(active) == 1


async def test_two_borrowers_do_not_collide(test_db):
    async with AsyncSessionLocal() as s:
        await _add(s, borrower="u1")
        await _add(s, borrower="u2")
        assert len((await s.scalars(select(CardLoan))).all()) == 2


async def test_the_same_borrower_in_another_guild_DOES_collide(test_db):
    """This asserted the opposite until the library became one shelf.

    Per-guild uniqueness was defensible while a library was a per-server idea.
    It is not now: the library is a single MTGO account, so a borrower holding
    a deck is holding its only copies of those cards, and standing in another
    server does not entitle them to a second. Allowing it meant the same cards
    could be out twice over with the ledger showing both loans as legitimate.
    """
    from sqlalchemy.exc import IntegrityError

    async with AsyncSessionLocal() as s:
        await _add(s, borrower="u1", guild="g1")
        with pytest.raises(IntegrityError):
            await _add(s, borrower="u1", guild="g2")
