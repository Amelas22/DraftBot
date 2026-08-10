"""Shared pytest fixtures and seeding helpers.

test_db: a throwaway file-backed SQLite database with the full schema,
wired into the app's AsyncSessionLocal for the duration of one test and
unwired afterward. Many older test files still carry a local copy of this
fixture (which shadows this one, harmlessly); new test files should use
this one.

seed_session: the one seeder for DraftSession + MatchResult fixtures --
import it (``from conftest import seed_session``) instead of hand-writing
another per-file variant.
"""
import os
import tempfile
from datetime import datetime

import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from database.models_base import Base
from database.db_session import AsyncSessionLocal
from models.draft_session import DraftSession
from models.match import MatchResult


@pytest_asyncio.fixture
async def test_db():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    previous_bind = AsyncSessionLocal.kw.get("bind")
    AsyncSessionLocal.configure(bind=engine)
    yield engine
    # Restore the prior binding BEFORE disposing this engine: the factory
    # is process-wide, and leaving it bound to a disposed engine would make
    # any later test that doesn't request this fixture fail on session use
    # (order-dependent breakage).
    AsyncSessionLocal.configure(bind=previous_bind)
    await engine.dispose()
    os.unlink(tmp.name)


async def seed_session(session_id="s1", guild="g", stype="staked",
                       stage="completed", victory=None, teams=None,
                       matches=(), start=None, sign_ups=None,
                       cube="TestCube"):
    """Seed one DraftSession plus its MatchResults.

    teams: (team_a_list, team_b_list) or None (legacy-style, no team JSON).
    matches: iterable of (player1, player2, winner, submitted_at_or_None).
    """
    when = start or datetime(2026, 1, 1)
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(
            session_id=session_id, guild_id=guild, session_type=stype,
            session_stage=stage,
            victory_message_id_results_channel=victory,
            team_a=list(teams[0]) if teams else None,
            team_b=list(teams[1]) if teams else None,
            draft_start_time=when, teams_start_time=when,
            sign_ups=sign_ups, cube=cube))
        for i, (p1, p2, w, ts) in enumerate(matches):
            s.add(MatchResult(session_id=session_id, match_number=i + 1,
                              player1_id=p1, player2_id=p2, winner_id=w,
                              result_submitted_at=ts))
        await s.commit()
