"""
Tests for SignUpHistory integration with Discord view components.
"""
import pytest
import pytest_asyncio
import asyncio
import tempfile
import os
import uuid
from models.sign_up_history import SignUpHistory
from database.models_base import Base
from database.db_session import AsyncSessionLocal
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import select


@pytest_asyncio.fixture
async def test_db():
    """Create a temporary test database"""
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
async def test_multiple_join_leave_events_sequence(test_db):
    """Test a sequence of join/leave events creates proper history"""
    session_id = "test_session_sequence"
    user_id = "sequence_user"
    display_name = "SequenceUser"
    guild_id = "sequence_guild"
    
    # Record a sequence of events
    await SignUpHistory.record_signup_event(
        session_id=session_id,
        user_id=user_id,
        display_name=display_name,
        action="join",
        guild_id=guild_id
    )
    
    await SignUpHistory.record_signup_event(
        session_id=session_id,
        user_id=user_id,
        display_name=display_name,
        action="leave",
        guild_id=guild_id
    )
    
    await SignUpHistory.record_signup_event(
        session_id=session_id,
        user_id=user_id,
        display_name=display_name,
        action="join",
        guild_id=guild_id
    )
    
    # Verify all events were recorded in correct order
    async with AsyncSessionLocal() as session:
        query = select(SignUpHistory).where(
            SignUpHistory.session_id == session_id,
            SignUpHistory.user_id == user_id
        ).order_by(SignUpHistory.timestamp)
        result = await session.execute(query)
        records = result.scalars().all()
        
        assert len(records) == 3
        assert records[0].action == "join"
        assert records[1].action == "leave"
        assert records[2].action == "join"
        
        # Verify timestamps are in chronological order
        assert records[0].timestamp <= records[1].timestamp <= records[2].timestamp


@pytest.mark.asyncio 
async def test_uuid_generation_uniqueness(test_db):
    """Test that each signup event gets a unique UUID"""
    session_id = "test_uuid_session"
    user_id = "uuid_user"
    display_name = "UUIDUser"
    guild_id = "uuid_guild"
    
    # Create multiple events rapidly
    event_count = 5
    for i in range(event_count):
        await SignUpHistory.record_signup_event(
            session_id=session_id,
            user_id=f"{user_id}_{i}",
            display_name=f"{display_name}_{i}",
            action="join",
            guild_id=guild_id
        )
    
    # Verify all UUIDs are unique
    async with AsyncSessionLocal() as session:
        query = select(SignUpHistory).where(SignUpHistory.session_id == session_id)
        result = await session.execute(query)
        records = result.scalars().all()
        
        assert len(records) == event_count
        
        # Check all UUIDs are valid and unique
        uuids = []
        for record in records:
            # Validate UUID format
            try:
                uuid_obj = uuid.UUID(record.id)
                uuids.append(record.id)
            except ValueError:
                pytest.fail(f"Invalid UUID: {record.id}")
        
        # Check uniqueness
        assert len(set(uuids)) == event_count, "Not all UUIDs are unique"


async def run_standalone_tests():
    """Run tests that don't require complex mocking."""
    from sqlalchemy.ext.asyncio import create_async_engine
    import tempfile
    import os
    
    # Create test database
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db.close()
    
    engine = create_async_engine(f"sqlite+aiosqlite:///{temp_db.name}")
    AsyncSessionLocal.configure(bind=engine)
    
    # Create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    tests = [
        ("Event Sequence", test_multiple_join_leave_events_sequence),
        ("UUID Uniqueness", test_uuid_generation_uniqueness),
    ]
    
    passed = 0
    print("SignUpHistory View Integration Tests")
    print("-" * 45)
    
    try:
        for test_name, test_func in tests:
            try:
                await test_func(engine)
                print(f"✅ {test_name}")
                passed += 1
            except Exception as e:
                print(f"❌ {test_name}: {e}")
        
        print("-" * 45)
        print(f"Results: {passed}/{len(tests)} tests passed")
        
        if passed < len(tests):
            print("Note: View component tests with mocking require pytest")
            
    finally:
        await engine.dispose()
        os.unlink(temp_db.name)


if __name__ == "__main__":
    asyncio.run(run_standalone_tests())