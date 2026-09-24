"""Telling a borrower they cannot afford the deposit, in numbers.

"You don't have enough tix to cover the deposit" leaves a player guessing at
every figure that matters: what the deposit is, what they hold, and how much to
top up. They cannot even tell whether they are one tix short or ten, so the
only way forward is to add some and try again until it works.

The same question is already answered properly for a short library -- name the
card, what there is and what they must find -- and a short wallet deserves the
same treatment.
"""
import pytest

from conftest import stub_library

from database.db_session import db_session
from services import wallet_service
import services.card_lending_service as svc
from cogs.card_lending_commands import describe_deposit_shortfall

GUILD, BORROWER = "g1", "u1"


def test_the_message_names_the_deposit_what_they_have_and_the_gap():
    text = describe_deposit_shortfall({"deposit": 5, "have": 2, "short": 3})

    assert "5" in text, "what the deck's deposit is"
    assert "2" in text, "what they hold"
    assert "3" in text, "and how much to top up"
    assert "nothing has been charged" in text.lower()


def test_an_empty_wallet_still_reads_as_a_number():
    """`0` must survive the templating -- a falsy figure dropped from the text
    is exactly the case where the player is most confused."""
    text = describe_deposit_shortfall({"deposit": 5, "have": 0, "short": 5})

    assert "0" in text


async def _a_loan_for(borrower):
    """A deck waiting to be collected -- deposit_shortfall only ever runs to
    explain why a particular borrow could not be paid for."""
    from models.card_loan import CardLoan
    from database.db_session import AsyncSessionLocal
    async with AsyncSessionLocal() as s:
        s.add(CardLoan(guild_id=GUILD, borrower_id=borrower,
                       cards=[{"name": "Swamp", "qty": 4}], state="assigned",
                       source="fixture:seed"))
        await s.commit()


@pytest.mark.asyncio
async def test_the_figures_come_from_the_loan_and_the_wallet(test_db, monkeypatch):
    """What a borrow costs is the price of the cube its deck came from, so the
    shortfall is quoted against the loan rather than against a guild-wide
    number. This loan has no draft behind it, so it falls back to the guild
    setting -- which is the only case where that number is still consulted."""
    stub_library(monkeypatch, svc, collateral=5)
    await _a_loan_for(BORROWER)
    async with db_session() as s:
        await wallet_service.transfer_in(s, GUILD, "system:test-seed", BORROWER, 2,
                                         "seed:test", notes="opening")

    assert await svc.deposit_shortfall(GUILD, BORROWER) == {
        "deposit": 5, "have": 2, "short": 3}


@pytest.mark.asyncio
async def test_a_library_that_charges_nothing_is_never_short(test_db, monkeypatch):
    stub_library(monkeypatch, svc, collateral=0)
    await _a_loan_for(BORROWER)

    assert await svc.deposit_shortfall(GUILD, BORROWER) == {
        "deposit": 0, "have": 0, "short": 0}
