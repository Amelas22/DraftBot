import os, tempfile
import pytest, pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from database.db_session import AsyncSessionLocal
from database.models_base import Base
from models.quiz_scheduling import QuizChannel, QuizSchedule


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
async def test_quiz_schedule_defaults_to_pick(test_db):
    async with AsyncSessionLocal() as s:
        async with s.begin():
            s.add(QuizChannel(channel_id="c1", guild_id="g1"))
            s.add(QuizSchedule(channel_id="c1", post_time="10:00"))  # no quiz_type
    async with AsyncSessionLocal() as s:
        row = (await s.execute(select(QuizSchedule).where(QuizSchedule.channel_id == "c1"))).scalar_one()
    assert row.quiz_type == "pick"


@pytest.mark.asyncio
async def test_quiz_schedule_stores_trophy(test_db):
    async with AsyncSessionLocal() as s:
        async with s.begin():
            s.add(QuizChannel(channel_id="c1", guild_id="g1"))
            s.add(QuizSchedule(channel_id="c1", post_time="18:00", quiz_type="trophy"))
    async with AsyncSessionLocal() as s:
        row = (await s.execute(select(QuizSchedule).where(QuizSchedule.post_time == "18:00"))).scalar_one()
    assert row.quiz_type == "trophy"
