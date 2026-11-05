"""A tournament draft's rooms and its match thread point at each other."""
from contextlib import asynccontextmanager
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

LOBBY = "https://discord.com/channels/1/2/3"


def _fake_db_session():
    @asynccontextmanager
    async def fake():
        yield MagicMock()
    return fake


def _match(thread_id="900", control_id="777"):
    match = MagicMock()
    match.id = 143
    match.team_a_wins = None
    match.team_b_wins = None
    match.thread_id = thread_id
    match.control_message_id = control_id
    match.pairings_channel_id = None
    match.pairings_message_id = None
    match.booked_start_utc = None
    match.booked_by_name = None
    return match


def _draft(rooms=True):
    draft = MagicMock()
    draft.guild_id = "1"
    draft.draft_channel_id = "2"
    draft.message_id = "3"
    draft.draft_chat_channel = "555"
    draft.rooms_created_at = datetime(2026, 11, 5) if rooms else None
    return draft


def _thread():
    thread = MagicMock()
    thread.id = 900
    thread.send = AsyncMock()
    thread.fetch_message = AsyncMock()
    return thread


def test_control_body_points_at_the_room_once_rooms_exist():
    from match_control_view import control_body_and_view
    body, _view = control_body_and_view(_match(), "Alpha", "Bravo", "Week 3", _draft(rooms=True))
    assert "<#555>" in body and LOBBY not in body


def test_control_body_links_the_lobby_until_rooms_exist():
    from match_control_view import control_body_and_view
    body, _view = control_body_and_view(_match(), "Alpha", "Bravo", "Week 3", _draft(rooms=False))
    assert LOBBY in body and "<#555>" not in body


@pytest.mark.asyncio
async def test_room_header_is_posted_pinned_and_set_as_topic():
    import match_control_view

    channel = MagicMock(spec=discord.TextChannel)
    posted = MagicMock()
    channel.send = AsyncMock(return_value=posted)
    channel.edit = AsyncMock()
    channel.pins = AsyncMock(return_value=[])
    facts = (_match(thread_id="900"), "Alpha", "Bravo", "Week 3", _draft())

    with patch.object(match_control_view, "match_facts", AsyncMock(return_value=facts)), \
         patch.object(match_control_view, "db_session", _fake_db_session()), \
         patch.object(match_control_view, "safe_pin", AsyncMock()) as pin:
        await match_control_view.post_room_header(channel, 143)

    text = channel.send.call_args.args[0]
    assert "#143 · Week 3" in text and "<#900>" in text
    pin.assert_awaited_once_with(posted)
    assert channel.edit.call_args.kwargs["topic"] == "#143 · Week 3 — Alpha vs Bravo"


@pytest.mark.asyncio
async def test_room_header_is_not_posted_twice_when_room_creation_resumes():
    """A run that died after the header leaves it pinned; the retry that
    finishes the rooms must not pin a second one."""
    import match_control_view

    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock()
    channel.edit = AsyncMock()
    already = MagicMock()
    already.content = "**#143 · Week 3 — Alpha vs Bravo**\nScheduling thread: <#900>\nResults record to the tournament automatically."
    channel.pins = AsyncMock(return_value=[already])
    facts = (_match(thread_id="900"), "Alpha", "Bravo", "Week 3", _draft())

    with patch.object(match_control_view, "match_facts", AsyncMock(return_value=facts)), \
         patch.object(match_control_view, "db_session", _fake_db_session()):
        await match_control_view.post_room_header(channel, 143)

    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_room_header_failure_never_escapes_into_room_creation():
    import match_control_view

    channel = MagicMock(spec=discord.TextChannel)
    channel.pins = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=403), "nope"))
    channel.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=403), "nope"))
    facts = (_match(), "Alpha", "Bravo", "Week 3", _draft())

    with patch.object(match_control_view, "match_facts", AsyncMock(return_value=facts)), \
         patch.object(match_control_view, "db_session", _fake_db_session()):
        await match_control_view.post_room_header(channel, 143)  # no raise


