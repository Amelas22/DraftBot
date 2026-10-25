"""The two ways into the borrow and return flows.

A slash command and a button arrive as different pycord shapes, and the flows
see neither: each is adapted into a `LibraryCaller`. What matters is that both
answer the player the same way the commands always have -- privately, with an
ephemeral defer and ephemeral followups -- and that a press from outside a
server reaches the flows' gate as "no server" rather than as an error.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from library_views.callers import ContextCaller, InteractionCaller
from services import library_loan_actions as actions

pytestmark = pytest.mark.asyncio


def _interaction(guild=True):
    return SimpleNamespace(
        user=SimpleNamespace(id=1234),
        guild=SimpleNamespace(id=99) if guild else None,
        guild_id=99 if guild else None,
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()))


def _ctx(guild=True):
    return SimpleNamespace(
        author=SimpleNamespace(id=1234),
        guild=SimpleNamespace(id=99) if guild else None,
        guild_id=99 if guild else None,
        defer=AsyncMock(),
        followup=SimpleNamespace(send=AsyncMock()))


async def test_a_button_press_is_answered_privately():
    """Visibly, too: pycord defers a component invisibly unless told not to,
    and ignores `ephemeral` when it does, so the press would show nothing --
    not the ephemeral "thinking…" the slash command shows -- while the flow
    queues behind the serve."""
    interaction = _interaction()
    caller = InteractionCaller(interaction)

    assert (caller.user_id, caller.guild_id) == (1234, 99)
    await caller.defer()
    await caller.send("hello")

    interaction.response.defer.assert_awaited_once_with(ephemeral=True, invisible=False)
    interaction.followup.send.assert_awaited_once_with("hello", ephemeral=True)


async def test_a_button_can_carry_a_view_with_its_reply():
    interaction = _interaction()
    view = discord.ui.View()

    await InteractionCaller(interaction).send("pick one", view=view)

    interaction.followup.send.assert_awaited_once_with(
        "pick one", view=view, ephemeral=True)


async def test_a_press_outside_a_server_carries_no_server():
    assert InteractionCaller(_interaction(guild=False)).guild_id is None


async def test_a_press_with_no_user_is_refused_at_the_boundary():
    """Pycord types the user as optional; a component press always has one, so
    its absence is a broken assumption to fail loudly on, not a player."""
    interaction = _interaction()
    interaction.user = None
    with pytest.raises(ValueError):
        InteractionCaller(interaction)


async def test_a_button_runs_the_same_gate_as_the_command():
    """Driven through a flow, not just the adapter: a press from outside a
    server is deferred and then told why, exactly as /library return is."""
    interaction = _interaction(guild=False)
    ctx = _ctx(guild=False)

    await actions.return_deck(InteractionCaller(interaction))
    await actions.return_deck(ContextCaller(ctx))

    said = actions.OUTSIDE_A_SERVER
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, invisible=False)
    interaction.followup.send.assert_awaited_once_with(said, ephemeral=True)
    ctx.defer.assert_awaited_once_with(ephemeral=True)
    ctx.followup.send.assert_awaited_once_with(said, ephemeral=True)
