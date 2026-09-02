
import pytest
import pytest_asyncio
import tempfile
import os
import time
from unittest.mock import AsyncMock, MagicMock
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import select

from database.models_base import Base
from database.db_session import AsyncSessionLocal
from database.message_management import Message, handle_sticky_message_update, StickyUpdateResult

@pytest_asyncio.fixture
async def test_db():
    """Create a temporary test database"""
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db.close()

    engine = create_async_engine(f"sqlite+aiosqlite:///{temp_db.name}")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Configure the global AsyncSessionLocal to use our test engine
    # This affects all modules importing AsyncSessionLocal
    AsyncSessionLocal.configure(bind=engine)

    yield engine

    await engine.dispose()
    os.unlink(temp_db.name)

@pytest.mark.asyncio
async def test_handle_sticky_message_update_deletes_corrupted_record(test_db):
    """Test that handle_sticky_message_update deletes records with missing draft_session_id"""
    
    async with AsyncSessionLocal() as session:
        # 1. Create a corrupted sticky message (missing draft_session_id)
        corrupted_message = Message(
            guild_id="123",
            channel_id="456",
            message_id="789",
            content="Sticky Content",
            view_metadata={"view_type": "draft"}, # Missing draft_session_id
            is_sticky=True,
            message_count=10,
            last_activity=time.time(),
            last_update_time=0
        )
        session.add(corrupted_message)
        await session.commit()
    
    # 2. Call handle_sticky_message_update
    mock_bot = AsyncMock()
    
    # We need a fresh session for the function call, mimicking real usage pattern
    # The function takes specific session arg, so we pass one
    async with AsyncSessionLocal() as session:
        # Re-fetch the object attached to this session
        result = await session.execute(select(Message).filter_by(channel_id="456"))
        msg = result.scalars().first()
        assert msg is not None, "Setup failed: message not found in DB"
        
        # The function should return CLEANED_UP and delete the record
        result = await handle_sticky_message_update(msg, mock_bot, session)

        assert result == StickyUpdateResult.CLEANED_UP, "Function should return CLEANED_UP when handling corruption"
        
        # Verify deletion in the same session (flush happens in delete helper but not commit)
        # The helper performs session.delete(msg). The changes are pending.
        # We can try to select it again.
        
        # Actually, let's verify in a NEW session to ensure it persisted if the function commits?
        # The helper function 'handle_sticky_message_update' calls session.commit() at the end.
        
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Message).filter_by(channel_id="456"))
        msg = result.scalars().first()
        assert msg is None, "Corrupted message should have been deleted from DB"


# --- #wheres-the-draft notifications ---------------------------------------

async def _seed_draft(session_id, session_type, tournament_match_id=None):
    """One draft session row, the way a launched draft leaves it."""
    from models.draft_session import DraftSession
    async with AsyncSessionLocal() as db:
        async with db.begin():
            db.add(DraftSession(
                session_id=session_id,
                guild_id="1355718878298116096",
                draft_id=f"DRAFT-{session_id}",
                draft_channel_id="900",
                message_id="800",
                session_type=session_type,
                tournament_match_id=tournament_match_id,
            ))


def _sticky(session_id):
    return Message(
        guild_id="1355718878298116096",
        channel_id="900",
        message_id="800",
        view_metadata={"view_type": "draft", "draft_session_id": session_id},
        notification_message_id=None,
    )


def _guild_with_channel():
    """A guild whose #wheres-the-draft exists and records what it is sent."""
    channel = MagicMock()
    channel.name = "wheres-the-draft"
    channel.send = AsyncMock(return_value=MagicMock(id=4242))
    guild = MagicMock()
    guild.id = 1355718878298116096
    guild.channels = [channel]
    guild.roles = []
    return guild, channel


@pytest.mark.asyncio
async def test_a_tournament_match_is_not_announced_in_wheres_the_draft(test_db):
    """A tournament match's teams are already fixed, so announcing it invites
    people to a draft they cannot join -- under a line reading "Looking for
    Drafters", which is not true of it either.

    It reached that line because the ping keys only on session_type: a
    tournament match is 'premade', which is absent from every guild's
    session_roles, so find_session_role fell through to the default drafter
    role -- "Cube Drafter" in all six configs.
    """
    from unittest.mock import patch
    from database.message_management import DraftStickyStrategy

    await _seed_draft("t-1", "premade", tournament_match_id=7)
    guild, channel = _guild_with_channel()

    with patch("database.message_management._get_guild", AsyncMock(return_value=guild)), \
         patch("database.message_management.find_notification_channel",
               AsyncMock(return_value=channel)):
        result = await DraftStickyStrategy()._post_or_update_notification(
            MagicMock(), _sticky("t-1"))

    channel.send.assert_not_awaited()
    assert result is None


@pytest.mark.asyncio
async def test_an_ordinary_draft_is_still_announced(test_db):
    """The other side of the rule: the channel is how people find a draft they
    can actually join, so a normal draft must keep its announcement."""
    from unittest.mock import patch
    from database.message_management import DraftStickyStrategy

    await _seed_draft("d-1", "random")
    guild, channel = _guild_with_channel()

    with patch("database.message_management._get_guild", AsyncMock(return_value=guild)), \
         patch("database.message_management.find_notification_channel",
               AsyncMock(return_value=channel)):
        result = await DraftStickyStrategy()._post_or_update_notification(
            MagicMock(), _sticky("d-1"))

    channel.send.assert_awaited_once()
    assert "Looking for Drafters" in channel.send.await_args.kwargs["content"]
    assert result == "4242"
