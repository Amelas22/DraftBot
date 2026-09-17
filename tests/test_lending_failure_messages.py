"""What a player is told when a library trade does not go through.

"The trade didn't complete" is true and useless. The serve knows exactly why --
the binder was short, the username did not resolve, the cards were never put in
the window -- and every one of those is something the player can fix themselves
if anyone tells them. A failed borrow and a failed return also need opposite
advice: one leaves the deck on the shelf, the other leaves it with them.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cogs.card_lending_commands import CardLendingCommands
from helpers.money_gate import explain_trade_failure


pytestmark = pytest.mark.asyncio

BINDER = "partner's binder does not hold enough (Swamp: your binder has 1, I need 4)"


def _ctx():
    ctx = SimpleNamespace(author=SimpleNamespace(id=1), guild=SimpleNamespace(id=99),
                          guild_id=99, defer=AsyncMock())
    ctx.followup = SimpleNamespace(send=AsyncMock())
    return ctx


def _said(ctx):
    return " ".join(str(c.args[0]) for c in ctx.followup.send.await_args_list if c.args)


async def test_a_binder_shortfall_is_explained_not_just_quoted():
    """MTGO puts received cards in the collection; trading uses the binder. A
    player who does not know that reads this as the bot losing their cards."""
    out = explain_trade_failure(BINDER)
    assert "binder" in out.lower()
    assert BINDER in out, "the serve's own words are kept"
    assert "trade binder" in out.lower(), "and what to do about it is added"


async def test_an_unknown_failure_is_passed_through_unchanged():
    assert explain_trade_failure("something odd") == "something odd"


async def test_the_link_command_named_is_one_that_exists():
    """The library pointed at /link, which is not a command in this bot."""
    from cogs.card_lending_commands import _MESSAGES
    assert "/link_mtgo" in _MESSAGES["not_linked"]
    assert "`/link`" not in _MESSAGES["not_linked"]


async def test_a_failed_return_says_you_still_have_the_deck(monkeypatch):
    import cogs.card_lending_commands as mod
    cog = CardLendingCommands(bot=SimpleNamespace())
    ctx = _ctx()
    await cog._report_outcome(ctx, "failed", "returned", BINDER)
    said = _said(ctx)
    assert "still have" in said.lower() or "still with you" in said.lower()
    assert "binder" in said.lower(), "the actual reason has to reach them"


async def test_a_failed_borrow_says_the_deck_is_still_reserved(monkeypatch):
    cog = CardLendingCommands(bot=SimpleNamespace())
    ctx = _ctx()
    await cog._report_outcome(ctx, "failed", "borrowed", "trade timed out")
    said = _said(ctx)
    assert "reserved" in said.lower() or "still waiting" in said.lower()
    assert "trade timed out" in said
