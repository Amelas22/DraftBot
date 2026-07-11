import discord
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.draft_commands import DraftCommands


def make_channel(channel_id, name):
    channel = MagicMock()
    channel.id = channel_id
    channel.name = name
    channel.mention = f"#{name}"
    channel.set_permissions = AsyncMock()
    channel.send = AsyncMock()
    return channel


def make_ctx(invoker_id, channels):
    ctx = MagicMock()
    ctx.author.id = int(invoker_id)
    ctx.author.display_name = f"user-{invoker_id}"
    ctx.channel_id = 100
    ctx.followup.send = AsyncMock()
    ctx.guild.get_channel = lambda cid: channels.get(cid)
    return ctx


def make_sub(user_id=9):
    sub = MagicMock()
    sub.id = user_id
    sub.display_name = "SubGuy"
    sub.mention = f"<@{user_id}>"
    return sub


def make_draft(draft_id="abc123", channel_ids=None, draft_chat_channel="100",
               team_a=("1",), team_b=("2",)):
    return SimpleNamespace(
        draft_id=draft_id,
        session_id="sess_123",
        channel_ids=channel_ids if channel_ids is not None else [100, 101, 102],
        draft_chat_channel=draft_chat_channel,
        team_a=list(team_a),
        team_b=list(team_b),
        sign_ups={"1": "One", "2": "Two"},
        team_a_name=None,
        team_b_name=None,
    )


@pytest.fixture
def cog():
    return DraftCommands(bot=MagicMock())


# Channel names as Discord stores them: text channels lowercased
def standard_channels(draft_id="abc123"):
    return {
        100: make_channel(100, f"draft-chat-{draft_id}"),
        101: make_channel(101, f"red-team-chat-{draft_id}"),
        102: make_channel(102, f"blue-team-chat-{draft_id}"),
    }


@pytest.mark.asyncio
async def test_player_grants_sub_draft_chat_and_own_team_chat(cog):
    channels = standard_channels()
    ctx = make_ctx("1", channels)  # invoker on team_a
    sub = make_sub()
    draft = make_draft()
    with patch("cogs.draft_commands.DraftSession.get_by_any_channel_id",
               AsyncMock(return_value=draft)), \
         patch("cogs.draft_commands.is_bot_manager", AsyncMock(return_value=False)):
        await cog._do_add_sub(ctx, sub, None)

    # Draft chat + red team chat get overwrites, blue team chat does not
    channels[100].set_permissions.assert_awaited_once_with(
        sub, read_messages=True, manage_messages=True)
    channels[101].set_permissions.assert_awaited_once_with(
        sub, read_messages=True, manage_messages=True)
    channels[102].set_permissions.assert_not_awaited()

    # Public announcement in draft chat, ephemeral confirmation to invoker
    channels[100].send.assert_awaited_once()
    assert sub.mention in channels[100].send.await_args.args[0]
    ctx.followup.send.assert_awaited_once()
    assert ctx.followup.send.await_args.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_admin_outside_draft_grants_chosen_team(cog):
    channels = standard_channels()
    ctx = make_ctx("99", channels)  # not in draft
    sub = make_sub()
    draft = make_draft()
    with patch("cogs.draft_commands.DraftSession.get_by_any_channel_id",
               AsyncMock(return_value=draft)), \
         patch("cogs.draft_commands.is_bot_manager", AsyncMock(return_value=True)):
        await cog._do_add_sub(ctx, sub, "B")

    channels[100].set_permissions.assert_awaited_once()
    channels[102].set_permissions.assert_awaited_once()
    channels[101].set_permissions.assert_not_awaited()


@pytest.mark.asyncio
async def test_not_a_draft_channel_is_ephemeral_error(cog):
    ctx = make_ctx("1", {})
    with patch("cogs.draft_commands.DraftSession.get_by_any_channel_id",
               AsyncMock(return_value=None)):
        await cog._do_add_sub(ctx, make_sub(), None)
    ctx.followup.send.assert_awaited_once()
    assert ctx.followup.send.await_args.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_non_participant_non_admin_gets_error_and_no_grants(cog):
    channels = standard_channels()
    ctx = make_ctx("99", channels)
    draft = make_draft()
    with patch("cogs.draft_commands.DraftSession.get_by_any_channel_id",
               AsyncMock(return_value=draft)), \
         patch("cogs.draft_commands.is_bot_manager", AsyncMock(return_value=False)):
        await cog._do_add_sub(ctx, make_sub(), None)
    for channel in channels.values():
        channel.set_permissions.assert_not_awaited()
    ctx.followup.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_deleted_channels_reported_as_error(cog):
    # Session exists but none of its channels resolve in the guild anymore
    ctx = make_ctx("1", {})
    draft = make_draft()
    with patch("cogs.draft_commands.DraftSession.get_by_any_channel_id",
               AsyncMock(return_value=draft)), \
         patch("cogs.draft_commands.is_bot_manager", AsyncMock(return_value=False)):
        await cog._do_add_sub(ctx, make_sub(), None)
    ctx.followup.send.assert_awaited_once()
    assert "deleted" in ctx.followup.send.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_no_channel_ids_yet_is_error(cog):
    ctx = make_ctx("1", {})
    draft = make_draft(channel_ids=[])
    with patch("cogs.draft_commands.DraftSession.get_by_any_channel_id",
               AsyncMock(return_value=draft)):
        await cog._do_add_sub(ctx, make_sub(), None)
    ctx.followup.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_partial_discord_failure_is_reported(cog):
    channels = standard_channels()
    channels[101].set_permissions = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=500), "boom"))
    ctx = make_ctx("1", channels)
    draft = make_draft()
    with patch("cogs.draft_commands.DraftSession.get_by_any_channel_id",
               AsyncMock(return_value=draft)), \
         patch("cogs.draft_commands.is_bot_manager", AsyncMock(return_value=False)):
        await cog._do_add_sub(ctx, make_sub(), None)
    message = ctx.followup.send.await_args.args[0]
    assert "failed" in message.lower()


@pytest.mark.asyncio
async def test_draft_chat_failure_skips_announcement_and_notes_it(cog):
    channels = standard_channels()
    channels[100].set_permissions = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=500), "boom"))
    ctx = make_ctx("1", channels)
    draft = make_draft()
    with patch("cogs.draft_commands.DraftSession.get_by_any_channel_id",
               AsyncMock(return_value=draft)), \
         patch("cogs.draft_commands.is_bot_manager", AsyncMock(return_value=False)):
        await cog._do_add_sub(ctx, make_sub(), None)
    channels[100].send.assert_not_awaited()
    message = ctx.followup.send.await_args.args[0]
    assert "announcement" in message.lower()


@pytest.mark.asyncio
async def test_all_grants_failing_reports_could_not_grant(cog):
    channels = standard_channels()
    for channel in channels.values():
        channel.set_permissions = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(status=500), "boom"))
    ctx = make_ctx("1", channels)
    draft = make_draft()
    with patch("cogs.draft_commands.DraftSession.get_by_any_channel_id",
               AsyncMock(return_value=draft)), \
         patch("cogs.draft_commands.is_bot_manager", AsyncMock(return_value=False)):
        await cog._do_add_sub(ctx, make_sub(), None)
    message = ctx.followup.send.await_args.args[0]
    assert "could not grant" in message.lower()
    assert not message.lower().startswith("granted")
