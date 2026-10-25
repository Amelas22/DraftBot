"""The library panel's buttons: who may use them, and that they run the same
borrow and return the slash commands run."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import AsyncSessionLocal
from models.card_loan import CardLoan
from models.draft_session import DraftSession
from services import library_panel
from test_library_commands import _run

pytestmark = pytest.mark.asyncio

NO_DECK = "📭 You have no deck from this draft."


@pytest.fixture
def reserved(monkeypatch):
    async def users(draft, library_id=None):
        return {"3"}
    monkeypatch.setattr(library_panel, "library_users", users)


async def _seed(loans=()):
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(session_id="s1", guild_id="g", sign_ups={"3": "Ali"},
                           draft_chat_channel="555"))
        for borrower, source in loans:
            s.add(CardLoan(guild_id="g", library_id="lib", borrower_id=borrower,
                           cards=[{"name": "Swamp", "qty": 4}], state="assigned",
                           source=source))
        await s.commit()


def _interaction(user_id):
    interaction = MagicMock()
    interaction.user = SimpleNamespace(id=user_id)
    interaction.channel_id = 555
    interaction.guild = SimpleNamespace(id=99)
    interaction.guild_id = 99
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _refused(interaction):
    """The panel's own refusal: the interaction's one response, sent without a
    defer, so nothing else may answer it."""
    interaction.response.defer.assert_not_awaited()
    interaction.followup.send.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()
    assert interaction.response.send_message.await_args.kwargs == {"ephemeral": True}
    return interaction.response.send_message.await_args.args[0]


@pytest.fixture
def flows(monkeypatch):
    import library_views.panel_view as view
    stubs = SimpleNamespace(borrow=AsyncMock(), return_deck=AsyncMock())
    monkeypatch.setattr(view, "borrow", stubs.borrow)
    monkeypatch.setattr(view, "return_deck", stubs.return_deck)
    return stubs


async def test_the_button_says_what_the_slash_command_says(monkeypatch):
    """A press the panel lets through runs the borrow flow behind /library
    borrow, and the borrower hears exactly what the command tells them."""
    import library_views.panel_view as view
    import services.library_loan_actions as mod

    loan = SimpleNamespace(cards=[{"name": "Swamp", "qty": 7}], job_id="j1",
                           state="assigned", id=1, library_id=None)
    slash = await _run(monkeypatch, "borrow", "dispatched", loan=loan)

    # _run's stubs stay in place for the press; only the detached followup,
    # which _run drained itself, needs catching again.
    detached = []
    monkeypatch.setattr(mod, "spawn_followup", lambda label, coro: detached.append(coro))
    monkeypatch.setattr(view, "_session_for_channel", AsyncMock(return_value="s1"))
    monkeypatch.setattr(view, "may_use_panel", AsyncMock(return_value=None))
    interaction = _interaction(1234)
    await view.LibraryPanelView().borrow_button.callback(interaction)
    for coro in detached:
        await coro
    panel = " ".join(str(c.args[0]) for c in interaction.followup.send.await_args_list
                     if c.args)

    assert panel == slash
    assert "ready to collect" in panel
    # Answered once, by the flow's own defer; the panel adds no response.
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, invisible=False)
    interaction.response.send_message.assert_not_awaited()


async def test_a_borrower_from_this_draft_runs_the_command(test_db, reserved):
    from library_views.panel_view import may_use_panel
    await _seed(loans=[("7", "draft:s1")])
    assert await may_use_panel("s1", "7", "borrow") is None
    assert await may_use_panel("s1", "7", "return") is None


async def test_a_reserved_player_without_a_loan_runs_the_command(test_db, reserved):
    from library_views.panel_view import may_use_panel
    await _seed()
    assert await may_use_panel("s1", "3", "borrow") is None
    # Nothing to hand back without a loan.
    assert await may_use_panel("s1", "3", "return") == NO_DECK


async def test_a_borrower_from_another_draft_is_refused(test_db, reserved, flows):
    from library_views.panel_view import LibraryPanelView, may_use_panel
    await _seed(loans=[("3", "draft:other")])
    said = await may_use_panel("s1", "3", "borrow")
    assert said is not None and "another draft" in said
    assert said.startswith(NO_DECK)

    interaction = _interaction(3)
    view = LibraryPanelView()
    await view.borrow_button.callback(interaction)
    flows.borrow.assert_not_awaited()
    assert "another draft" in _refused(interaction)


async def test_a_loan_for_no_draft_counts_as_another_draft(test_db, reserved):
    from library_views.panel_view import may_use_panel
    await _seed(loans=[("3", "fixture:x")])
    said = await may_use_panel("s1", "3", "return")
    assert said is not None and "another draft" in said


async def test_a_spectator_is_refused(test_db, reserved):
    from library_views.panel_view import may_use_panel
    await _seed()
    assert await may_use_panel("s1", "42", "borrow") == NO_DECK


async def test_the_buttons_run_the_borrow_and_return_flows(test_db, reserved, flows):
    from library_views.callers import InteractionCaller
    from library_views.panel_view import LibraryPanelView
    await _seed(loans=[("7", "draft:s1")])
    view = LibraryPanelView()

    pressed = _interaction(7)
    await view.borrow_button.callback(pressed)
    await view.return_button.callback(_interaction(7))

    assert flows.borrow.await_count == 1
    assert flows.return_deck.await_count == 1
    caller = flows.borrow.await_args.args[0]
    assert isinstance(caller, InteractionCaller)
    assert caller.user_id == 7 and caller.guild_id == 99
    # The flow defers; the panel must not have answered first.
    pressed.response.defer.assert_not_awaited()
    pressed.response.send_message.assert_not_awaited()


async def test_a_button_outside_any_draft_chat_is_refused(test_db, flows):
    from library_views.panel_view import LibraryPanelView
    interaction = _interaction(7)
    interaction.channel_id = 999
    await LibraryPanelView().borrow_button.callback(interaction)
    flows.borrow.assert_not_awaited()
    assert _refused(interaction) == NO_DECK


async def test_panel_view_has_fixed_custom_ids():
    from library_views.panel_view import LibraryPanelView, panel_view
    borrow = panel_view("borrow")
    assert borrow is not None
    assert [i.custom_id for i in borrow.children] == ["library_panel:borrow"]
    ret = panel_view("return")
    assert ret is not None
    assert [i.custom_id for i in ret.children] == ["library_panel:return"]
    assert panel_view("closed") is None
    persistent = LibraryPanelView()
    assert persistent.timeout is None
    assert {i.custom_id for i in persistent.children} == {
        "library_panel:borrow", "library_panel:return"}
