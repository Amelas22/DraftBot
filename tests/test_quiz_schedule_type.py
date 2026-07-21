import os, tempfile
import pytest, pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from database.db_session import AsyncSessionLocal
from database.models_base import Base
from models.quiz_scheduling import QuizChannel, QuizSchedule
from cogs.quiz_scheduling_cog import QuizSchedulingCog


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


def _ctx():
    ctx = MagicMock()
    ctx.defer = AsyncMock()
    ctx.followup.send = AsyncMock()
    return ctx


@pytest_asyncio.fixture
async def cog_and_channel(test_db):
    async with AsyncSessionLocal() as s:
        async with s.begin():
            s.add(QuizChannel(channel_id="c1", guild_id="g1"))
    cog = QuizSchedulingCog(bot=MagicMock())
    channel = MagicMock(); channel.id = "c1"; channel.mention = "#quiz"
    return cog, channel


@pytest.mark.asyncio
async def test_add_quiz_schedule_persists_trophy_type(cog_and_channel):
    cog, channel = cog_and_channel
    await cog.add_quiz_schedule.callback(cog, _ctx(), channel, 18, 0, "trophy")
    async with AsyncSessionLocal() as s:
        row = (await s.execute(select(QuizSchedule).where(QuizSchedule.post_time == "18:00"))).scalar_one()
    assert row.quiz_type == "trophy"


@pytest.mark.asyncio
async def test_add_quiz_schedule_defaults_to_pick(cog_and_channel):
    cog, channel = cog_and_channel
    await cog.add_quiz_schedule.callback(cog, _ctx(), channel, 10, 0)  # no type
    async with AsyncSessionLocal() as s:
        row = (await s.execute(select(QuizSchedule).where(QuizSchedule.post_time == "10:00"))).scalar_one()
    assert row.quiz_type == "pick"


@pytest.mark.asyncio
async def test_get_channel_schedules_includes_type(test_db):
    async with AsyncSessionLocal() as s:
        async with s.begin():
            s.add(QuizChannel(channel_id="c1", guild_id="g1"))
            s.add(QuizSchedule(channel_id="c1", post_time="18:00", quiz_type="trophy"))
    cog = QuizSchedulingCog(bot=MagicMock())
    rows = await cog.get_channel_schedules("c1")
    assert rows and rows[0][1] == "18:00" and rows[0][2] == "trophy"
