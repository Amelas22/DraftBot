import os
import tempfile

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from database.models_base import Base
from database.db_session import AsyncSessionLocal
from models.trophy_quiz_session import TrophyQuizSession
from models.trophy_quiz_submission import TrophyQuizSubmission
from services.leaderboard_service import get_trophy_quiz_points_leaderboard_data


@pytest_asyncio.fixture
async def test_db():
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{temp_db.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    AsyncSessionLocal.configure(bind=engine)

    yield engine

    await engine.dispose()
    os.unlink(temp_db.name)


@pytest.mark.asyncio
async def test_ranks_by_total_points(test_db):
    async with AsyncSessionLocal() as session:
        async with session.begin():
            session.add(TrophyQuizSession(
                quiz_id="g-1", display_id=1, guild_id="g",
                channel_id="c", draft_session_id="d",
                decks=[], posted_by="m"
            ))
            session.add(TrophyQuizSubmission(
                quiz_id="g-1", player_id="lo", display_name="Lo",
                guesses=[], direction_correct=True, exact_points=[],
                points_earned=4
            ))
            session.add(TrophyQuizSubmission(
                quiz_id="g-1", player_id="hi", display_name="Hi",
                guesses=[], direction_correct=True, exact_points=[],
                points_earned=12
            ))

    async with AsyncSessionLocal() as session:
        rows = await get_trophy_quiz_points_leaderboard_data("g", "lifetime", 10, session)

    assert [r["player_id"] for r in rows] == ["hi", "lo"]
    assert rows[0]["total_points"] == 12
