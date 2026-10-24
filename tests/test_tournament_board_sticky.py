"""A tournament's registration board is sticky while registration is open: the sticky
manager reposts it at the bottom of its channel and the tournament's board pointer
follows, so registrations keep editing the live copy."""
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from sqlalchemy import select

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import db_session
from database.message_management import (
    Message, StickyUpdateResult, handle_sticky_message_update)
from models.tournament import Tournament
from services.tournament_formatter import post_registration_board, update_registration_board


async def _seed_tournament(status="registration", board_message_id=None):
    async with db_session() as session:
        t = Tournament(guild_id="g1", name="Lotus Open", total_rounds=0, format="manual",
                       status=status, entry_fee=1,
                       board_channel_id="777" if board_message_id else None,
                       board_message_id=board_message_id)
        session.add(t)
        await session.flush()
        return t.id


def _discord_message(message_id, channel):
    m = MagicMock()
    m.id = message_id
    m.channel = channel
    m.guild.id = "g1"
    m.content = ""
    m.pinned = False
    m.pin = AsyncMock()
    m.delete = AsyncMock()
    return m


def _channel():
    channel = MagicMock()
    channel.id = 777
    return channel


def _bot_for(channel):
    bot = MagicMock()
    bot.fetch_channel = AsyncMock(return_value=channel)  # the sticky manager
    bot.get_channel = MagicMock(return_value=channel)  # update_registration_board
    return bot


async def _sticky_rows():
    async with db_session() as session:
        return (await session.execute(select(Message).filter_by(is_sticky=True))).scalars().all()


async def _board(t_id):
    async with db_session() as session:
        t = await session.get(Tournament, t_id)
        return t.board_channel_id, t.board_message_id


async def _make_sticky(t_id, message_id="900", view_type="tournament_board", **extra):
    async with db_session() as session:
        session.add(Message(guild_id="g1", channel_id="777", message_id=message_id, content="",
                            view_metadata={"view_type": view_type, "tournament_id": t_id, **extra},
                            is_sticky=True, message_count=10, last_activity=0.0,
                            last_update_time=0.0))


async def _force_repost(bot):
    async with db_session() as session:
        sticky = (await session.execute(select(Message).filter_by(is_sticky=True))).scalars().one()
        return await handle_sticky_message_update(sticky, bot, session, force=True)


@pytest.mark.asyncio
async def test_a_new_board_is_posted_sticky(test_db):  # noqa: F811
    t_id = await _seed_tournament()
    channel = _channel()
    channel.send = AsyncMock(return_value=_discord_message(900, channel))

    await post_registration_board(MagicMock(), channel, t_id)

    [sticky] = await _sticky_rows()
    assert sticky.message_id == "900" and sticky.channel_id == "777"
    assert sticky.view_metadata == {"view_type": "tournament_board", "tournament_id": t_id}
    assert await _board(t_id) == ("777", "900")


@pytest.mark.asyncio
async def test_a_new_board_never_evicts_a_live_drafts_sticky(test_db):  # noqa: F811
    from conftest import seed_session

    t_id = await _seed_tournament()
    await seed_session("s1", stage=None)
    await _make_sticky(t_id, message_id="555", view_type="draft", draft_session_id="s1")
    channel = _channel()
    channel.send = AsyncMock(return_value=_discord_message(900, channel))

    await post_registration_board(MagicMock(), channel, t_id)

    [sticky] = await _sticky_rows()
    assert sticky.message_id == "555"
    assert await _board(t_id) == ("777", "900")  # still posted and tracked


@pytest.mark.asyncio
async def test_a_new_board_replaces_a_cancelled_drafts_leftover_sticky(test_db):  # noqa: F811
    # No draft session behind it: the sticky is a cancelled queue's leftover.
    t_id = await _seed_tournament()
    await _make_sticky(t_id, message_id="555", view_type="draft", draft_session_id="gone")
    channel = _channel()
    channel.send = AsyncMock(return_value=_discord_message(900, channel))

    await post_registration_board(MagicMock(), channel, t_id)

    [sticky] = await _sticky_rows()
    assert sticky.message_id == "900"
    assert sticky.view_metadata == {"view_type": "tournament_board", "tournament_id": t_id}