@pytest.mark.asyncio
async def test_rooms_open_is_posted_to_the_thread_and_the_control_message_follows():
    import match_control_view

    thread = _thread()
    bot = MagicMock()
    bot.get_channel.return_value = thread
    facts = (_match(), "Alpha", "Bravo", "Week 3", _draft(rooms=True))

    with patch.object(match_control_view, "match_facts", AsyncMock(return_value=facts)), \
         patch.object(match_control_view, "db_session", _fake_db_session()), \
         patch.object(match_control_view, "_refresh_match_views_with_facts", AsyncMock()) as refresh:
        await match_control_view.announce_rooms_open(bot, 143)

    text = thread.send.call_args.args[0]
    assert "<#555>" in text and "Rooms are open" in text
    refresh.assert_awaited_once_with(bot, 143, facts)


@pytest.mark.asyncio
async def test_rooms_open_without_a_room_posts_nothing():
    import match_control_view

    thread = _thread()
    bot = MagicMock()
    bot.get_channel.return_value = thread
    facts = (_match(), "Alpha", "Bravo", "Week 3", _draft(rooms=False))

    with patch.object(match_control_view, "match_facts", AsyncMock(return_value=facts)), \
         patch.object(match_control_view, "db_session", _fake_db_session()), \
         patch.object(match_control_view, "_refresh_match_views_with_facts", AsyncMock()):
        await match_control_view.announce_rooms_open(bot, 143)

    thread.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_recorded_result_is_posted_to_the_thread_as_a_fresh_message():
    import match_control_view

    thread = _thread()
    bot = MagicMock()
    bot.get_channel.return_value = thread
    match = _match()
    match.team_a_wins, match.team_b_wins = 5, 3
    facts = (match, "Alpha", "Bravo", "Week 3", _draft())

    with patch.object(match_control_view, "match_facts", AsyncMock(return_value=facts)), \
         patch.object(match_control_view, "db_session", _fake_db_session()):
        await match_control_view.post_result_to_thread(bot, 143)

    text = thread.send.call_args.args[0]
    assert "Result recorded" in text and "5–3" in text


@pytest.mark.asyncio
async def test_result_post_tolerates_a_vanished_thread():
    import match_control_view

    bot = MagicMock()
    bot.get_channel.return_value = None
    bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    match = _match()
    match.team_a_wins, match.team_b_wins = 5, 3
    facts = (match, "Alpha", "Bravo", "Week 3", None)

    with patch.object(match_control_view, "match_facts", AsyncMock(return_value=facts)), \
         patch.object(match_control_view, "db_session", _fake_db_session()):
        await match_control_view.post_result_to_thread(bot, 143)  # no raise


# --- the victory chokepoint posts the result into the thread exactly once ----

@pytest.mark.asyncio
async def test_victory_sync_posts_to_the_thread_on_the_clinch():
    import utils

    bot = MagicMock()
    with patch("services.tournament_service.sync_linked_result",
               AsyncMock(return_value=(MagicMock(), False, True))), \
         patch("services.tournament_formatter.update_standings_message_for_match", AsyncMock()), \
         patch("cogs.tournament_commands.after_bracket_result", AsyncMock()), \
         patch("match_control_view.safe_refresh_match_views", AsyncMock()), \
         patch("match_control_view.post_result_to_thread", AsyncMock()) as post:
        await utils._sync_linked_tournament_match(bot, 143, 5, 3, "draft-1")

    post.assert_awaited_once_with(bot, 143)


@pytest.mark.asyncio
async def test_victory_sync_does_not_repost_a_corrected_score():
    import utils

    bot = MagicMock()
    with patch("services.tournament_service.sync_linked_result",
               AsyncMock(return_value=(MagicMock(), False, False))), \
         patch("services.tournament_formatter.update_standings_message_for_match", AsyncMock()), \
         patch("cogs.tournament_commands.after_bracket_result", AsyncMock()), \
         patch("match_control_view.safe_refresh_match_views", AsyncMock()), \
         patch("match_control_view.post_result_to_thread", AsyncMock()) as post:
        await utils._sync_linked_tournament_match(bot, 143, 7, 3, "draft-1")

    post.assert_not_awaited()
