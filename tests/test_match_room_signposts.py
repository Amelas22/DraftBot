"""A tournament draft's rooms and its match thread point at each other."""
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from test_match_control_flow import make_thread

LOBBY = "https://discord.com/channels/1/2/3"
CHAT_ID, THREAD_ID = 555, 900


def _match(thread_id=str(THREAD_ID), control_id="777"):
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
    draft.draft_chat_channel = str(CHAT_ID)
    draft.rooms_created_at = datetime(2026, 11, 5) if rooms else None
    return draft


def _chat():
    chat = MagicMock(spec=discord.TextChannel)
    chat.id = CHAT_ID
    chat.send = AsyncMock(return_value=MagicMock())
    chat.edit = AsyncMock()
    return chat


def _bot(chat=None, thread=None):
    bot = MagicMock()
    bot.get_channel.side_effect = {CHAT_ID: chat, THREAD_ID: thread}.get
    bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    return bot


@contextmanager
def _with_facts(facts):
    """match_control_view reading these facts instead of a database."""
    import match_control_view

    @asynccontextmanager
    async def fake_db():
        yield MagicMock()

    with patch.object(match_control_view, "match_facts", AsyncMock(return_value=facts)), \
         patch.object(match_control_view, "db_session", fake_db):
        yield


def test_control_body_points_at_the_room_once_rooms_exist():
    from match_control_view import control_body_and_view
    body, _view = control_body_and_view(_match(), "Alpha", "Bravo", "Week 3", _draft(rooms=True))
    assert f"<#{CHAT_ID}>" in body and LOBBY not in body


def test_control_body_links_the_lobby_until_rooms_exist():
    from match_control_view import control_body_and_view
    body, _view = control_body_and_view(_match(), "Alpha", "Bravo", "Week 3", _draft(rooms=False))
    assert LOBBY in body and f"<#{CHAT_ID}>" not in body


# --- when the rooms are created --------------------------------------------

@pytest.mark.asyncio
async def test_rooms_created_heads_the_chat_tells_the_thread_and_refreshes():
    import match_control_view

    chat, thread = _chat(), make_thread(THREAD_ID)
    bot = _bot(chat, thread)
    facts = (_match(), "Alpha", "Bravo", "Week 3", _draft(rooms=True))

    with _with_facts(facts), \
         patch.object(match_control_view, "safe_pin", AsyncMock()) as pin, \
         patch.object(match_control_view, "_refresh_match_views_with_facts", AsyncMock()) as refresh:
        await match_control_view.on_tournament_rooms_created(bot, 143)

    header = chat.send.call_args.args[0]
    assert "#143 · Week 3" in header and f"<#{THREAD_ID}>" in header
    pin.assert_awaited_once_with(chat.send.return_value)
    assert chat.edit.call_args.kwargs["topic"] == "#143 · Week 3 — Alpha vs Bravo"
    opened = thread.send.call_args.args[0]
    assert "Rooms are open" in opened and f"<#{CHAT_ID}>" in opened
    refresh.assert_awaited_once_with(bot, 143, facts)


@pytest.mark.asyncio
async def test_rooms_created_without_rooms_does_nothing():
    import match_control_view

    chat, thread = _chat(), make_thread(THREAD_ID)
    facts = (_match(), "Alpha", "Bravo", "Week 3", _draft(rooms=False))

    with _with_facts(facts), \
         patch.object(match_control_view, "_refresh_match_views_with_facts", AsyncMock()) as refresh:
        await match_control_view.on_tournament_rooms_created(_bot(chat, thread), 143)

    chat.send.assert_not_awaited()
    thread.send.assert_not_awaited()
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_rooms_created_without_a_thread_still_heads_the_chat():
    import match_control_view

    chat = _chat()
    facts = (_match(thread_id=None), "Alpha", "Bravo", "Week 3", _draft(rooms=True))

    with _with_facts(facts), \
         patch.object(match_control_view, "safe_pin", AsyncMock()), \
         patch.object(match_control_view, "_refresh_match_views_with_facts", AsyncMock()):
        await match_control_view.on_tournament_rooms_created(_bot(chat, None), 143)

    assert "<#" not in chat.send.call_args.args[0]


@pytest.mark.asyncio
async def test_rooms_created_swallows_a_refused_header():
    """Room creation has already committed; a signpost failing must not turn
    it into an error for the person who clicked."""
    import match_control_view

    chat = _chat()
    chat.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=403), "nope"))
    facts = (_match(), "Alpha", "Bravo", "Week 3", _draft(rooms=True))

    with _with_facts(facts), \
         patch.object(match_control_view, "_refresh_match_views_with_facts", AsyncMock()):
        await match_control_view.on_tournament_rooms_created(_bot(chat, make_thread(THREAD_ID)), 143)


@pytest.mark.asyncio
async def test_rooms_created_swallows_anything_else():
    import match_control_view

    with patch.object(match_control_view, "match_facts", AsyncMock(side_effect=RuntimeError("db down"))), \
         patch.object(match_control_view, "db_session", MagicMock(side_effect=RuntimeError("db down"))):
        await match_control_view.on_tournament_rooms_created(_bot(), 143)  # no raise


# --- when the result lands ---------------------------------------------------

@pytest.mark.asyncio
async def test_recorded_result_is_posted_to_the_thread_as_a_fresh_message():
    import match_control_view

    thread = make_thread(THREAD_ID)
    match = _match()
    match.team_a_wins, match.team_b_wins = 5, 3

    with _with_facts((match, "Alpha", "Bravo", "Week 3", _draft())):
        await match_control_view.post_result_to_thread(_bot(None, thread), 143)

    text = thread.send.call_args.args[0]
    assert "Result recorded" in text and "5–3" in text


@pytest.mark.asyncio
async def test_result_post_tolerates_a_vanished_thread():
    import match_control_view

    match = _match()
    match.team_a_wins, match.team_b_wins = 5, 3

    with _with_facts((match, "Alpha", "Bravo", "Week 3", None)):
        await match_control_view.post_result_to_thread(_bot(), 143)  # no raise


# --- the victory chokepoint posts the result when the winner changes ---------

def _victory_patches(winner_changed, post):
    return [
        patch("services.tournament_service.sync_linked_result",
              AsyncMock(return_value=(MagicMock(), False, winner_changed))),
        patch("services.tournament_formatter.update_standings_message_for_match", AsyncMock()),
        patch("cogs.tournament_commands.after_bracket_result", AsyncMock()),
        patch("match_control_view.safe_refresh_match_views", AsyncMock()),
        patch("match_control_view.post_result_to_thread", post),
    ]


@pytest.mark.asyncio
async def test_victory_sync_posts_to_the_thread_on_the_clinch():
    import utils

    bot, post = MagicMock(), AsyncMock()
    patches = _victory_patches(True, post)
    for p in patches:
        p.start()
    try:
        await utils._sync_linked_tournament_match(bot, 143, 5, 3, "draft-1")
    finally:
        for p in patches:
            p.stop()
    post.assert_awaited_once_with(bot, 143)


@pytest.mark.asyncio
async def test_victory_sync_does_not_repost_a_corrected_score():
    import utils

    post = AsyncMock()
    patches = _victory_patches(False, post)
    for p in patches:
        p.start()
    try:
        await utils._sync_linked_tournament_match(MagicMock(), 143, 7, 3, "draft-1")
    finally:
        for p in patches:
            p.stop()
    post.assert_not_awaited()
