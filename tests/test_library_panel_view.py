"""The library panel's buttons: who may use them, and that they run the same
borrow and return the slash commands run."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from discord.utils import MISSING

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import AsyncSessionLocal
from models.card_loan import CardLoan
from models.draft_session import DraftSession
from services import library_panel_state
from test_library_commands import _run

pytestmark = pytest.mark.asyncio

NO_DECK = "📭 You have no deck from this draft."


@pytest.fixture
def reserved(monkeypatch):
    async def users(draft, library_id=None):
        return {"3"}
    monkeypatch.setattr(library_panel_state, "library_users", users)


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
    stubs = SimpleNamespace(borrow=AsyncMock(), return_deck=AsyncMock(),
                            decline=AsyncMock())
    monkeypatch.setattr(view, "borrow", stubs.borrow)
    monkeypatch.setattr(view, "return_deck", stubs.return_deck)
    monkeypatch.setattr(view, "decline", stubs.decline)
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
    assert [i.custom_id for i in borrow.children] == [
        "library_panel:borrow", "library_panel:some", "library_panel:decline"]
    ret = panel_view("return")
    assert ret is not None
    assert [i.custom_id for i in ret.children] == ["library_panel:return"]
    assert panel_view("closed") is None
    persistent = LibraryPanelView()
    assert persistent.timeout is None
    assert {i.custom_id for i in persistent.children} == {
        "library_panel:borrow", "library_panel:return", "library_panel:decline",
        "library_panel:some"}


# --- changing your mind after the draft --------------------------------------

async def test_any_drafter_may_press_borrow_to_ask_for_their_deck(test_db):
    """Not only the reserved: somebody who said no before the draft asks for
    their deck here, and the borrow flow decides whether they can have it."""
    from library_views.panel_view import may_use_panel
    await _seed()
    assert await may_use_panel("s1", "3", "borrow") is None
    assert await may_use_panel("s1", "42", "borrow") == NO_DECK, "not a drafter"


async def test_only_a_drafter_with_a_deck_from_here_may_turn_it_down(test_db):
    from library_views.panel_view import may_use_panel
    await _seed(loans=[("7", "draft:s1"), ("8", "draft:other")])
    assert await may_use_panel("s1", "7", "decline") is None
    assert "another draft" in (await may_use_panel("s1", "8", "decline") or "")
    assert await may_use_panel("s1", "3", "decline") == NO_DECK


async def test_the_buttons_name_the_draft_they_were_pressed_in(test_db, reserved, flows):
    from library_views.panel_view import LibraryPanelView
    await _seed(loans=[("7", "draft:s1")])
    view = LibraryPanelView()

    await view.borrow_button.callback(_interaction(7))
    await view.decline_button.callback(_interaction(7))

    assert flows.borrow.await_args.args[1] == "s1"
    assert flows.decline.await_args.args[1] == "s1"


async def test_turning_a_deck_down_says_how_to_change_your_mind(monkeypatch):
    import services.library_loan_actions as mod
    caller = SimpleNamespace(user_id=7, guild_id=99, defer=AsyncMock(), send=AsyncMock())
    monkeypatch.setattr(mod, "decline_offer", AsyncMock(return_value=True))

    await mod.decline(caller, "s1")

    said = caller.send.await_args.args[0]
    assert "back on the shelf" in said and "Borrow my deck" in said
    mod.decline_offer.assert_awaited_once_with(7, "s1")


async def test_nothing_to_turn_down_points_at_return(monkeypatch):
    import services.library_loan_actions as mod
    caller = SimpleNamespace(user_id=7, guild_id=99, defer=AsyncMock(), send=AsyncMock())
    monkeypatch.setattr(mod, "decline_offer", AsyncMock(return_value=False))

    await mod.decline(caller, "s1")

    assert "Return my deck" in caller.send.await_args.args[0]


# --- borrowing only some cards -----------------------------------------------

async def test_borrow_mode_shows_some_cards_button():
    from library_views.panel_view import panel_view
    borrow = panel_view("borrow")
    assert borrow is not None
    assert "library_panel:some" in [i.custom_id for i in borrow.children]
    ret = panel_view("return")
    assert ret is not None
    assert "library_panel:some" not in [i.custom_id for i in ret.children]


async def test_the_form_has_three_fields_and_only_the_first_is_required():
    from library_views.some_cards import SomeCardsModal
    modal = SomeCardsModal("s1")
    assert modal.title == "Borrow only some cards"
    assert [c.label for c in modal.children] == ["Card 1", "Card 2", "Card 3"]
    assert [c.required for c in modal.children] == [True, False, False]
    assert all(c.max_length == 141 for c in modal.children)


async def test_a_press_opens_the_form_as_the_first_response(test_db, reserved):
    from library_views.panel_view import LibraryPanelView
    from library_views.some_cards import SomeCardsModal
    await _seed()
    interaction = _interaction(3)
    interaction.response.send_modal = AsyncMock()
    await LibraryPanelView().some_button.callback(interaction)
    interaction.response.defer.assert_not_awaited()
    interaction.response.send_message.assert_not_awaited()
    modal = interaction.response.send_modal.await_args.args[0]
    assert isinstance(modal, SomeCardsModal)


async def test_a_refused_press_answers_without_a_modal(test_db, reserved):
    from library_views.panel_view import LibraryPanelView
    await _seed(loans=[("3", "draft:other")])
    interaction = _interaction(3)
    interaction.response.send_modal = AsyncMock()
    await LibraryPanelView().some_button.callback(interaction)
    interaction.response.send_modal.assert_not_awaited()
    assert "another draft" in _refused(interaction)


@pytest.fixture
def some(monkeypatch):
    import library_views.some_cards as mod
    stubs = SimpleNamespace(borrow_some=AsyncMock(), spawned=[])

    def spawn(label, coro):
        stubs.spawned.append(coro)
    monkeypatch.setattr(mod, "borrow_some", stubs.borrow_some)
    monkeypatch.setattr(mod, "spawn_followup", spawn)
    return stubs


def _pressed(user_id=3, message_id=777):
    """A press on the confirm message, which the modal's followup posted."""
    interaction = _interaction(user_id)
    interaction.message = SimpleNamespace(id=message_id)
    interaction.response.edit_message = AsyncMock()
    return interaction


