"""Choosing a counterparty who turns out to have nothing left to settle.

Discord rejects a string select with no options (400, error code 50035). The settle
flow's entity picker builds its options from the tix balance and any open card loans,
so when both are empty it used to send an empty select and die on the spot — which is
how prod incident 1 ended.

The picker lists anyone with a non-zero tix balance OR an open card loan, so a
card-only counterparty is offered with a balance of 0 (merge_counterparty_entries
says as much). That is the reachable route here: their last card came back between
the menu being drawn and clicked. /settle checks the same condition itself, against
live values, before it builds anything.

An error case rather than an ordinary one, so it is guarded rather than described in
detail — there is nothing for the player to decide, only something to be told.
"""
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import debt_views.settle_views as sv
from debt_views.settle_views import CounterpartySelectView

GUILD = SimpleNamespace(id=1, get_member=lambda _id: None)


def _view(balances, positions_by_cp=None):
    with patch.object(sv, "get_member_name_plain", lambda g, i: f"player-{i}"):
        return CounterpartySelectView(
            user_id="u1", guild_id="g1", balances=balances, guild=GUILD,
            positions_by_cp=positions_by_cp)


def _interaction(selected):
    interaction = MagicMock()
    interaction.data = {"values": [selected]}
    interaction.response.edit_message = AsyncMock()
    return interaction


def _flow(positions=None):
    """The lookups the callback makes before it decides anything, as one context."""
    stack = ExitStack()
    stack.enter_context(patch.object(sv, "get_entries_since_last_settlement",
                                     new=AsyncMock(return_value=[])))
    stack.enter_context(patch.object(sv, "get_open_card_positions",
                                     new=AsyncMock(return_value=positions or [])))
    stack.enter_context(patch.object(sv, "get_member_name_plain", lambda g, i: "them"))
    stack.enter_context(patch.object(sv, "get_member_name", lambda g, i: "**them**"))
    return stack


@pytest.mark.asyncio
async def test_nothing_left_to_settle_says_so_instead_of_sending_an_empty_select():
    """Built the way the picker really produces a zero-tix counterparty: no tix
    balance at all, listed only because a card was out on loan. get_all_balances_for
    never returns a zero, so a `{"c1": 0}` balances dict would be testing a state the
    code cannot reach."""
    view = _view({}, positions_by_cp={"c1": [{"card_name": "Black Lotus", "net": -1}]})
    interaction = _interaction("c1")

    with _flow():
        await view.select_callback(interaction)

    interaction.response.edit_message.assert_awaited_once()
    kwargs = interaction.response.edit_message.await_args.kwargs
    assert kwargs["view"] is None, "a picker with nothing to pick is rejected by Discord"
    assert "nothing left to settle" in kwargs["content"].lower()
    view.stop()


@pytest.mark.asyncio
async def test_a_real_balance_still_gets_the_picker():
    """Non-vacuity: the guard must not swallow the ordinary case."""
    view = _view({"c1": -5})
    interaction = _interaction("c1")

    with _flow():
        await view.select_callback(interaction)

    kwargs = interaction.response.edit_message.await_args.kwargs
    assert kwargs.get("view") is not None, "there is 5 tix to settle here"
    view.stop()


@pytest.mark.asyncio
async def test_an_open_card_loan_alone_is_enough_to_settle():
    """Zero tix but an outstanding card is still something to settle, so the balance
    on its own is the wrong thing to branch on."""
    view = _view({}, positions_by_cp={"c1": [{"card_name": "Black Lotus", "net": -1}]})
    interaction = _interaction("c1")
    positions = [{"card_name": "Black Lotus", "net": -1}]

    with _flow(positions=positions):
        await view.select_callback(interaction)

    kwargs = interaction.response.edit_message.await_args.kwargs
    assert kwargs.get("view") is not None
    view.stop()
