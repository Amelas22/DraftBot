"""
Tests for SignUpHistory integration with Discord view components.
"""
import pytest
import pytest_asyncio
import asyncio
import tempfile
import os
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from models.sign_up_history import SignUpHistory
from database.models_base import Base
from database.db_session import AsyncSessionLocal
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import select


class MockInteraction:
    """Mock Discord interaction for testing"""
    def __init__(self, user_id: str, display_name: str, guild_id: str):
        self.user = MagicMock()
        # Handle both numeric and non-numeric user IDs for testing flexibility
        try:
            self.user.id = int(user_id)
        except ValueError:
            # If not numeric, hash the string to create a fake numeric ID
            self.user.id = abs(hash(user_id)) % (10 ** 18)  # Discord snowflake-like ID
        self.user.display_name = display_name
        try:
            self.guild_id = int(guild_id)
        except ValueError:
            self.guild_id = abs(hash(guild_id)) % (10 ** 18)
        self.response = AsyncMock()


class MockDraftSession:
    """Mock draft session for testing"""
    def __init__(self, session_id: str, sign_ups=None, draftmancer_role_users=None):
        self.session_id = session_id
        self.sign_ups = sign_ups or {}
        self.draftmancer_role_users = draftmancer_role_users or []
        self.should_ping = False
        self.draft_start_time = datetime.now()
        self.session_type = "regular"
        self.min_stake = 10


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
async def test_signup_join_event_recorded(test_db):
    """Test that join events are recorded when user signs up"""
    from views import SignUpButton
    
    # Create mock objects
    session_id = "test_session_123"
    user_id = "456789"
    display_name = "TestUser"
    guild_id = "987654"
    
    interaction = MockInteraction(user_id, display_name, guild_id)
    draft_session = MockDraftSession(session_id)
    
    # Create the signup button
    signup_button = SignUpButton(session_id)
    signup_button.draft_session_id = session_id
    
    # Mock get_draft_session to return our mock
    with patch('views.get_draft_session', return_value=draft_session), \
         patch('views.AsyncSessionLocal') as mock_session:
        
        # Mock the database session context manager
        mock_db_session = AsyncMock()
        mock_session.return_value.__aenter__.return_value = mock_db_session
        mock_db_session.begin.return_value.__aenter__ = AsyncMock()
        mock_db_session.begin.return_value.__aexit__ = AsyncMock()
        mock_db_session.execute = AsyncMock()
        mock_db_session.commit = AsyncMock()
        
        # Call the callback - this should record the signup event
        try:
            await signup_button.callback(interaction)
        except Exception:
            # We expect some errors due to incomplete mocking, but the SignUpHistory call should work
            pass
    
    # Verify the signup event was recorded
    async with AsyncSessionLocal() as session:
        query = select(SignUpHistory).where(
            SignUpHistory.session_id == session_id,
            SignUpHistory.user_id == user_id,
            SignUpHistory.action == "join"
        )
        result = await session.execute(query)
        record = result.scalar_one_or_none()
        
        assert record is not None
        assert record.session_id == session_id
        assert record.user_id == user_id
        assert record.user_display_name == display_name
        assert record.action == "join"
        assert record.guild_id == guild_id


@pytest.mark.asyncio
async def test_signup_leave_event_recorded(test_db):
    """Test that leave events are recorded when user cancels signup"""
    from views import CancelSignUpButton
    
    # Create mock objects
    session_id = "test_session_456"
    user_id = "789123"
    display_name = "LeaveUser"
    guild_id = "654321"
    
    interaction = MockInteraction(user_id, display_name, guild_id)
    draft_session = MockDraftSession(session_id, sign_ups={user_id: display_name})
    
    # Create the cancel button
    cancel_button = CancelSignUpButton(session_id)
    cancel_button.draft_session_id = session_id
    
    # Mock get_draft_session to return our mock
    with patch('views.get_draft_session', return_value=draft_session), \
         patch('views.AsyncSessionLocal') as mock_session:
        
        # Mock the database session context manager
        mock_db_session = AsyncMock()
        mock_session.return_value.__aenter__.return_value = mock_db_session
        mock_db_session.begin.return_value.__aenter__ = AsyncMock()
        mock_db_session.begin.return_value.__aexit__ = AsyncMock()
        mock_db_session.execute = AsyncMock()
        mock_db_session.commit = AsyncMock()
        
        # Call the callback - this should record the leave event
        try:
            await cancel_button.callback(interaction)
        except Exception:
            # We expect some errors due to incomplete mocking, but the SignUpHistory call should work
            pass
    
    # Verify the leave event was recorded
    async with AsyncSessionLocal() as session:
        query = select(SignUpHistory).where(
            SignUpHistory.session_id == session_id,
            SignUpHistory.user_id == user_id,
            SignUpHistory.action == "leave"
        )
        result = await session.execute(query)
        record = result.scalar_one_or_none()
        
        assert record is not None
        assert record.session_id == session_id
        assert record.user_id == user_id
        assert record.user_display_name == display_name
        assert record.action == "leave"
        assert record.guild_id == guild_id


@pytest.mark.asyncio
async def test_user_removal_leave_event_recorded(test_db):
    """Test that leave events are recorded when admin removes a user"""
    from views import UserRemovalSelect
    from discord import SelectOption
    
    # Create mock objects
    session_id = "test_session_789"
    user_id_to_remove = "111222"
    removed_user_name = "RemovedUser"
    guild_id = "555666"
    admin_user_id = "admin123"
    
    interaction = MockInteraction(admin_user_id, "AdminUser", guild_id)
    draft_session = MockDraftSession(session_id, sign_ups={user_id_to_remove: removed_user_name})
    
    # Create the removal select
    options = [SelectOption(label=removed_user_name, value=user_id_to_remove)]
    removal_select = UserRemovalSelect(options, session_id)
    removal_select.values = [user_id_to_remove]  # Simulate user selection
    
    # Mock get_draft_session to return our mock
    with patch('views.get_draft_session', return_value=draft_session), \
         patch('views.AsyncSessionLocal') as mock_session:
        
        # Mock the database session context manager
        mock_db_session = AsyncMock()
        mock_session.return_value.__aenter__.return_value = mock_db_session
        mock_db_session.begin.return_value.__aenter__ = AsyncMock()
        mock_db_session.begin.return_value.__aexit__ = AsyncMock()
        mock_db_session.execute = AsyncMock()
        mock_db_session.commit = AsyncMock()
        
        # Call the callback - this should record the leave event
        try:
            await removal_select.callback(interaction)
        except Exception:
            # We expect some errors due to incomplete mocking, but the SignUpHistory call should work
            pass
    
    # Verify the leave event was recorded
    async with AsyncSessionLocal() as session:
        query = select(SignUpHistory).where(
            SignUpHistory.session_id == session_id,
            SignUpHistory.user_id == user_id_to_remove,
            SignUpHistory.action == "leave"
        )
        result = await session.execute(query)
        record = result.scalar_one_or_none()
        
        assert record is not None
        assert record.session_id == session_id
        assert record.user_id == user_id_to_remove
        assert record.user_display_name == removed_user_name
        assert record.action == "leave"
        assert record.guild_id == guild_id


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