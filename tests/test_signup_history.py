"""
Unit tests for SignUpHistory model
"""
import pytest
import asyncio
import tempfile
import os
import uuid
from datetime import datetime
from models.sign_up_history import SignUpHistory
from database.models_base import Base
from database.db_session import AsyncSessionLocal
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy import select


@pytest.fixture
async def test_db():
    """Create a temporary test database"""
    # Create temporary database file
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db.close()
    
    # Create async engine with SQLite
    engine = create_async_engine(f"sqlite+aiosqlite:///{temp_db.name}")
    
    # Create all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    # Set up the session factory
    test_session = AsyncSessionLocal
    test_session.configure(bind=engine)
    
    yield engine
    
    # Cleanup
    await engine.dispose()
    os.unlink(temp_db.name)


@pytest.mark.asyncio
async def test_record_signup_event_join(test_db):
    """Test recording a join event"""
    session_id = "test_session_123"
    user_id = "user_456"
    display_name = "TestUser"
    action = "join"
    guild_id = "guild_789"
    
    # Record the signup event
    await SignUpHistory.record_signup_event(
        session_id=session_id,
        user_id=user_id,
        display_name=display_name,
        action=action,
        guild_id=guild_id
    )
    
    # Verify the record was created
    async with AsyncSessionLocal() as session:
        query = select(SignUpHistory).where(
            SignUpHistory.session_id == session_id,
            SignUpHistory.user_id == user_id,
            SignUpHistory.action == action
        )
        result = await session.execute(query)
        record = result.scalar_one_or_none()
        
        assert record is not None
        assert record.session_id == session_id
        assert record.user_id == user_id
        assert record.user_display_name == display_name
        assert record.action == action
        assert record.guild_id == guild_id
        assert record.timestamp is not None
        assert isinstance(record.timestamp, datetime)
        
        # Check that a valid UUID was generated
        try:
            uuid.UUID(record.id)
            uuid_is_valid = True
        except ValueError:
            uuid_is_valid = False
        assert uuid_is_valid, f"ID '{record.id}' is not a valid UUID"


@pytest.mark.asyncio 
async def test_record_signup_event_leave(test_db):
    """Test recording a leave event"""
    session_id = "test_session_456"
    user_id = "user_789"
    display_name = "LeaveUser"
    action = "leave"
    guild_id = "guild_123"
    
    # Record the signup event
    await SignUpHistory.record_signup_event(
        session_id=session_id,
        user_id=user_id,
        display_name=display_name,
        action=action,
        guild_id=guild_id
    )
    
    # Verify the record was created
    async with AsyncSessionLocal() as session:
        query = select(SignUpHistory).where(
            SignUpHistory.session_id == session_id,
            SignUpHistory.user_id == user_id,
            SignUpHistory.action == action
        )
        result = await session.execute(query)
        record = result.scalar_one_or_none()
        
        assert record is not None
        assert record.action == "leave"


@pytest.mark.asyncio
async def test_multiple_events_unique_ids(test_db):
    """Test that multiple events for the same user generate unique IDs"""
    session_id = "test_session_999"
    user_id = "user_999"
    display_name = "MultiUser"
    guild_id = "guild_999"
    
    # Record multiple events in quick succession
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
    
    # Verify all records were created with unique IDs
    async with AsyncSessionLocal() as session:
        query = select(SignUpHistory).where(
            SignUpHistory.session_id == session_id,
            SignUpHistory.user_id == user_id
        ).order_by(SignUpHistory.timestamp)
        result = await session.execute(query)
        records = result.scalars().all()
        
        assert len(records) == 3
        
        # Check that all IDs are unique
        ids = [record.id for record in records]
        assert len(set(ids)) == 3  # All unique
        
        # Check the sequence of actions
        assert records[0].action == "join"
        assert records[1].action == "leave"
        assert records[2].action == "join"


@pytest.mark.asyncio
async def test_repr_method(test_db):
    """Test the __repr__ method"""
    session_id = "test_session_repr"
    user_id = "user_repr"
    display_name = "ReprUser"
    action = "join"
    guild_id = "guild_repr"
    
    await SignUpHistory.record_signup_event(
        session_id=session_id,
        user_id=user_id,
        display_name=display_name,
        action=action,
        guild_id=guild_id
    )
    
    # Get the record and test __repr__
    async with AsyncSessionLocal() as session:
        query = select(SignUpHistory).where(SignUpHistory.session_id == session_id)
        result = await session.execute(query)
        record = result.scalar_one()
        
        repr_str = repr(record)
        assert "SignUpHistory" in repr_str
        assert session_id in repr_str
        assert user_id in repr_str
        assert action in repr_str


async def run_model_tests():
    """Run all SignUpHistory model tests."""
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
        ("Join Event Recording", test_record_signup_event_join),
        ("Leave Event Recording", test_record_signup_event_leave),
        ("Multiple Events Unique IDs", test_multiple_events_unique_ids),
        ("Model Representation", test_repr_method),
    ]
    
    passed = 0
    print("SignUpHistory Model Tests")
    print("-" * 30)
    
    try:
        for test_name, test_func in tests:
            try:
                await test_func(engine)
                print(f"✅ {test_name}")
                passed += 1
            except Exception as e:
                print(f"❌ {test_name}: {e}")
        
        print("-" * 30)
        print(f"Results: {passed}/{len(tests)} tests passed")
        
    finally:
        await engine.dispose()
        os.unlink(temp_db.name)
    
    return passed == len(tests)


if __name__ == "__main__":
    asyncio.run(run_model_tests())