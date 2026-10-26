"""The Find a team board is sticky while the server's scheduler flag is on: the sticky
manager reposts it, button and all, at the bottom of its channel."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from sqlalchemy import select

from cogs import lfg_cog
from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import db_session
from database.message_management import (
    Message, StickyUpdateResult, handle_sticky_message_update)


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


async def _ensure(channel, enabled=True):
    """Run ensure_lfg_board with the channel lookup resolving to ``channel``."""
    guild = MagicMock()
    guild.id = 1
    found = AsyncMock(return_value=(channel, False))
    with patch.object(lfg_cog, "scheduler_enabled", return_value=enabled), \
            patch.object(lfg_cog, "get_config", return_value={}), \
            patch.object(lfg_cog, "update_setting") as update, \
            patch.object(lfg_cog, "ensure_channel", new=found):
        await lfg_cog.ensure_lfg_board(MagicMock(), guild, None)
    return found, update


async def _sticky_rows():
    async with db_session() as session:
        return (await session.execute(select(Message).filter_by(is_sticky=True))).scalars().all()


async def _make_sticky(message_id="900", **metadata):
    async with db_session() as session:
        session.add(Message(guild_id="1", channel_id="777", message_id=message_id, content="",
                            view_metadata=metadata, is_sticky=True, message_count=10,
                            last_activity=0.0, last_update_time=0.0))


async def _force_repost(bot):
    async with db_session() as session:
        sticky = (await session.execute(select(Message).filter_by(is_sticky=True))).scalars().one()
        return await handle_sticky_message_update(sticky, bot, session, force=True)


@pytest.mark.asyncio
async def test_opening_registration_posts_the_board_sticky_in_find_a_team(test_db):  # noqa: F811
    from helpers.tournament_channels import FIND_A_TEAM

    channel = _channel()
    board = _discord_message(900, channel)
    channel.send = AsyncMock(return_value=board)

    found, update = await _ensure(channel)

    assert found.await_args.args[2] is FIND_A_TEAM
    update.assert_called_once_with("1", "tournament.default_lfg_channel_id", "777")
    [sticky] = await _sticky_rows()
    assert (sticky.channel_id, sticky.message_id) == ("777", "900")
    assert sticky.view_metadata == {"view_type": "lfg_board"}
    assert isinstance(channel.send.await_args.kwargs["view"], lfg_cog.LfgBoardView)
    board.pin.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_next_tournament_does_not_post_a_second_board(test_db):  # noqa: F811
    await _make_sticky(view_type="lfg_board")
    channel = _channel()
    channel.send = AsyncMock()

    await _ensure(channel)

    channel.send.assert_not_awaited()
    [sticky] = await _sticky_rows()
    assert sticky.message_id == "900"


@pytest.mark.asyncio
async def test_no_board_while_the_flag_is_off(test_db):  # noqa: F811
    channel = _channel()
    channel.send = AsyncMock()

    found, _update = await _ensure(channel, enabled=False)

    found.assert_not_awaited()
    channel.send.assert_not_awaited()
    assert await _sticky_rows() == []


@pytest.mark.asyncio
async def test_a_repost_carries_the_button_and_deletes_the_old_board(test_db):  # noqa: F811
    await _make_sticky(view_type="lfg_board")
    channel = _channel()
    old = _discord_message(900, channel)
    channel.send = AsyncMock(return_value=_discord_message(901, channel))
    channel.fetch_message = AsyncMock(return_value=old)
    bot = MagicMock()
    bot.fetch_channel = AsyncMock(return_value=channel)

    with patch("config.scheduler_enabled", return_value=True):
        assert await _force_repost(bot) == StickyUpdateResult.SUCCESS

    sent = channel.send.await_args.kwargs
    assert sent["embed"].title == "Find a team"
    assert isinstance(sent["view"], lfg_cog.LfgBoardView)
    [sticky] = await _sticky_rows()
    assert sticky.message_id == "901"
    old.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_sticky_ends_when_the_flag_is_turned_off(test_db):  # noqa: F811
    await _make_sticky(view_type="lfg_board")
    channel = _channel()
    channel.send = AsyncMock()
    bot = MagicMock()
    bot.fetch_channel = AsyncMock(return_value=channel)

    with patch("config.scheduler_enabled", return_value=False):
        assert await _force_repost(bot) == StickyUpdateResult.CLEANED_UP

    assert await _sticky_rows() == []
    channel.send.assert_not_awaited()


async def _open_team(captain="cap", name="Rook"):
    from services.team_finder import open_team
    async with db_session() as session:
        assert (await open_team(session, "1", captain, name, 3, "", NOW)).ok
        await session.commit()


NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_the_first_board_and_every_repost_list_the_open_teams(test_db):  # noqa: F811
    await _open_team()
    channel = _channel()
    channel.send = AsyncMock(return_value=_discord_message(900, channel))
    await _ensure(channel)
    assert "**Rook's team** · 1/3 · needs 2" in channel.send.await_args.kwargs["embed"].description

    channel.send = AsyncMock(return_value=_discord_message(901, channel))
    channel.fetch_message = AsyncMock(return_value=_discord_message(900, channel))
    bot = MagicMock()
    bot.fetch_channel = AsyncMock(return_value=channel)
    with patch("config.scheduler_enabled", return_value=True):
        assert await _force_repost(bot) == StickyUpdateResult.SUCCESS
    assert "**Rook's team**" in channel.send.await_args.kwargs["embed"].description


def _refresh_bot(board):
    channel = MagicMock(spec=discord.TextChannel)  # refresh narrows the fetched channel
    channel.fetch_message = AsyncMock(return_value=board)
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=None)
    bot.fetch_channel = AsyncMock(return_value=channel)
    return bot


def _board_channel(channel_id="777"):
    """The server's configured find-a-team channel, as ensure_lfg_board records it."""
    return patch.object(lfg_cog, "get_config",
                        return_value={"tournament": {"default_lfg_channel_id": channel_id}})