def _origin():
    """The modal submission that posted the confirm message."""
    origin = MagicMock()
    origin.followup.edit_message = AsyncMock()
    return origin


async def test_cancel_borrows_nothing_and_takes_the_buttons_away(some):
    from library_views.some_cards import SomeCardsConfirmView
    view = SomeCardsConfirmView([{"name": "Swamp", "qty": 1}], "s1")
    interaction = _pressed()
    await view.cancel.callback(interaction)
    some.borrow_some.assert_not_called()
    assert some.spawned == []
    interaction.response.edit_message.assert_awaited_once_with(
        content="👌 Nothing borrowed.", view=None)
    assert view.is_finished()


async def test_a_second_cancel_is_a_no_op(some):
    from library_views.some_cards import SomeCardsConfirmView
    view = SomeCardsConfirmView([{"name": "Swamp", "qty": 1}], "s1")
    await view.cancel.callback(_pressed())
    again = _pressed()
    await view.cancel.callback(again)
    again.response.edit_message.assert_not_awaited()
    again.response.send_message.assert_not_awaited()


async def test_confirm_borrows_exactly_the_carried_cards(some):
    from library_views.some_cards import SomeCardsConfirmView
    cards = [{"name": "Swamp", "qty": 1}, {"name": "Island", "qty": 2}]
    origin = _origin()
    view = SomeCardsConfirmView(cards, "s1", origin=origin)
    interaction = _pressed()
    await view.confirm.callback(interaction)
    assert view.is_finished()
    assert len(some.spawned) == 1
    await some.spawned[0]
    caller, session_id, sent = some.borrow_some.await_args.args
    assert (caller.user_id, session_id, sent) == (3, "s1", cards)
    # borrow_some defers; acknowledging here too would make that a second ack.
    interaction.response.defer.assert_not_awaited()
    interaction.response.edit_message.assert_not_awaited()
    # The buttons are taken away through the modal's own webhook, which posted
    # the message, leaving this press's one response to borrow_some.
    origin.followup.edit_message.assert_awaited_once_with(777, view=None)


