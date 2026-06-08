import os
import tempfile
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from database.models_base import Base
from models.draft_session import DraftSession
from models.match import MatchResult
from cogs.draft_control import abandon_draft_session, AbandonVoteView


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


async def _seed(factory, session_id="s1", stage="pairings"):
    async with factory() as db:
        async with db.begin():
            db.add(DraftSession(
                session_id=session_id, guild_id="g", session_stage=stage,
                sign_ups={"1": "A", "2": "B"},
            ))
            db.add(MatchResult(
                session_id=session_id, match_number=1, player1_id="1", player2_id="2",
                player1_wins=2, player2_wins=1, winner_id="1", result_submitted_at=datetime.now(),
            ))
            db.add(MatchResult(
                session_id=session_id, match_number=2, player1_id="1", player2_id="2",
                winner_id=None,
            ))


@pytest.mark.asyncio
async def test_abandon_voids_matches_and_marks_abandoned(test_db):
    await _seed(test_db)

    await abandon_draft_session("s1", session_factory=test_db)

    async with test_db() as db:
        ds = (await db.execute(
            select(DraftSession).where(DraftSession.session_id == "s1")
        )).scalar_one()
        assert ds.session_stage == "abandoned"
        assert ds.deletion_time is not None

        results = (await db.execute(
            select(MatchResult).where(MatchResult.session_id == "s1")
        )).scalars().all()
        assert results, "expected match rows"
        assert all(r.winner_id is None for r in results)
        assert all(r.player1_wins == 0 and r.player2_wins == 0 for r in results)
        assert all(r.result_submitted_at is None for r in results)


@pytest.mark.asyncio
async def test_abandon_vote_needs_majority_even_participants():
    view = AbandonVoteView("s1", ["1", "2", "3", "4"])
    view.votes = {"1": True, "2": True, "3": None, "4": None}
    passed, yes, total = view.get_vote_result()
    assert (yes, total) == (2, 4)
    assert not passed  # need 3 of 4

    view.votes["3"] = True
    passed, _, _ = view.get_vote_result()
    assert passed


@pytest.mark.asyncio
async def test_abandon_vote_majority_odd_participants():
    view = AbandonVoteView("s1", ["1", "2", "3"])
    view.votes = {"1": True, "2": True, "3": False}
    passed, yes, total = view.get_vote_result()
    assert passed and yes == 2 and total == 3  # need 2 of 3
