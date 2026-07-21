import pytest, pytest_asyncio, tempfile, os
from sqlalchemy.ext.asyncio import create_async_engine
from database.models_base import Base
from database.db_session import AsyncSessionLocal
from models.trophy_quiz_session import TrophyQuizSession
from models.trophy_quiz_submission import TrophyQuizSubmission


@pytest_asyncio.fixture
async def test_db():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.db'); tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    AsyncSessionLocal.configure(bind=engine)
    yield engine
    await engine.dispose(); os.unlink(tmp.name)


@pytest.mark.asyncio
async def test_round_trip(test_db):
    async with AsyncSessionLocal() as s:
        async with s.begin():
            s.add(TrophyQuizSession(
                quiz_id="g-1", display_id=1, guild_id="g", channel_id="c",
                draft_session_id="d",
                decks=[{"slot": "A", "drafter_id": "u1", "wins": 3},
                       {"slot": "B", "drafter_id": "u2", "wins": 0}],
                posted_by="mod"))
            s.add(TrophyQuizSubmission(
                quiz_id="g-1", player_id="p", display_name="P",
                guesses=[3, 0], direction_correct=True, exact_points=[4, 4],
                points_earned=12))
    async with AsyncSessionLocal() as s:
        sub = await s.get(TrophyQuizSubmission, ("g-1", "p"))
        assert sub.points_earned == 12 and sub.direction_correct is True
        quiz = await s.get(TrophyQuizSession, "g-1")
        assert len(quiz.decks) == 2