async def test_a_second_confirm_is_a_no_op(some):
    from library_views.some_cards import SomeCardsConfirmView
    view = SomeCardsConfirmView([{"name": "Swamp", "qty": 1}], "s1", origin=_origin())
    first, second = _pressed(), _pressed()
    await view.confirm.callback(first)
    await view.confirm.callback(second)
    for coro in some.spawned:
        await coro
    assert some.borrow_some.await_count == 1
    second.response.defer.assert_not_awaited()


async def test_a_failed_button_clear_does_not_break_the_borrow(some):
    import discord
    from library_views.some_cards import SomeCardsConfirmView
    origin = _origin()
    origin.followup.edit_message.side_effect = discord.HTTPException(
        MagicMock(status=404), "Unknown Message")
    view = SomeCardsConfirmView([{"name": "Swamp", "qty": 1}], "s1", origin=origin)
    await view.confirm.callback(_pressed())
    await some.spawned[0]
    assert some.borrow_some.await_count == 1


async def _submit(monkeypatch, plan, hold=5, loan=None, library=None):
    import library_views.some_cards as mod
    monkeypatch.setattr(mod, "match_for_borrow", AsyncMock(return_value=plan))
    monkeypatch.setattr(mod, "active_loan", AsyncMock(return_value=loan))
    behind = AsyncMock(return_value=library if library is not None else object())
    monkeypatch.setattr(mod, "library_behind", behind)
    held = AsyncMock(return_value=hold)
    monkeypatch.setattr(mod, "hold_for", held)
    modal = mod.SomeCardsModal("s1")
    modal.children[0].value = "swamp"
    interaction = _interaction(3)
    await modal.callback(interaction)
    interaction.behind, interaction.held = behind, held
    return interaction


async def test_the_quote_prices_a_waiting_deck_by_its_own_library(monkeypatch):
    """The trade charges through library_behind(loan, ...), which prefers the
    library stamped on the loan; the quote must read the same one."""
    from services.card_name_match import NameMatch
    from services.library_loan_actions import SomeCardsPlan
    match = NameMatch(found=[], unknown=[], ambiguous=[])
    plan = SomeCardsPlan(available=[{"name": "Swamp", "qty": 1}], reserved=[], match=match)
    loan, library = SimpleNamespace(library_id="stamped"), object()

    interaction = await _submit(monkeypatch, plan, loan=loan, library=library)

    interaction.behind.assert_awaited_once_with(loan, 99)
    assert interaction.held.await_args.args[0] is library
    interaction.behind.reset_mock()
    interaction = await _submit(monkeypatch, plan, loan=None)
    interaction.behind.assert_awaited_once_with(None, 99)


async def test_a_refusal_from_the_match_is_shown_as_is(monkeypatch):
    interaction = await _submit(monkeypatch, "nope")
    interaction.response.defer.assert_awaited_once()
    assert interaction.followup.send.await_args.args[0] == "nope"
    assert interaction.followup.send.await_args.kwargs["view"] is MISSING


async def test_confirm_is_offered_only_when_something_is_available(monkeypatch):
    from services.card_name_match import NameMatch
    from services.library_loan_actions import SomeCardsPlan
    match = NameMatch(found=[], unknown=["x"], ambiguous=[])
    empty = SomeCardsPlan(available=[], reserved=[], match=match)
    interaction = await _submit(monkeypatch, empty)
    view = interaction.followup.send.await_args.kwargs["view"]
    assert [i.label for i in view.children] == ["Cancel"]

    full = SomeCardsPlan(available=[{"name": "Swamp", "qty": 1}], reserved=[], match=match)
    interaction = await _submit(monkeypatch, full)
    view = interaction.followup.send.await_args.kwargs["view"]
    assert [i.label for i in view.children] == ["Confirm", "Cancel"]
    assert "5 tix" in interaction.followup.send.await_args.args[0]
