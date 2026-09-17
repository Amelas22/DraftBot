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

from database.db_session import db_session
from services import wallet_service
import services.card_lending_service as svc
from cogs.card_lending_commands import describe_deposit_shortfall

GUILD, BORROWER = "g1", "u1"


def test_the_message_names_the_deposit_what_they_have_and_the_gap():
    text = describe_deposit_shortfall({"need": 5, "have": 2, "short": 3})

    assert "5" in text, "what the deck's deposit is"
    assert "2" in text, "what they hold"
    assert "3" in text, "and how much to top up"
    assert "nothing has been charged" in text.lower()


def test_an_empty_wallet_still_reads_as_a_number():
    """`0` must survive the templating -- a falsy figure dropped from the text
    is exactly the case where the player is most confused."""
    text = describe_deposit_shortfall({"need": 5, "have": 0, "short": 5})

    assert "0" in text


@pytest.mark.asyncio
async def test_the_figures_come_from_the_guild_and_the_wallet(test_db, monkeypatch):
    monkeypatch.setattr(svc, "card_library_collateral", lambda gid: 5)
    async with db_session() as s:
        await wallet_service.transfer_in(s, GUILD, "system:test-seed", BORROWER, 2,
                                         "seed:test", notes="opening")

    assert await svc.deposit_shortfall(GUILD, BORROWER) == {"need": 5, "have": 2, "short": 3}


@pytest.mark.asyncio
async def test_a_library_that_charges_nothing_is_never_short(test_db, monkeypatch):
    monkeypatch.setattr(svc, "card_library_collateral", lambda gid: 0)

    assert await svc.deposit_shortfall(GUILD, BORROWER) == {"need": 0, "have": 0, "short": 0}
