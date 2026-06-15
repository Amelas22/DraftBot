"""Tests for Slice 4: the auto-updating tournament standings message."""
import os
import random
import tempfile
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from database.models_base import Base
from models.tournament import Tournament, TournamentParticipant
from services.tournament_formatter import (
    create_standings_embed,
    update_standings_message,
    update_standings_message_for_match,
)
from services.tournament_service import (
    create_tournament,
    register_team,
    set_result,
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


def _fake_db_session(factory):
    @asynccontextmanager
    async def fake():
        async with factory() as inner:
            yield inner
            await inner.commit()
    return fake


def _participant(team_name, points, wins=0, losses=0, draws=0):
    return TournamentParticipant(
        tournament_id=1, team_id=1, team_name=team_name, captain_user_id="1",
        points=points, match_wins=wins, match_losses=losses, match_draws=draws,
    )


# ---- model columns ----------------------------------------------------------------

def test_tournament_has_standings_message_columns():
    t = Tournament(guild_id="1", name="Spring", total_rounds=3)
    assert t.standings_channel_id is None
    assert t.standings_message_id is None


# ---- create_standings_embed (pure) ------------------------------------------------

def test_standings_embed_lists_teams_in_given_order():
    tournament = Tournament(guild_id="1", name="Spring Cup", total_rounds=3)
    tournament.status = "active"
    tournament.current_round = 1
    participants = [
        _participant("Alpha", 3, wins=1),
        _participant("Bravo", 0, losses=1),
    ]
    embed = create_standings_embed(tournament, participants)
    assert "Spring Cup" in embed.title
    body = "\n".join(f.value for f in embed.fields)
    assert "Alpha" in body and "Bravo" in body
    assert body.index("Alpha") < body.index("Bravo")
    assert "3" in body  # points shown


def test_standings_embed_handles_no_participants():
    tournament = Tournament(guild_id="1", name="Spring Cup", total_rounds=3)
    tournament.status = "registration"
    tournament.current_round = 0
    embed = create_standings_embed(tournament, [])
    # Should not raise and should produce some placeholder content
    assert embed.fields


# ---- update_standings_message -----------------------------------------------------

@pytest.mark.asyncio
async def test_update_standings_message_edits_stored_message(test_db):
    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()
        await register_team(session, tournament.id, "Alpha", "1")
        await register_team(session, tournament.id, "Bravo", "2")
        await session.commit()
        matches = await start_tournament(session, tournament.id, random.Random(7))
        await set_result(session, matches[0].id, 2, 0)
        tournament.standings_channel_id = "555"
        tournament.standings_message_id = "777"
        await session.commit()
        tid = tournament.id

    message = MagicMock()
    message.edit = AsyncMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)
    bot = MagicMock()
    bot.get_channel.return_value = channel

    with patch("services.tournament_formatter.db_session", _fake_db_session(test_db)):
        await update_standings_message(bot, tid)

    bot.get_channel.assert_called_once_with(555)
    channel.fetch_message.assert_awaited_once_with(777)
    message.edit.assert_awaited_once()
    assert "embed" in message.edit.call_args.kwargs


@pytest.mark.asyncio
async def test_update_standings_message_noop_when_not_posted(test_db):
    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()
        tid = tournament.id

    bot = MagicMock()
    with patch("services.tournament_formatter.db_session", _fake_db_session(test_db)):
        await update_standings_message(bot, tid)
    bot.get_channel.assert_not_called()


@pytest.mark.asyncio
async def test_update_standings_message_survives_missing_message(test_db):
    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()
        tournament.standings_channel_id = "555"
        tournament.standings_message_id = "777"
        await session.commit()
        tid = tournament.id

    channel = MagicMock()
    channel.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    bot = MagicMock()
    bot.get_channel.return_value = channel

    with patch("services.tournament_formatter.db_session", _fake_db_session(test_db)):
        await update_standings_message(bot, tid)  # must not raise


# ---- update_standings_message_for_match -------------------------------------------

@pytest.mark.asyncio
async def test_update_for_match_resolves_tournament_and_edits(test_db):
    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()
        await register_team(session, tournament.id, "Alpha", "1")
        await register_team(session, tournament.id, "Bravo", "2")
        await session.commit()
        matches = await start_tournament(session, tournament.id, random.Random(7))
        tournament.standings_channel_id = "555"
        tournament.standings_message_id = "777"
        await session.commit()
        match_id = matches[0].id

    message = MagicMock()
    message.edit = AsyncMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)
    bot = MagicMock()
    bot.get_channel.return_value = channel

    with patch("services.tournament_formatter.db_session", _fake_db_session(test_db)):
        await update_standings_message_for_match(bot, match_id)

    message.edit.assert_awaited_once()
