"""The Set up a team draft wizard, driven through its picker, buttons and modal
against a real test database. Discord itself is mocked; the room opener is real."""
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from sqlalchemy import select

import team_draft_wizard as wiz
from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import db_session
from models.team_draft import TeamDraft

GUILD = 1
ORGANISER, ASH, BO, CY = 300, 201, 202, 203


def _member(user_id, name, bot=False):
    m = MagicMock()
    m.id = user_id
    m.bot = bot
    m.display_name = name
    return m


def _role(role_id, name, members):
    r = MagicMock(spec=discord.Role)
    r.id = role_id
    r.name = name
    r.members = members
    return r


MEMBERS = {uid: _member(uid, name) for uid, name in
           ((ORGANISER, "Organiser"), (ASH, "Ash"), (BO, "Bo"), (CY, "Cy"))}
GARRUK = _role(111, "Team Garruk", [MEMBERS[CY]])
ROLES = {111: GARRUK}


def _guild():
    guild = MagicMock()
    guild.id = GUILD
    guild.get_role.side_effect = ROLES.get
    guild.get_member.side_effect = MEMBERS.get
    return guild


GUILD_OBJ = _guild()


def _board_channel():
    thread = MagicMock()
    thread.id = 900
    control = MagicMock()
    control.id = 777
    control.pin = AsyncMock()
    thread.send = AsyncMock(return_value=control)
    thread.delete = AsyncMock()
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 50
    channel.guild = GUILD_OBJ
    channel.create_thread = AsyncMock(return_value=thread)
    return channel


def _interaction(channel=None):
    i = MagicMock()
    i.user = MEMBERS[ORGANISER]
    i.guild = GUILD_OBJ
    i.guild_id = GUILD
    i.channel = channel if channel is not None else _board_channel()
    i.response.send_modal = AsyncMock()
    i.response.send_message = AsyncMock()
    i.response.edit_message = AsyncMock()
    i.response.defer = AsyncMock()
    i.edit_original_response = AsyncMock()
    return i


def _sent(i):
    """(content, view) of the latest reply through ``i``."""
    for call in (i.edit_original_response, i.response.edit_message, i.response.send_message):
        if call.await_args is not None:
            kwargs = call.await_args.kwargs
            content = kwargs.get("content", call.await_args.args[0] if call.await_args.args else None)
            return content, kwargs.get("view")
    raise AssertionError("nothing was sent")


@pytest.fixture
def enabled():
    with patch.object(wiz, "scheduler_enabled", return_value=True) as flag:
        yield flag


async def _pick(view, values):
    picker = next(item for item in view.children
                  if isinstance(item, wiz.PrefilledMentionableSelect))
    i = _interaction(view.state.channel)
    with patch.object(type(picker), "values", new=values):
        await picker.callback(i)
    return i


def _button(view, label):
    return next(item for item in view.children
                if isinstance(item, discord.ui.Button) and item.label == label)


async def _press(view, label):
    i = _interaction(view.state.channel)
    await _button(view, label).callback(i)
    return i


async def _to_confirm(a_values, b_values):
    start = _interaction()
    await wiz.start_setup(start)
    _content, side_a = _sent(start)
    to_b = await _pick(side_a, a_values)
    _content, side_b = _sent(to_b)
    return await _pick(side_b, b_values)


async def _rows():
    async with db_session() as session:
        return (await session.execute(select(TeamDraft))).scalars().all()


@pytest.mark.asyncio
async def test_the_board_button_starts_the_wizard_ephemerally(test_db, enabled):  # noqa: F811
    i = _interaction()
    await wiz.TeamDraftBoardView().setup_button.callback(i)

    i.response.edit_message.assert_not_awaited()  # never rewrite the board
    assert i.response.send_message.await_args.kwargs["ephemeral"] is True
    content, view = _sent(i)
    assert "Step 1 of 3" in content
    picker = next(item for item in view.children
                  if isinstance(item, wiz.PrefilledMentionableSelect))
    payload = picker.to_component_dict()
    assert payload["type"] == discord.ComponentType.mentionable_select.value
    assert (payload["min_values"], payload["max_values"]) == (1, 5)


@pytest.mark.asyncio
async def test_picking_both_sides_reaches_confirm_with_the_default_name(test_db, enabled):  # noqa: F811
    confirm = await _to_confirm([GARRUK], [MEMBERS[ASH], MEMBERS[BO]])

    content, view = _sent(confirm)
    assert "Step 3 of 3" in content
    assert "Team Garruk vs Ash, Bo" in content
    assert "<@&111>" in content and "<@201>" in content and "<@202>" in content
    assert view.state.a.role_ids == ("111",) and view.state.b.user_ids == ("201", "202")
    assert {item.label for item in view.children
            if isinstance(item, discord.ui.Button)} == {"Create", "Rename", "Back"}
    assert await _rows() == []  # nothing written before Create


