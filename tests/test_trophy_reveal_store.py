import os, tempfile
import pytest, pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from database.db_session import AsyncSessionLocal
from database.models_base import Base
from services.trophy_quiz_reveal_store import record_reveal, has_revealed


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
async def test_has_revealed_false_before_reveal(test_db):
    assert await has_revealed("q1", "u1") is False


@pytest.mark.asyncio
async def test_record_then_has_revealed_true(test_db):
    await record_reveal("q1", "u1")
    assert await has_revealed("q1", "u1") is True
    assert await has_revealed("q1", "u2") is False   # scoped per player
    assert await has_revealed("q2", "u1") is False   # scoped per quiz


@pytest.mark.asyncio
async def test_record_reveal_is_idempotent(test_db):
    await record_reveal("q1", "u1")
    await record_reveal("q1", "u1")                   # no error, no duplicate
    assert await has_revealed("q1", "u1") is True
