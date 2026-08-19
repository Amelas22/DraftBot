"""Match-thread lookup and control-message facts, against a real SQLite session."""
import os
import random
import tempfile

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


async def _one_match(session, thread_id="900"):
    """A started 2-team tournament's only match, with a thread attached."""
    tournament = await create_tournament(session, "g1", "Spring", 3)
    await session.commit()
    await register_team(session, tournament.id, "Alpha", "1")
    await register_team(session, tournament.id, "Bravo", "2")
    await session.commit()
    matches = await start_tournament(session, tournament.id, random.Random(7))
    await session.commit()
    match = await session.get(TournamentMatch, matches[0].id)
    match.thread_id = thread_id
    await session.commit()
    return match


async def _link_draft(session, match_id, session_id="d1"):
    session.add(DraftSession(
        session_id=session_id, guild_id="g1", session_type="premade",
        draft_channel_id="55", message_id="66", tournament_match_id=match_id,
    ))
    await session.commit()


@pytest.mark.asyncio
async def test_match_facts_returns_team_names_and_round(test_db):
    from match_control_view import match_facts

    async with test_db() as session:
        match = await _one_match(session)
        _m, a_name, b_name, round_number, draft = await match_facts(session, match.id)

    assert {a_name, b_name} == {"Alpha", "Bravo"}
    assert round_number == 1
    assert draft is None


@pytest.mark.asyncio
async def test_match_facts_finds_the_linked_draft(test_db):
    from match_control_view import match_facts

    async with test_db() as session:
        match = await _one_match(session)
        await _link_draft(session, match.id)
        _m, _a, _b, _r, draft = await match_facts(session, match.id)

    assert draft is not None and draft.session_id == "d1"


@pytest.mark.asyncio
async def test_match_facts_is_none_for_a_missing_match(test_db):
    from match_control_view import match_facts

    async with test_db() as session:
        assert await match_facts(session, 999999) is None


def test_lobby_link_is_none_without_a_draft():
    from match_control_view import lobby_link

    assert lobby_link(None) is None


def test_lobby_link_points_at_the_lobby_message():
    from match_control_view import lobby_link

    draft = DraftSession(session_id="d1", guild_id="7", draft_channel_id="8", message_id="9")
    assert lobby_link(draft) == "https://discord.com/channels/7/8/9"


@pytest.mark.asyncio
async def test_control_body_offers_start_when_scheduling(test_db):
    from match_control_view import control_body_and_view

    async with test_db() as session:
        match = await _one_match(session)
        body, view = control_body_and_view(match, "Alpha", "Bravo", 1, None)

    assert "Not started yet" in body
    assert view is not None


@pytest.mark.asyncio
async def test_control_body_drops_the_button_once_drafting(test_db):
    from match_control_view import control_body_and_view
    from models.draft_session import DraftSession as DS

    async with test_db() as session:
        match = await _one_match(session)
        draft = DS(session_id="d1", guild_id="7", draft_channel_id="8", message_id="9")
        body, view = control_body_and_view(match, "Alpha", "Bravo", 1, draft)

    assert "Draft in progress" in body
    assert "https://discord.com/channels/7/8/9" in body
    assert view is None