@pytest.mark.asyncio
async def test_back_reopens_a_side_with_its_picks_prefilled(test_db, enabled):  # noqa: F811
    confirm = await _to_confirm([GARRUK], [MEMBERS[ASH]])
    _content, view = _sent(confirm)

    back = await _press(view, "Back")

    content, side_b = _sent(back)
    assert "Step 2 of 3" in content
    picker = next(item for item in side_b.children
                  if isinstance(item, wiz.PrefilledMentionableSelect))
    assert picker.to_component_dict()["default_values"] == [{"id": "201", "type": "user"}]
    first = await _press(side_b, "Back")
    _content, side_a = _sent(first)
    picker = next(item for item in side_a.children
                  if isinstance(item, wiz.PrefilledMentionableSelect))
    assert picker.to_component_dict()["default_values"] == [{"id": "111", "type": "role"}]


@pytest.mark.asyncio
async def test_rename_sets_the_thread_name(test_db, enabled):  # noqa: F811
    confirm = await _to_confirm([GARRUK], [MEMBERS[ASH], MEMBERS[BO]])
    _content, view = _sent(confirm)

    rename = await _press(view, "Rename")
    modal = rename.response.send_modal.await_args.args[0]
    assert modal.children[0].max_length == 100
    modal.children[0].value = "  Friday showdown  "
    renamed = _interaction(view.state.channel)
    await modal.callback(renamed)
    content, view = _sent(renamed)
    assert "Friday showdown" in content

    created = await _press(view, "Create")

    channel = view.state.channel
    assert channel.create_thread.await_args.kwargs["name"] == "Friday showdown"
    [row] = await _rows()
    assert row.name == "Friday showdown"
    assert "<#900>" in _sent(created)[0]


@pytest.mark.asyncio
async def test_a_blank_rename_goes_back_to_the_default_name(test_db, enabled):  # noqa: F811
    confirm = await _to_confirm([GARRUK], [MEMBERS[ASH]])
    _content, view = _sent(confirm)
    view.state.name = "Old name"

    rename = await _press(view, "Rename")
    modal = rename.response.send_modal.await_args.args[0]
    modal.children[0].value = "   "
    renamed = _interaction(view.state.channel)
    await modal.callback(renamed)

    content, view = _sent(renamed)
    assert view.state.name == ""
    assert "Team Garruk vs Ash" in content


@pytest.mark.asyncio
async def test_create_refuses_overlapping_sides_and_stays_on_the_step(test_db, enabled):  # noqa: F811
    # Cy is in Team Garruk and also named on side B.
    confirm = await _to_confirm([GARRUK], [MEMBERS[ASH], MEMBERS[CY]])
    _content, view = _sent(confirm)

    created = await _press(view, "Create")

    content, again = _sent(created)
    assert "1 person is on both sides" in content
    assert "Step 3 of 3" in content
    assert isinstance(again, wiz.ConfirmView)
    view.state.channel.create_thread.assert_not_awaited()
    assert await _rows() == []


@pytest.mark.asyncio
async def test_create_opens_the_room_and_links_it(test_db, enabled):  # noqa: F811
    confirm = await _to_confirm([GARRUK], [MEMBERS[ASH], MEMBERS[BO]])
    _content, view = _sent(confirm)

    created = await _press(view, "Create")

    # The buttons go first, so a second press can't make a second matchup.
    assert created.response.edit_message.await_args.kwargs["view"] is None
    content, final_view = _sent(created)
    assert content == "Your team draft is ready: <#900>"
    assert final_view is None
    channel = view.state.channel
    assert channel.create_thread.await_args.kwargs["name"] == "Team Garruk vs Ash, Bo"
    [row] = await _rows()
    assert (row.guild_id, row.channel_id, row.created_by) == ("1", "50", "300")
    assert (row.side_a_roles, row.side_a_users) == (["111"], [])
    assert (row.side_b_roles, row.side_b_users) == ([], ["201", "202"])
    assert (row.thread_id, row.control_message_id) == ("900", "777")


@pytest.mark.asyncio
async def test_a_room_that_cannot_open_leaves_no_matchup(test_db, enabled):  # noqa: F811
    confirm = await _to_confirm([GARRUK], [MEMBERS[ASH]])
    _content, view = _sent(confirm)
    view.state.channel.create_thread = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=403), "Missing Permissions"))

    created = await _press(view, "Create")

    assert "couldn't open" in _sent(created)[0].lower()
    assert await _rows() == []


@pytest.mark.asyncio
async def test_flag_off_says_not_turned_on(test_db):  # noqa: F811
    i = _interaction()
    with patch.object(wiz, "scheduler_enabled", return_value=False):
        await wiz.TeamDraftBoardView().setup_button.callback(i)

    i.response.send_message.assert_awaited_once_with(
        "Team drafts aren't turned on in this server.", ephemeral=True)


@pytest.mark.asyncio
async def test_start_draft_is_refused_while_the_flag_is_off(test_db):  # noqa: F811
    import team_draft_view

    i = _interaction()
    with patch.object(team_draft_view, "scheduler_enabled", return_value=False), \
            patch("modals.CubeDraftSelectionView") as picker:
        await team_draft_view.start_team_draft(i, 1)

    picker.assert_not_called()
    i.response.send_message.assert_awaited_once_with(
        "Team drafts aren't turned on in this server.", ephemeral=True)
