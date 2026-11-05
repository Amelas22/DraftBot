"""The Sub for a player picker: who it offers as absent, and what Confirm does
-- the checks and the offer under the draft's panel lock, chat access for a sub
from outside the draft, and the replies."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
import pytest_asyncio

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import AsyncSessionLocal
from helpers.utils import PrefilledSelect
from models.draft_session import DraftSession

pytestmark = pytest.mark.asyncio

REFUSAL = ("📭 **Bo** still has their deck out. It has to come back before a "
           "sub can take it.")


@pytest_asyncio.fixture(autouse=True)
async def draft(test_db):  # noqa: F811
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(session_id="s1", guild_id="99",
                           sign_ups={"3": "Ali", "5": "Bo", "4": "Cy"},
                           team_a=["3", "5"], team_b=["4"],
                           draft_chat_channel="555"))
        await s.commit()


def _member(user_id, name):
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.display_name = name
    return member


@contextmanager
def _picked(absent, sub):
    """What Discord sends back for the two selects."""
    with patch.object(discord.ui.Select, "values", new=[absent]), \
            patch.object(PrefilledSelect, "values", new=[sub]):
        yield


def _interaction(user_id=3):
    interaction = MagicMock()
    interaction.user = SimpleNamespace(id=user_id, display_name="Ali")
    interaction.guild = SimpleNamespace(id=99)
    interaction.client = MagicMock()
    interaction.message = SimpleNamespace(id=777)
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.followup.edit_message = AsyncMock()
    interaction.channel.send = AsyncMock()
    return interaction


@pytest.fixture
def stubs(monkeypatch):
    s = SimpleNamespace(check=AsyncMock(return_value=None),
                        offer=AsyncMock(return_value=42), nudge=AsyncMock(),
                        grant=AsyncMock(return_value=SimpleNamespace(
                            granted=["#draft-chat"], failed=[], announced=True)))
    monkeypatch.setattr("library_views.sub_picker.check_sub", s.check)
    monkeypatch.setattr("library_views.sub_picker.offer_to_sub", s.offer)
    monkeypatch.setattr("library_views.sub_picker.nudge", s.nudge)
    monkeypatch.setattr("library_views.sub_picker.grant_sub_access", s.grant)
    return s


async def test_build_lists_the_pressers_teammates_including_them():
    from library_views.sub_picker import SubPickerView
    view = await SubPickerView.build("s1", "3", False)
    absent = view.children[0]
    assert [(o.value, o.label) for o in absent.options] == [("3", "Ali"), ("5", "Bo")]


async def test_build_for_a_manager_lists_both_teams():
    from library_views.sub_picker import SubPickerView
    view = await SubPickerView.build("s1", "9", True)
    assert {o.value for o in view.children[0].options} == {"3", "5", "4"}


async def test_the_picker_has_two_selects_then_confirm_and_cancel():
    from library_views.sub_picker import SubPickerView
    view = await SubPickerView.build("s1", "3", False)
    absent, who, confirm, cancel = view.children
    assert (absent.custom_id, absent.placeholder) == ("library_sub:absent", "Who can't play?")
    assert who.type == discord.ComponentType.user_select
    assert (who.custom_id, who.placeholder, who.min_values, who.max_values) == (
        "library_sub:who", "Who is subbing?", 1, 1)
    assert (confirm.label, confirm.style) == ("Confirm", discord.ButtonStyle.primary)
    assert (cancel.label, cancel.style) == ("Cancel", discord.ButtonStyle.secondary)
    assert view.timeout == 180


async def test_confirm_runs_checks_offer_and_reply_for_an_insider(stubs):
    from library_views.sub_picker import SubPickerView
    view = await SubPickerView.build("s1", "3", False)
    interaction = _interaction()
    with _picked("5", _member(3, "Ali")):
        await view.confirm.callback(interaction)
    stubs.check.assert_awaited_once()
    stubs.offer.assert_awaited_once()
    stubs.grant.assert_not_awaited()
    stubs.nudge.assert_awaited_once_with(interaction.client, "s1")
    assert interaction.channel.send.await_args.args[0] == (
        "**Ali** is subbing for **Bo** and has been offered **Bo**'s deck.")
    assert interaction.followup.send.await_args.args[0].startswith(
        "✅ **Bo**'s deck is offered to **Ali**.")
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.followup.edit_message.assert_awaited_once_with(777, view=None)
    assert view.is_finished()


async def test_confirm_grants_access_for_an_outside_sub(stubs):
    from library_views.sub_picker import SubPickerView
    view = await SubPickerView.build("s1", "3", False)
    interaction = _interaction()
    with _picked("5", _member(7, "Sal")):
        await view.confirm.callback(interaction)
    stubs.grant.assert_awaited_once()
    guild, row, member, decision = stubs.grant.await_args.args
    assert (guild, row.session_id, member.id, decision.team_key) == (
        interaction.guild, "s1", 7, "A")
    assert stubs.grant.await_args.kwargs["note"] == (
        "**Sal** is subbing for **Bo** and has been offered **Bo**'s deck.")
    assert stubs.grant.await_args.kwargs["added_by"] == "Ali"
    stubs.offer.assert_awaited_once()
    # The grant's announcement carries the line.
    interaction.channel.send.assert_not_awaited()
    assert interaction.followup.send.await_args.args[0].startswith(
        "✅ **Bo**'s deck is offered to **Sal**.")


async def test_a_failed_grant_still_offers_and_says_to_add_them(stubs):
    from library_views.sub_picker import SubPickerView
    stubs.grant.return_value = SimpleNamespace(granted=[], failed=["#draft-chat"],
                                               announced=False)
    view = await SubPickerView.build("s1", "3", False)
    interaction = _interaction()
    with _picked("5", _member(7, "Sal")):
        await view.confirm.callback(interaction)
    stubs.offer.assert_awaited_once()
    assert interaction.followup.send.await_args.args[0].endswith(
        " ⚠️ Could not grant chat access — add them with /add_sub.")


async def test_a_manager_grants_to_the_absent_players_team(stubs):
    from library_views.sub_picker import SubPickerView
    view = await SubPickerView.build("s1", "9", True)
    with _picked("4", _member(7, "Sal")):
        await view.confirm.callback(_interaction(9))
    assert stubs.grant.await_args.args[3].team_key == "B"


async def test_confirm_stops_at_a_refusal(stubs):
    from library_views.sub_picker import SubPickerView
    stubs.check.return_value = REFUSAL
    view = await SubPickerView.build("s1", "3", False)
    interaction = _interaction()
    with _picked("5", _member(3, "Ali")):
        await view.confirm.callback(interaction)
    stubs.offer.assert_not_awaited()
    stubs.grant.assert_not_awaited()
    interaction.channel.send.assert_not_awaited()
    assert interaction.followup.send.await_args.args[0] == REFUSAL


async def test_a_refused_offer_is_the_reply(stubs):
    from library_views.sub_picker import SubPickerView
    stubs.offer.return_value = REFUSAL
    view = await SubPickerView.build("s1", "3", False)
    interaction = _interaction()
    with _picked("5", _member(3, "Ali")):
        await view.confirm.callback(interaction)
    interaction.channel.send.assert_not_awaited()
    assert interaction.followup.send.await_args.args[0] == REFUSAL


async def test_the_offer_runs_under_the_panel_lock_and_the_nudge_after(stubs):
    from library_views.sub_picker import SubPickerView
    from services.library_panel import _lock_for
    seen = []

    async def check(ctx):
        seen.append(("check", _lock_for("s1").locked()))

    async def offer(ctx):
        seen.append(("offer", _lock_for("s1").locked()))
        return 42

    async def nudge(bot, session_id):
        seen.append(("nudge", _lock_for("s1").locked()))
    stubs.check.side_effect, stubs.offer.side_effect = check, offer
    stubs.nudge.side_effect = nudge
    view = await SubPickerView.build("s1", "3", False)
    with _picked("5", _member(3, "Ali")):
        await view.confirm.callback(_interaction())
    assert seen == [("check", True), ("offer", True), ("nudge", False)]


async def test_confirm_needs_both_picks(stubs):
    from library_views.sub_picker import SubPickerView
    view = await SubPickerView.build("s1", "3", False)
    interaction = _interaction()
    await view.confirm.callback(interaction)   # nothing picked yet
    interaction.response.send_message.assert_awaited_once_with(
        "Pick both first.", ephemeral=True)
    stubs.check.assert_not_awaited()
    assert not view.is_finished()


async def test_a_sub_who_is_not_a_member_is_refused(stubs):
    from library_views.sub_picker import SubPickerView
    view = await SubPickerView.build("s1", "3", False)
    interaction = _interaction()
    with _picked("5", SimpleNamespace(id=7, display_name="Sal")):
        await view.confirm.callback(interaction)
    stubs.check.assert_not_awaited()
    assert interaction.followup.send.await_args.args[0] == "📭 Pick a member of this server."


async def test_a_second_confirm_is_a_no_op(stubs):
    from library_views.sub_picker import SubPickerView
    view = await SubPickerView.build("s1", "3", False)
    with _picked("5", _member(3, "Ali")):
        await view.confirm.callback(_interaction())
        again = _interaction()
        await view.confirm.callback(again)
    assert stubs.offer.await_count == 1
    again.response.defer.assert_not_awaited()


async def test_cancel_subs_nobody(stubs):
    from library_views.sub_picker import SubPickerView
    view = await SubPickerView.build("s1", "3", False)
    interaction = _interaction()
    await view.cancel.callback(interaction)
    interaction.response.edit_message.assert_awaited_once_with(
        content="👌 Nobody subbed.", view=None)
    stubs.check.assert_not_awaited()
    assert view.is_finished()
