"""What a deck costs is the price of the LIBRARY its cards came from.

The guild-wide `card_library.collateral_tix` in configs/<guild>.json was the
number a server admin could rewrite through the bot's own commands, which is
exactly the number they must not be able to lower. It is gone. The price comes
from the library a server is bound to, set by whoever runs the library from a
shell tool, and every cube that library offers is offered on those terms.

A server bound to no library cannot borrow at all -- that is a refusal, not a
free one.
"""
import pytest

from database.db_session import AsyncSessionLocal
from models.card_loan import CardLoan
from conftest import a_library
from models.draft_session import DraftSession
import services.card_lending_service as svc

pytestmark = pytest.mark.asyncio

GUILD, BORROWER, CUBE, SESSION = "g1", "u1", "mycube", "s1"


async def _priced(collateral, cube=CUBE, guild=GUILD, library=None):
    await a_library(library or f"lib-{guild}", guild=guild,
                    collateral=collateral, cubes=(cube,))


async def _drafted_loan(cube=CUBE, library=None):
    """A deck assigned from a library. `library` must name one that EXISTS.

    It used to hardcode `lib-{GUILD}`, which the two-library test never
    creates -- so library_behind found no such row, fell through to the
    server's library, and the assertion labelled "its own library's price"
    was measuring the guild fallback instead.
    """
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(session_id=SESSION, guild_id=GUILD, cube=cube))
        loan = CardLoan(guild_id=GUILD, borrower_id=BORROWER,
                        library_id=library or f"lib-{GUILD}",
                        cards=[{"name": "Swamp", "qty": 4}], state="assigned",
                        source=f"draft:{SESSION}")
        s.add(loan)
        await s.commit()
        return loan


async def test_a_deck_costs_what_its_cube_costs(test_db):
    await _priced(25)
    loan = await _drafted_loan()

    assert await svc.collateral_for(loan, GUILD) == 25


async def test_a_free_cube_costs_nothing_rather_than_falling_back(test_db):
    """Zero is a price somebody chose. Treating it as "unset" and reaching for
    the guild config is how a free cube would silently start charging."""
    await _priced(0)
    loan = await _drafted_loan()

    assert await svc.collateral_for(loan, GUILD) == 0


async def test_an_unpriced_cube_refuses_rather_than_guessing(test_db):
    """No row means this server is not supported for this cube. Falling back to
    the guild number would let an unpriced cube be borrowed at whatever the
    server happened to have set -- the hole this exists to close."""
    loan = await _drafted_loan()

    assert await svc.collateral_for(loan, GUILD) is None


async def test_the_same_cube_costs_what_each_library_charges(test_db):
    """Two communities can both offer PowerLSV on their own terms -- one from
    members' own cards for nothing, one from a sponsor's for a deposit. What
    differs is the LIBRARY behind it, not the room somebody is standing in."""
    await _priced(0, guild=GUILD, library="free-lib")
    await _priced(50, guild="g2", library="paid-lib")
    loan = await _drafted_loan(library="free-lib")

    assert await svc.collateral_for(loan, GUILD) == 0, "its own library's price"

    async with AsyncSessionLocal() as s:
        other = await s.get(CardLoan, loan.id)
        other.library_id = "paid-lib"
        await s.commit()
    assert await svc.collateral_for(await _reload(loan.id), "g2") == 50


async def test_a_loan_naming_no_library_falls_back_to_this_server_s(test_db):
    """A seeded fixture, a hand-made repair, or any loan written before the
    column existed names no library. The server's own is the only sensible
    answer, and it is a real one -- unlike the guild config number this used to
    reach for, which a server admin could rewrite through the bot."""
    await _priced(7)
    async with AsyncSessionLocal() as s:
        loan = CardLoan(guild_id=GUILD, borrower_id=BORROWER,
                        cards=[{"name": "Swamp", "qty": 4}], state="assigned",
                        source="fixture:seed")
        s.add(loan)
        await s.commit()

    assert await svc.collateral_for(loan, GUILD) == 7


async def _reload(loan_id):
    async with AsyncSessionLocal() as s:
        return await s.get(CardLoan, loan_id)


async def test_a_free_cube_needs_no_wallet(test_db, monkeypatch):
    """The bug this fixes: a guild-wide collateral was read at the gate, so a
    server whose cubes are all free was told to enable the wallet before anyone
    could borrow. What a deck costs belongs to the CUBE, and a free one touches
    no money at all."""
    import cogs.card_lending_commands as cog

    assert cog.library_gate(_a_ctx(monkeypatch)) is None


async def test_the_gate_lets_a_charging_guild_through_without_a_wallet(
        test_db, monkeypatch):
    """The stale read is invisible, so it is pinned by behaviour rather than by
    reading the source: the guild config still HOLDS a collateral_tix, it
    simply must not decide anything any more.

    A guild that still carries the old number and has no wallet is the case
    that separates the two readings -- the gate used to refuse it outright,
    and now says nothing, because whether money is involved is a question
    about the cube and is asked at the charge.
    """
    import cogs.card_lending_commands as cog

    monkeypatch.setattr("config.get_config",
                        lambda gid: {"features": {"card_library":
                                                  {"enabled": True,
                                                   "collateral_tix": 100}}})

    assert cog.library_gate(_a_ctx(monkeypatch)) is None


def _a_ctx(monkeypatch):
    """A usable serve in some guild.

    Takes monkeypatch rather than assigning the module attribute outright: an
    unrestored patch here leaked into every later test in the session, and
    pytest-randomly is installed, so the alphabetical ordering that hides it
    today is not something to rely on.
    """
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    import cogs.card_lending_commands as cog
    monkeypatch.setattr(cog, "get_lending_client",
                        MagicMock(return_value=SimpleNamespace(enabled=True)))
    return SimpleNamespace(guild=SimpleNamespace(id=999), guild_id=999)


async def test_a_charging_cube_refuses_before_any_cards_move(test_db, monkeypatch):
    """The other side of the same rule: a cube that DOES ask for a deposit
    needs the wallet enabled, and the refusal has to land before the serve is
    asked for anything.

    Asked per cube at the charge rather than once at the gate -- gating the
    whole library on a guild-wide number is what made a free cube demand a
    wallet in the first place.
    """
    from conftest import FakeLendingServe

    client = FakeLendingServe()
    monkeypatch.setattr(svc, "get_lending_client", lambda: client)

    async def handle(_):
        return "Borrower01"
    monkeypatch.setattr(svc, "_mtgo_handle", handle)
    monkeypatch.setattr(svc, "is_money_server", lambda gid: False)

    await _priced(25)
    await _drafted_loan()
    await _stock("Swamp", 4)

    status, _ = await svc.start_borrow(GUILD, BORROWER)

    assert status == "no_wallet"
    assert not client.lent, "no trade was attempted"


async def _stock(name, qty, library=None):
    """Put cards on this library's shelf, the way a settled deposit does.

    Needed because a borrow now checks it can actually cover the deck before
    it looks at money. Without stock the refusal under test is never reached,
    and the test would pass or fail on the order of two unrelated checks.
    """
    from services import debt_service, wallet_service
    await debt_service.create_card_loan(
        guild_id=wallet_service.library_scope(library or f"lib-{GUILD}"),
        lender_id="donor", borrower_id=wallet_service.HOUSE_LIBRARY,
        card_name=name, quantity=qty, created_by="test",
        source_id=f"seed-{name}")