@pytest.mark.asyncio
async def test_a_repost_moves_the_board_pointer_and_deletes_the_old_board(test_db):  # noqa: F811
    t_id = await _seed_tournament(board_message_id="900")
    await _make_sticky(t_id)
    channel = _channel()
    old = _discord_message(900, channel)
    channel.send = AsyncMock(return_value=_discord_message(901, channel))
    channel.fetch_message = AsyncMock(return_value=old)
    bot = _bot_for(channel)

    assert await _force_repost(bot) == StickyUpdateResult.SUCCESS

    assert await _board(t_id) == ("777", "901")
    [sticky] = await _sticky_rows()
    assert sticky.message_id == "901"
    old.delete.assert_awaited_once()
    assert channel.send.await_args.kwargs["embed"].title.endswith("Registration open")


@pytest.mark.asyncio
async def test_the_sticky_ends_when_registration_closes(test_db):  # noqa: F811
    t_id = await _seed_tournament(status="in_progress", board_message_id="900")
    await _make_sticky(t_id)
    channel = _channel()
    channel.send = AsyncMock()
    bot = _bot_for(channel)

    assert await _force_repost(bot) == StickyUpdateResult.CLEANED_UP

    channel.send.assert_not_awaited()
    assert await _sticky_rows() == []
    assert await _board(t_id) == ("777", "900")  # the board stays where it is


@pytest.mark.asyncio
async def test_a_board_moved_by_hand_is_no_longer_reposted(test_db):  # noqa: F811
    t_id = await _seed_tournament(board_message_id="950")
    await _make_sticky(t_id, message_id="900")
    channel = _channel()
    channel.send = AsyncMock()
    bot = _bot_for(channel)

    assert await _force_repost(bot) == StickyUpdateResult.CLEANED_UP

    channel.send.assert_not_awaited()
    assert await _board(t_id) == ("777", "950")


@pytest.mark.asyncio
async def test_a_refresh_racing_a_repost_does_not_orphan_the_new_board(test_db):  # noqa: F811
    t_id = await _seed_tournament(board_message_id="900")
    channel = _channel()

    async def repost_then_miss(message_id):
        # The repost lands between the refresh reading the pointer and fetching.
        async with db_session() as session:
            (await session.get(Tournament, t_id)).board_message_id = "901"
        raise discord.NotFound(MagicMock(status=404), "gone")

    channel.fetch_message = AsyncMock(side_effect=repost_then_miss)
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)

    await update_registration_board(bot, t_id)

    assert await _board(t_id) == ("777", "901")


@pytest.mark.asyncio
async def test_a_close_landing_mid_repost_still_reaches_the_new_board(test_db):  # noqa: F811
    t_id = await _seed_tournament(board_message_id="900")
    await _make_sticky(t_id)
    channel = _channel()
    old, new = _discord_message(900, channel), _discord_message(901, channel)
    new.edit = AsyncMock()

    async def send_after_close(**kwargs):
        # The embed is already rendered; registration closes before it is sent.
        async with db_session() as session:
            (await session.get(Tournament, t_id)).status = "in_progress"
        return new

    channel.send = AsyncMock(side_effect=send_after_close)
    channel.fetch_message = AsyncMock(side_effect=lambda mid: {900: old, 901: new}[mid])
    bot = _bot_for(channel)

    assert await _force_repost(bot) == StickyUpdateResult.SUCCESS

    assert channel.send.await_args.kwargs["embed"].title.endswith("Registration open")
    redrawn = new.edit.await_args.kwargs["embed"]
    assert not redrawn.title.endswith("Registration open")


@pytest.mark.asyncio
async def test_a_new_board_is_posted_with_the_register_button(test_db):  # noqa: F811
    from register_team_wizard import RegisterTeamView

    t_id = await _seed_tournament()
    channel = _channel()
    channel.send = AsyncMock(return_value=_discord_message(900, channel))

    await post_registration_board(MagicMock(), channel, t_id)

    assert isinstance(channel.send.await_args.kwargs["view"], RegisterTeamView)


@pytest.mark.asyncio
async def test_a_repost_keeps_the_register_button(test_db):  # noqa: F811
    from register_team_wizard import RegisterTeamView

    t_id = await _seed_tournament(board_message_id="900")
    await _make_sticky(t_id)
    channel = _channel()
    channel.send = AsyncMock(return_value=_discord_message(901, channel))
    channel.fetch_message = AsyncMock(return_value=_discord_message(900, channel))

    assert await _force_repost(_bot_for(channel)) == StickyUpdateResult.SUCCESS

    assert isinstance(channel.send.await_args.kwargs["view"], RegisterTeamView)
