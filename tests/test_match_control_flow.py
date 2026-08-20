"""Play / Start draft behaviour, with Discord mocked at the boundary."""
import os
import random
import tempfile
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from database.models_base import Base
from models.draft_session import DraftSession
from models.tournament import TournamentMatch
from services.tournament_service import (
    create_tournament,
    register_team,
    start_tournament,
)


@pytest_asyncio.fixture
async def test_db():
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{temp_db.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield factory
    await engine.dispose()
    os.unlink(temp_db.name)


@pytest.fixture
def patched_db(test_db):
    """Point match_control_view's db_session at the throwaway database."""
    @asynccontextmanager
    async def fake_db_session():
        async with test_db() as inner:
            yield inner
            await inner.commit()

    with patch("match_control_view.db_session", fake_db_session):
        yield test_db


async def _match(session, thread_id=None):
    tournament = await create_tournament(session, "g1", "Spring", 3)
    await session.commit()
    await register_team(session, tournament.id, "Alpha", "1")
    await register_team(session, tournament.id, "Bravo", "2")
    await session.commit()
    matches = await start_tournament(session, tournament.id, random.Random(7))
    await session.commit()
    match = await session.get(TournamentMatch, matches[0].id)
    if thread_id:
        match.thread_id = thread_id
    await session.commit()
    return match


def make_thread(thread_id=900):
    thread = MagicMock()
    thread.id = thread_id
    thread.mention = f"<#{thread_id}>"
    sent = MagicMock()
    sent.id = 777
    thread.send = AsyncMock(return_value=sent)
    thread.fetch_message = AsyncMock()
    return thread


def make_interaction(thread):
    interaction = MagicMock()
    interaction.guild_id = 1
    interaction.guild.get_channel.return_value = thread
    interaction.message.thread = None
    interaction.message.create_thread = AsyncMock(return_value=thread)
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


@pytest.mark.asyncio
async def test_play_creates_thread_and_control_message(patched_db):
    from match_control_view import open_match_room

    async with patched_db() as session:
        match = await _match(session)

    thread = make_thread()
    interaction = make_interaction(thread)
    with patch("match_control_view.safe_pin", AsyncMock()):
        await open_match_room(interaction, match.id)

    interaction.message.create_thread.assert_awaited_once()
    thread.send.assert_awaited_once()
    async with patched_db() as session:
        stored = await session.get(TournamentMatch, match.id)
        assert stored.thread_id == "900"
        assert stored.control_message_id == "777"


@pytest.mark.asyncio
async def test_play_twice_reuses_one_thread_and_one_control_message(patched_db):
    from match_control_view import open_match_room

    async with patched_db() as session:
        match = await _match(session)

    thread = make_thread()
    existing = MagicMock()
    existing.id = 777
    existing.edit = AsyncMock()
    thread.fetch_message = AsyncMock(return_value=existing)

    with patch("match_control_view.safe_pin", AsyncMock()):
        await open_match_room(make_interaction(thread), match.id)
        second = make_interaction(thread)
        await open_match_room(second, match.id)

    # The bug: a second click must not create a second thread or post again.
    second.message.create_thread.assert_not_awaited()
    assert thread.send.await_count == 1
    existing.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_play_adopts_a_thread_made_by_hand(patched_db):
    from match_control_view import open_match_room

    async with patched_db() as session:
        match = await _match(session)

    thread = make_thread()
    interaction = make_interaction(thread)
    interaction.message.thread = thread  # organiser created it off the pairing message

    with patch("match_control_view.safe_pin", AsyncMock()):
        await open_match_room(interaction, match.id)

    # Discord rejects a second thread on one message, so it must be adopted.
    interaction.message.create_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_draft_posts_the_cube_picker(patched_db):
    from match_control_view import start_match_draft

    async with patched_db() as session:
        match = await _match(session, thread_id="900")

    interaction = make_interaction(make_thread())
    with patch("match_control_view.cube_picker_for_match", MagicMock()) as picker:
        await start_match_draft(interaction, match.id)

    picker.assert_called_once()
    assert picker.call_args.args[1] == match.id
    interaction.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_draft_refuses_when_a_draft_is_live(patched_db):
    from match_control_view import start_match_draft

    async with patched_db() as session:
        match = await _match(session, thread_id="900")
        session.add(DraftSession(
            session_id="d1", guild_id="g1", session_type="premade",
            draft_channel_id="55", message_id="66", tournament_match_id=match.id,
        ))
        await session.commit()

    interaction = make_interaction(make_thread())
    with patch("match_control_view.cube_picker_for_match", MagicMock()) as picker:
        await start_match_draft(interaction, match.id)

    picker.assert_not_called()
    # It re-renders the stale message rather than only complaining to the clicker.
    interaction.response.edit_message.assert_awaited_once()
    assert "already underway" in interaction.followup.send.call_args.args[0]


@pytest.mark.asyncio
async def test_refresh_is_a_noop_when_no_control_message_exists(patched_db):
    from match_control_view import refresh_match_views

    async with patched_db() as session:
        match = await _match(session, thread_id="900")

    bot = MagicMock()
    bot.get_channel.return_value = make_thread()
    await refresh_match_views(bot, match.id)

    bot.get_channel.return_value.fetch_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_draft_picker_is_ephemeral(patched_db):
    from match_control_view import start_match_draft

    async with patched_db() as session:
        match = await _match(session, thread_id="900")

    interaction = make_interaction(make_thread())
    with patch("match_control_view.cube_picker_for_match", MagicMock()):
        await start_match_draft(interaction, match.id)

    # Ephemeral: the picker never persists in the thread, so no stale picker can
    # create a second draft for this match once one is under way.
    assert interaction.response.send_message.call_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_launch_block_for_is_none_when_match_is_free(patched_db):
    from match_control_view import launch_block_for

    async with patched_db() as session:
        match = await _match(session, thread_id="900")

    assert await launch_block_for(match.id) is None


@pytest.mark.asyncio
async def test_launch_block_for_reports_a_live_draft(patched_db):
    from match_control_view import launch_block_for

    async with patched_db() as session:
        match = await _match(session, thread_id="900")
        session.add(DraftSession(
            session_id="d1", guild_id="g1", session_type="premade",
            draft_channel_id="55", message_id="66", tournament_match_id=match.id,
        ))
        await session.commit()

    block = await launch_block_for(match.id)
    assert "already underway" in block


@pytest.mark.asyncio
async def test_create_match_room_makes_a_long_lived_thread_and_pins_control(patched_db):
    from match_control_view import create_match_room

    async with patched_db() as session:
        match = await _match(session)

    thread = make_thread()
    message = MagicMock()
    message.create_thread = AsyncMock(return_value=thread)

    with patch("match_control_view.safe_pin", AsyncMock()) as pin:
        result = await create_match_room(message, match.id)

    assert result is thread
    # 7 days: a thread made when pairings post must still be in the sidebar when
    # the match is actually played midweek.
    assert message.create_thread.call_args.kwargs["auto_archive_duration"] == 10080
    thread.send.assert_awaited_once()
    pin.assert_awaited_once()
    async with patched_db() as session:
        stored = await session.get(TournamentMatch, match.id)
        assert stored.thread_id == "900"
        assert stored.control_message_id == "777"


@pytest.mark.asyncio
async def test_create_match_room_degrades_when_discord_refuses(patched_db):
    import discord as _discord
    from match_control_view import create_match_room

    async with patched_db() as session:
        match = await _match(session)

    message = MagicMock()
    message.create_thread = AsyncMock(
        side_effect=_discord.HTTPException(MagicMock(), "no Manage Threads"))

    # The round must still post: a missing room costs a link, not the pairings.
    assert await create_match_room(message, match.id) is None
    async with patched_db() as session:
        stored = await session.get(TournamentMatch, match.id)
        assert stored.thread_id is None
