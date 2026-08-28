"""The lookup itself, against a real database rather than a patched one.

tests/test_abandon_from_any_channel.py patches get_by_any_channel_id to prove the
command CALLS it. That leaves the interesting half untested: whether the lookup
actually finds a draft from a team channel given how the columns are really typed.

They are typed inconsistently, which is the whole risk. draft_chat_channel is a
String column holding "100"; channel_ids is JSON holding ints; and Discord hands
the command an int. A lookup that compared any two of those directly would work in
a mocked test and fail on the first real stalled draft.
"""
import os
import tempfile

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from database.models_base import Base
from models.draft_session import DraftSession

DRAFT_CHAT = 100
RED_TEAM_CHAT = 101
BLUE_TEAM_CHAT = 102


@pytest_asyncio.fixture
async def real_db(monkeypatch):
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    temp_db.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{temp_db.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    # get_by_any_channel_id opens its own session, so point that at this database.
    import database.db_session as db_mod
    import models.draft_session as ds_mod
    monkeypatch.setattr(db_mod, "AsyncSessionLocal", factory, raising=False)
    monkeypatch.setattr(ds_mod, "db_session", factory)

    async with factory() as db:
        async with db.begin():
            db.add(DraftSession(
                session_id="s1", guild_id="g", session_stage="pairings",
                # Exactly as production stores them: the chat as a string, the
                # created channels as ints.
                draft_chat_channel=str(DRAFT_CHAT),
                channel_ids=[DRAFT_CHAT, RED_TEAM_CHAT, BLUE_TEAM_CHAT],
                sign_ups={"1": "A", "2": "B"},
            ))
    yield factory
    await engine.dispose()
    os.unlink(temp_db.name)


@pytest.mark.asyncio
async def test_finds_the_draft_from_a_team_channel_given_an_int(real_db):
    """Discord hands us an int; channel_ids holds ints; the column is JSON."""
    found = await DraftSession.get_by_any_channel_id(RED_TEAM_CHAT)
    assert found is not None and found.session_id == "s1"


@pytest.mark.asyncio
async def test_finds_the_draft_from_the_chat_channel_given_an_int(real_db):
    """draft_chat_channel is a String column, so an int has to be coerced."""
    found = await DraftSession.get_by_any_channel_id(DRAFT_CHAT)
    assert found is not None and found.session_id == "s1"


@pytest.mark.asyncio
async def test_a_channel_of_another_draft_finds_nothing(real_db):
    assert await DraftSession.get_by_any_channel_id(999) is None


@pytest.mark.asyncio
async def test_a_substring_of_a_stored_id_is_not_a_match(real_db):
    """The LIKE prefilter matches '10' inside '100'; exact membership must reject
    it. Without that check a player in an unrelated channel could abandon someone
    else's draft."""
    assert await DraftSession.get_by_any_channel_id(10) is None
