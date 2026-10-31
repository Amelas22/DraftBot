"""A match's booking change is announced in its room, tagging both teams."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from conftest import seed_tournament_match
from database.db_session import db_session
from helpers.matchup import Roster, Side, booking_announcement
from models.tournament import Tournament, TournamentParticipant
from services.booking import BookingChange

NEW = datetime(2026, 10, 28, 19, 30, tzinfo=timezone.utc)


def _change(old=None, new=NEW):
    return BookingChange(True, None, old, new, "Bo", 1)


async def _seed(role_a="111", role_b="222", name_a=None):
    async with db_session() as session:
        match = await seed_tournament_match(session, thread_id="900")
        (await session.get(Tournament, (await session.get(TournamentParticipant, match.team_a_participant_id)).tournament_id)).guild_id = "1"
        for pid, role in ((match.team_a_participant_id, role_a), (match.team_b_participant_id, role_b)):
            part = await session.get(TournamentParticipant, pid)
            part.role_id = role
        if name_a:
            (await session.get(TournamentParticipant, match.team_a_participant_id)).team_name = name_a
        await session.commit()
        return match.id


def _bot(existing_roles=(111, 222), thread=None):
    thread = thread or MagicMock(spec=discord.Thread)
    thread.send = AsyncMock()
    guild = MagicMock()
    guild.get_role.side_effect = lambda rid: object() if rid in existing_roles else None
    bot = MagicMock()
    bot.get_guild.return_value = guild
    bot.get_channel.return_value = thread
    return bot, thread


@pytest.mark.asyncio
async def test_booking_tags_both_team_roles_and_refreshes(test_db):
    from match_control_view import announce_match_booking

    match_id = await _seed()
    bot, thread = _bot()
    order = []
    thread.send = AsyncMock(side_effect=lambda **kw: order.append("send"))
    refresh = AsyncMock(side_effect=lambda *a: order.append("refresh"))

    with patch("match_control_view.safe_refresh_match_views", refresh):
        await announce_match_booking(bot, match_id, _change())

    sent = thread.send.call_args.kwargs
    assert sent["content"].startswith("<@&111> <@&222>")
    assert "Alpha" not in sent["content"].split("\n")[0]
    mentions = sent["allowed_mentions"]
    assert (mentions.everyone, mentions.users) == (False, False)
    assert mentions.to_dict()["roles"] == [111, 222]
    assert "roles" not in mentions.to_dict()["parse"]
    refresh.assert_awaited_once_with(bot, match_id)
    assert order == ["send", "refresh"]


@pytest.mark.asyncio
async def test_a_team_named_like_a_role_mention_pings_nobody_extra(test_db):
    from match_control_view import announce_match_booking

    match_id = await _seed(name_a="<@&999>")
    bot, thread = _bot()

    with patch("match_control_view.safe_refresh_match_views", AsyncMock()):
        await announce_match_booking(bot, match_id, _change())

    sent = thread.send.call_args.kwargs
    assert "999" not in sent["content"]
    assert sent["allowed_mentions"].to_dict()["roles"] == [111, 222]


@pytest.mark.asyncio
async def test_a_deleted_role_falls_back_to_that_sides_people(test_db):
    from match_control_view import announce_match_booking

    match_id = await _seed()
    bot, thread = _bot(existing_roles=(222,))
    people = (Roster("Alpha", (("1", "Ann"), ("11", "Al"))), Roster("Bravo", (("2", "Bea"),)))

    with patch("match_control_view.safe_refresh_match_views", AsyncMock()), \
         patch("match_control_view.match_side_rosters", AsyncMock(return_value=people)):
        await announce_match_booking(bot, match_id, _change())

    sent = thread.send.call_args.kwargs
    assert sent["content"].startswith("<@1> <@11> <@&222>")
    mentions = sent["allowed_mentions"].to_dict()
    assert mentions["roles"] == [222]
    assert mentions["users"] == [1, 11]


@pytest.mark.asyncio
async def test_a_role_that_was_never_made_falls_back_too(test_db):
    from match_control_view import announce_match_booking

    match_id = await _seed(role_a=None)
    bot, thread = _bot()
    people = (Roster("Alpha", (("1", "Ann"),)), Roster("Bravo", (("2", "Bea"),)))

    with patch("match_control_view.safe_refresh_match_views", AsyncMock()), \
         patch("match_control_view.match_side_rosters", AsyncMock(return_value=people)):
        await announce_match_booking(bot, match_id, _change())

    sent = thread.send.call_args.kwargs
    assert sent["content"].startswith("<@1> <@&222>")
    assert sent["allowed_mentions"].to_dict()["users"] == [1]


@pytest.mark.asyncio
async def test_rebooking_the_same_time_only_refreshes(test_db):
    from match_control_view import announce_match_booking

    match_id = await _seed()
    bot, thread = _bot()
    refresh = AsyncMock()

    with patch("match_control_view.safe_refresh_match_views", refresh):
        await announce_match_booking(bot, match_id, _change(old=NEW, new=NEW))

    thread.send.assert_not_awaited()
    refresh.assert_awaited_once_with(bot, match_id)


@pytest.mark.asyncio
async def test_a_move_and_a_clear_are_announced(test_db):
    from match_control_view import announce_match_booking

    match_id = await _seed()
    bot, thread = _bot()
    old = datetime(2026, 10, 27, 19, 30, tzinfo=timezone.utc)

    with patch("match_control_view.safe_refresh_match_views", AsyncMock()):
        await announce_match_booking(bot, match_id, _change(old=old, new=NEW))
        await announce_match_booking(bot, match_id, _change(old=NEW, new=None))

    moved, cleared = [c.kwargs["content"] for c in thread.send.call_args_list]
    a, b = Side("Alpha", ("111",), ()), Side("Bravo", ("222",), ())
    assert moved == booking_announcement("Bo", old, NEW, a, b)
    assert cleared == booking_announcement("Bo", NEW, None, a, b)


@pytest.mark.asyncio
async def test_a_refused_send_is_logged_and_the_control_still_refreshes(test_db):
    from match_control_view import announce_match_booking

    match_id = await _seed()
    bot, thread = _bot()
    thread.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "Missing Access"))
    refresh = AsyncMock()

    with patch("match_control_view.safe_refresh_match_views", refresh):
        await announce_match_booking(bot, match_id, _change())

    refresh.assert_awaited_once_with(bot, match_id)


@pytest.mark.asyncio
async def test_an_unreachable_room_is_logged_and_not_raised(test_db):
    from match_control_view import announce_match_booking

    match_id = await _seed()
    bot, _thread = _bot()
    bot.get_channel.return_value = None
    bot.fetch_channel = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "Unknown Channel"))

    with patch("match_control_view.safe_refresh_match_views", AsyncMock()):
        await announce_match_booking(bot, match_id, _change())

    bot.fetch_channel.assert_awaited_once_with(900)


@pytest.mark.asyncio
async def test_the_cog_hands_the_api_the_match_announcer():
    from cogs.scheduler_web_cog import SchedulerWebCog
    from match_control_view import announce_match_booking

    cog = object.__new__(SchedulerWebCog)  # skip __init__, which schedules _start itself
    cog.bot = MagicMock()
    with patch("cogs.scheduler_web_cog.build_app", MagicMock(side_effect=RuntimeError("stop"))) as build:
        with pytest.raises(RuntimeError):
            await cog._start()

    hook = build.call_args.kwargs["match_announce"]
    assert (hook.func, hook.args) == (announce_match_booking, (cog.bot,))
