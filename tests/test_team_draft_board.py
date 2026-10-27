"""The Set up a team draft board: /team_draft_board posts it once in #set-team-draft,
and the sticky manager reposts it, button and all, while the scheduler flag is on."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from cogs import team_draft_cog
from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import db_session
from database.message_management import (
    Message, StickyUpdateResult, handle_sticky_message_update)
from team_draft_wizard import TeamDraftBoardView


def _discord_message(message_id, channel):
    m = MagicMock()
    m.id = message_id
    m.channel = channel
    m.guild.id = "1"
    m.content = ""
    m.pinned = False
    m.pin = AsyncMock()
    m.delete = AsyncMock()
    return m


def _channel():
    channel = MagicMock()
    channel.id = 777
    return channel


def _ctx():
    ctx = MagicMock()
    ctx.guild.id = 1
    ctx.defer = AsyncMock()
    ctx.respond = AsyncMock()
    ctx.followup.send = AsyncMock()
    return ctx


async def _run_command(channel, enabled=True):
    found = AsyncMock(return_value=(channel, False))
    ctx = _ctx()
    cog = team_draft_cog.TeamDraftCog(MagicMock())
    with patch.object(team_draft_cog, "scheduler_enabled", return_value=enabled), \
            patch.object(team_draft_cog, "get_config", return_value={}), \
            patch.object(team_draft_cog, "update_setting") as update, \
            patch.object(team_draft_cog, "ensure_channel", new=found):
        await team_draft_cog.TeamDraftCog.team_draft_board.callback(cog, ctx)
    return ctx, found, update


async def _sticky_rows():
    async with db_session() as session:
        return (await session.execute(select(Message).filter_by(is_sticky=True))).scalars().all()


async def _force_repost(bot):
    async with db_session() as session:
        sticky = (await session.execute(select(Message).filter_by(is_sticky=True))).scalars().one()
        return await handle_sticky_message_update(sticky, bot, session, force=True)


def _last_reply(ctx):
    return ctx.followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_the_board_command_posts_one_sticky_board(test_db):  # noqa: F811
    from helpers.tournament_channels import SET_TEAM_DRAFT

    channel = _channel()
    board = _discord_message(900, channel)
    channel.send = AsyncMock(return_value=board)

    ctx, found, update = await _run_command(channel)
    await _run_command(channel)

    assert found.await_args.args[2] is SET_TEAM_DRAFT
    assert SET_TEAM_DRAFT.name == "set-team-draft" and SET_TEAM_DRAFT.read_only is False
    update.assert_called_once_with("1", "tournament.default_team_draft_channel_id", "777")
    channel.send.assert_awaited_once()
    sent = channel.send.await_args.kwargs
    assert sent["embed"].title == "Set up a team draft"
    assert isinstance(sent["view"], TeamDraftBoardView)
    [sticky] = await _sticky_rows()
    assert (sticky.channel_id, sticky.message_id) == ("777", "900")
    assert sticky.view_metadata == {"view_type": "team_draft_board"}
    board.pin.assert_awaited_once()
    assert "<#777>" in _last_reply(ctx)
    assert ctx.defer.await_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_the_board_command_says_not_turned_on_when_the_flag_is_off(test_db):  # noqa: F811
    channel = _channel()
    channel.send = AsyncMock()

    ctx, found, _update = await _run_command(channel, enabled=False)

    found.assert_not_awaited()
    channel.send.assert_not_awaited()
    ctx.respond.assert_awaited_once_with(
        "Team drafts aren't turned on in this server.", ephemeral=True)


async def _make_sticky():
    async with db_session() as session:
        session.add(Message(guild_id="1", channel_id="777", message_id="900", content="",
                            view_metadata={"view_type": "team_draft_board"}, is_sticky=True,
                            message_count=10, last_activity=0.0, last_update_time=0.0))


@pytest.mark.asyncio
async def test_a_repost_carries_the_button(test_db):  # noqa: F811
    await _make_sticky()
    channel = _channel()
    old = _discord_message(900, channel)
    channel.send = AsyncMock(return_value=_discord_message(901, channel))
    channel.fetch_message = AsyncMock(return_value=old)
    bot = MagicMock()
    bot.fetch_channel = AsyncMock(return_value=channel)

    with patch("config.scheduler_enabled", return_value=True):
        assert await _force_repost(bot) == StickyUpdateResult.SUCCESS

    sent = channel.send.await_args.kwargs
    assert sent["embed"].title == "Set up a team draft"
    assert isinstance(sent["view"], TeamDraftBoardView)
    [sticky] = await _sticky_rows()
    assert sticky.message_id == "901"
    old.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_sticky_ends_when_the_flag_is_turned_off(test_db):  # noqa: F811
    await _make_sticky()
    channel = _channel()
    channel.send = AsyncMock()
    bot = MagicMock()
    bot.fetch_channel = AsyncMock(return_value=channel)

    with patch("config.scheduler_enabled", return_value=False):
        assert await _force_repost(bot) == StickyUpdateResult.CLEANED_UP

    assert await _sticky_rows() == []
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_board_button_has_a_stable_custom_id():
    view = TeamDraftBoardView()
    assert view.timeout is None
    assert view.setup_button.custom_id == "team_draft:setup"
    assert view.setup_button.label == "Set up a team draft"
