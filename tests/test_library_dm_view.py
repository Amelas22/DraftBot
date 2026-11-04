"""The buttons on the library's DMs: Return on the reminder, Borrow on the
ready notice. A DM has no server, so a press runs the flow for the server the
presser's loan belongs to."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


async def test_dm_view_has_fixed_custom_ids():
    from library_views.dm_view import LibraryDmView, dm_view
    persistent = LibraryDmView()
    assert persistent.timeout is None
    assert [i.custom_id for i in persistent.children] == [
        "library_dm:borrow", "library_dm:return"]
    assert [i.custom_id for i in dm_view("borrow").children] == ["library_dm:borrow"]
    assert [i.custom_id for i in dm_view("return").children] == ["library_dm:return"]
    assert [i.label for i in persistent.children] == ["Borrow my deck", "Return my deck"]


async def test_the_dm_view_is_registered_at_startup():
    import re
    src = open("bot.py").read()
    assert re.search(r"bot\.add_view\(LibraryDmView\(\)\)", src)


def _interaction(user_id=7, guild=None):
    interaction = MagicMock()
    interaction.user = SimpleNamespace(id=user_id)
    interaction.guild = guild
    interaction.guild_id = None if guild is None else guild.id
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


@pytest.fixture
def flows(monkeypatch):
    import library_views.dm_view as view
    stubs = SimpleNamespace(borrow=AsyncMock(), return_deck=AsyncMock())
    monkeypatch.setattr(view, "borrow", stubs.borrow)
    monkeypatch.setattr(view, "return_deck", stubs.return_deck)
    return stubs


def _loan(guild_id="1355718878298116096", state="borrowed"):
    return SimpleNamespace(id=1, guild_id=guild_id, state=state)


async def test_return_runs_for_the_loans_server(monkeypatch, flows):
    import library_views.dm_view as view
    monkeypatch.setattr(view, "active_loan", AsyncMock(return_value=_loan()))
    interaction = _interaction()

    await view._press(interaction, "return")

    flows.return_deck.assert_awaited_once()
    caller = flows.return_deck.await_args.args[0]
    assert caller.guild_id == 1355718878298116096 and caller.user_id == 7
    flows.borrow.assert_not_awaited()


async def test_borrow_runs_for_the_loans_server(monkeypatch, flows):
    import library_views.dm_view as view
    monkeypatch.setattr(view, "active_loan", AsyncMock(return_value=_loan(state="assigned")))

    await view._press(_interaction(), "borrow")

    assert flows.borrow.await_args.args[0].guild_id == 1355718878298116096


async def test_the_loans_server_wins_over_where_the_press_happened(monkeypatch, flows):
    import library_views.dm_view as view
    monkeypatch.setattr(view, "active_loan", AsyncMock(return_value=_loan(guild_id="5")))

    await view._press(_interaction(guild=SimpleNamespace(id=99)), "return")

    assert flows.return_deck.await_args.args[0].guild_id == 5


async def test_a_press_with_nothing_out_says_so(monkeypatch, flows):
    import library_views.dm_view as view
    from services.library_loan_actions import LOAN_MESSAGES
    monkeypatch.setattr(view, "active_loan", AsyncMock(return_value=None))
    interaction = _interaction()

    await view._press(interaction, "return")

    interaction.response.send_message.assert_awaited_once_with(
        LOAN_MESSAGES["not_borrowed"], ephemeral=True)
    flows.return_deck.assert_not_awaited()


async def test_a_borrow_press_with_nothing_waiting_says_so(monkeypatch, flows):
    import library_views.dm_view as view
    from services.library_loan_actions import LOAN_MESSAGES
    monkeypatch.setattr(view, "active_loan", AsyncMock(return_value=None))
    interaction = _interaction()

    await view._press(interaction, "borrow")

    interaction.response.send_message.assert_awaited_once_with(
        LOAN_MESSAGES["no_loan"], ephemeral=True)


async def test_a_loan_from_no_real_server_is_refused_not_crashed(monkeypatch, flows):
    import library_views.dm_view as view
    from services.library_loan_actions import LOAN_MESSAGES
    monkeypatch.setattr(view, "active_loan", AsyncMock(return_value=_loan(guild_id="lotus")))
    interaction = _interaction()

    await view._press(interaction, "return")

    interaction.response.send_message.assert_awaited_once_with(
        LOAN_MESSAGES["not_borrowed"], ephemeral=True)
    flows.return_deck.assert_not_awaited()
