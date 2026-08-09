"""Shared pytest fixtures.

test_db: a throwaway file-backed SQLite database with the full schema,
wired into the app's AsyncSessionLocal for the duration of one test.
Many older test files still carry a local copy of this fixture (which
shadows this one, harmlessly); new test files should use this one.
"""
import os
import tempfile

import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from database.models_base import Base
from database.db_session import AsyncSessionLocal


@pytest_asyncio.fixture
async def test_db():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    AsyncSessionLocal.configure(bind=engine)
    yield engine
    await engine.dispose()
    os.unlink(tmp.name)
