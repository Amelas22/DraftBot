
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