@pytest.mark.asyncio
async def test_the_board_is_edited_in_place_only_when_its_list_changes(test_db):  # noqa: F811
    await _make_sticky(view_type="lfg_board")
    board = _discord_message(900, _channel())
    board.edit = AsyncMock()
    bot = _refresh_bot(board)
    lfg_cog.forget_rendered_boards()

    with _board_channel():
        await _refresh_three_times(bot, board)


async def _refresh_three_times(bot, board):
    await lfg_cog.refresh_lfg_board(bot, "1")
    assert lfg_cog.NO_TEAMS in board.edit.await_args.kwargs["embed"].description
    await lfg_cog.refresh_lfg_board(bot, "1")
    assert board.edit.await_count == 1

    await _open_team()
    await lfg_cog.refresh_lfg_board(bot, "1")
    assert board.edit.await_count == 2
    assert "**Rook's team**" in board.edit.await_args.kwargs["embed"].description


@pytest.mark.asyncio
async def test_a_board_left_in_an_old_channel_is_not_the_one_refreshed(test_db):  # noqa: F811
    async with db_session() as session:
        session.add(Message(guild_id="1", channel_id="555", message_id="500", content="",
                            view_metadata={"view_type": "lfg_board"}, is_sticky=True,
                            message_count=0, last_activity=0.0, last_update_time=0.0))
    await _make_sticky(view_type="lfg_board")
    board = _discord_message(900, _channel())
    board.edit = AsyncMock()
    bot = _refresh_bot(board)
    lfg_cog.forget_rendered_boards()

    with _board_channel("777"):
        await lfg_cog.refresh_lfg_board(bot, "1")

    bot.fetch_channel.assert_awaited_once_with(777)
    board.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_deleted_board_stops_being_refreshed(test_db):  # noqa: F811
    await _make_sticky(view_type="lfg_board")
    channel = MagicMock(spec=discord.TextChannel)
    channel.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=None)
    bot.fetch_channel = AsyncMock(return_value=channel)
    lfg_cog.forget_rendered_boards()

    with _board_channel():
        await lfg_cog.refresh_lfg_board(bot, "1")
        assert await _sticky_rows() == []
        await lfg_cog.refresh_lfg_board(bot, "1")

    channel.fetch_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_reposted_board_is_not_edited_again_for_the_same_list(test_db):  # noqa: F811
    await _make_sticky(view_type="lfg_board")
    channel = _channel()
    new = _discord_message(901, channel)
    new.edit = AsyncMock()

    def sent(**kwargs):
        new.embeds = [kwargs["embed"]]  # a sent discord.Message carries its embed
        return new
    channel.send = AsyncMock(side_effect=sent)
    channel.fetch_message = AsyncMock(side_effect=[_discord_message(900, channel), new])
    bot = MagicMock()
    bot.fetch_channel = AsyncMock(return_value=channel)
    lfg_cog.forget_rendered_boards()
    with patch("config.scheduler_enabled", return_value=True):
        assert await _force_repost(bot) == StickyUpdateResult.SUCCESS

    with _board_channel():
        await lfg_cog.refresh_lfg_board(bot, "1")

    new.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_board_means_nothing_to_refresh(test_db):  # noqa: F811
    bot = MagicMock()
    bot.fetch_channel = AsyncMock()
    with _board_channel():
        await lfg_cog.refresh_lfg_board(bot, "1")
    with patch.object(lfg_cog, "get_config", return_value={}):
        await lfg_cog.refresh_lfg_board(bot, "1")
    bot.fetch_channel.assert_not_awaited()
