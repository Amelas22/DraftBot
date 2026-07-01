"""Cleanup guard: don't reap drafts whose tournament match is still unfinished."""
import os
import random
import tempfile
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from database.models_base import Base
import models  # noqa: F401  register all tables
from models.tournament import TournamentMatch
from services.tournament_service import (
    create_tournament,
    extend_deletion_if_unfinished,
    register_team,
    set_result,
    start_tournament,
    tournament_match_is_unfinished,
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


class _DS:
    """Stand-in for a DraftSession row (only the fields the guard touches)."""
    def __init__(self, tournament_match_id):
        self.tournament_match_id = tournament_match_id
        self.deletion_time = None


async def _one_match(session):
    t = await create_tournament(session, "g1", "RR", 0, format="round_robin")
    await session.commit()
    await register_team(session, t.id, "Alpha", "1")
    await register_team(session, t.id, "Bravo", "2")
    await session.commit()
    matches = await start_tournament(session, t.id, random.Random(7))
    await session.commit()
    return matches[0]


@pytest.mark.asyncio
async def test_unfinished_match_is_unfinished(test_db):
    async with test_db() as session:
        m = await _one_match(session)
        assert await tournament_match_is_unfinished(session, m.id) is True


@pytest.mark.asyncio
async def test_finished_match_is_not_unfinished(test_db):
    async with test_db() as session:
        m = await _one_match(session)
        await set_result(session, m.id, 2, 1)
        await session.commit()
        assert await tournament_match_is_unfinished(session, m.id) is False


@pytest.mark.asyncio
async def test_bye_and_missing_and_none_are_not_unfinished(test_db):
    async with test_db() as session:
        m = await _one_match(session)
        m.is_bye = True
        await session.commit()
        assert await tournament_match_is_unfinished(session, m.id) is False
        assert await tournament_match_is_unfinished(session, 999999) is False
        assert await tournament_match_is_unfinished(session, None) is False


@pytest.mark.asyncio
async def test_guard_extends_and_skips_unfinished(test_db):
    async with test_db() as session:
        m = await _one_match(session)
        now = datetime(2026, 7, 1, 12, 0, 0)
        ds = _DS(m.id)
        skipped = await extend_deletion_if_unfinished(session, ds, now)
        assert skipped is True
        assert ds.deletion_time == now + timedelta(days=7)


@pytest.mark.asyncio
async def test_guard_leaves_finished_and_plain_sessions(test_db):
    async with test_db() as session:
        m = await _one_match(session)
        await set_result(session, m.id, 2, 1)
        await session.commit()
        now = datetime(2026, 7, 1, 12, 0, 0)

        finished = _DS(m.id)
        assert await extend_deletion_if_unfinished(session, finished, now) is False
        assert finished.deletion_time is None

        plain = _DS(None)
        assert await extend_deletion_if_unfinished(session, plain, now) is False
        assert plain.deletion_time is None
