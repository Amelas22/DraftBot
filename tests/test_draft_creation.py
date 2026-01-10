"""
Unit tests for draft creation flows (random and premade drafts).

Tests cover:
- Random draft creation and database persistence
- Premade draft creation with default and custom team names
- Embed signup field validation (the bug fix)
- Message info storage
- Deletion time calculation
"""

import pytest
import pytest_asyncio
import tempfile
import os
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import select

from database.models_base import Base
from session import AsyncSessionLocal
from models.draft_session import DraftSession
from models.session_details import SessionDetails
from sessions.random_session import RandomSession
from sessions.premade_session import PremadeSession


# ============================================================================
# FIXTURES
# ============================================================================

@pytest_asyncio.fixture
async def test_db():
    """Create a temporary test database and return a test session factory."""
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.ext.asyncio import AsyncSession

    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{temp_db.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Create test session factory for dependency injection
    test_session_factory = sessionmaker(
        engine,
        expire_on_commit=False,
        class_=AsyncSession
    )

    yield test_session_factory

    await engine.dispose()
    os.unlink(temp_db.name)


def create_mock_interaction(
    user_id="123456",
    guild_id="789012",
    display_name="TestUser"
):
    """Factory function to create mock Discord interactions."""
    interaction = MagicMock(spec_set=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = int(user_id)
    interaction.user.display_name = display_name
    interaction.guild_id = int(guild_id)
    interaction.guild = MagicMock()
    interaction.guild.id = int(guild_id)

    # Mock async methods
    interaction.response = AsyncMock()
    interaction.original_response = AsyncMock()

    # Mock message return
    mock_message = MagicMock()
    mock_message.id = "987654321"
    mock_message.channel = MagicMock()
    mock_message.channel.id = "111222333"
    interaction.original_response.return_value = mock_message

    interaction.client = MagicMock()
    return interaction


def create_session_details(interaction, cube_choice="test-cube"):
    """Create SessionDetails with optional overrides."""
    details = SessionDetails(interaction)
    details.cube_choice = cube_choice
    return details


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

async def get_draft_by_session_id(session_id: str, session_factory):
    """Helper to fetch draft from test database."""
    async with session_factory() as session:
        query = select(DraftSession).where(
            DraftSession.session_id == session_id
        )
        result = await session.execute(query)
        return result.scalar_one_or_none()


# ============================================================================
# RANDOM DRAFT TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_random_draft_basic_creation(test_db):
    """Test successful creation of a random draft with default values."""
    # Arrange
    interaction = create_mock_interaction()
    session_details = create_session_details(interaction)
    random_session = RandomSession(session_details, session_factory=test_db)

    # Act
    mock_draft_manager = MagicMock()
    mock_draft_manager.keep_connection_alive = AsyncMock()
    mock_draft_manager.socket_client = MagicMock()
    mock_draft_manager.socket_client.connected = False

    with patch('sessions.base_session.DraftSetupManager', return_value=mock_draft_manager), \
         patch('sessions.base_session.PersistentView'), \
         patch('sessions.base_session.make_message_sticky', new_callable=AsyncMock), \
         patch('sessions.base_session.get_session_deletion_hours', return_value=5), \
         patch('sessions.base_session.get_cube_thumbnail_url', return_value='https://example.com/thumb.jpg'):
        await random_session.create_draft_session(interaction, interaction.client)

    # Assert - Core functionality
    draft = await get_draft_by_session_id(session_details.session_id, test_db)
    assert draft is not None, "DraftSession should be created in database"
    assert draft.session_type == "random", "session_type should be 'random'"
    assert draft.cube == session_details.cube_choice, "cube should be stored"


@pytest.mark.asyncio
async def test_random_draft_has_signup_field(test_db):
    """Verify random draft has Sign-Ups field, not team fields."""
    # Arrange
    interaction = create_mock_interaction()
    session_details = create_session_details(interaction)
    random_session = RandomSession(session_details)

    # Act
    with patch('sessions.base_session.get_cube_thumbnail_url', return_value='https://example.com/thumb.jpg'):
        embed = random_session.create_embed()

    # Assert - Random drafts should have Sign-Ups field
    signup_field = next((f for f in embed.fields if f.name == "Sign-Ups:"), None)
    assert signup_field is not None, "Random drafts should have Sign-Ups field"

    # Assert - Random drafts should NOT have team-specific fields
    team_a_field = next((f for f in embed.fields if "Team A" in f.name), None)
    team_b_field = next((f for f in embed.fields if "Team B" in f.name), None)
    assert team_a_field is None, "Random drafts should not have Team A field"
    assert team_b_field is None, "Random drafts should not have Team B field"


# ============================================================================
# PREMADE DRAFT TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_premade_draft_basic_creation(test_db):
    """Test successful creation of premade draft with default team names."""
    # Arrange
    interaction = create_mock_interaction()
    session_details = create_session_details(interaction)
    premade_session = PremadeSession(session_details, session_factory=test_db)

    # Act
    # Mock DraftSetupManager with AsyncMock for keep_connection_alive
    mock_draft_manager = MagicMock()
    mock_draft_manager.keep_connection_alive = AsyncMock()
    mock_draft_manager.socket_client = MagicMock()
    mock_draft_manager.socket_client.connected = False

    with patch('sessions.base_session.DraftSetupManager', return_value=mock_draft_manager), \
         patch('sessions.base_session.PersistentView'), \
         patch('sessions.base_session.make_message_sticky', new_callable=AsyncMock), \
         patch('sessions.base_session.get_session_deletion_hours', return_value=5), \
         patch('sessions.base_session.get_cube_thumbnail_url', return_value='https://example.com/thumb.jpg'):
        await premade_session.create_draft_session(interaction, interaction.client)

    # Assert - Database
    draft = await get_draft_by_session_id(session_details.session_id, test_db)

    assert draft is not None, "DraftSession should be created"
    assert draft.session_type == "premade", "session_type should be 'premade'"
    assert draft.team_a_name == "Team A", "team_a_name should default to 'Team A'"
    assert draft.team_b_name == "Team B", "team_b_name should default to 'Team B'"
    assert draft.guild_id == str(interaction.guild_id), "guild_id should be stored"
    assert draft.tracked_draft is True, "tracked_draft should be True"


@pytest.mark.asyncio
async def test_premade_draft_custom_team_names(test_db):
    """Edge case - create premade draft with custom team names."""
    # Arrange
    interaction = create_mock_interaction()
    session_details = create_session_details(interaction)
    session_details.team_a_name = "Dragons"
    session_details.team_b_name = "Phoenixes"
    premade_session = PremadeSession(session_details, session_factory=test_db)

    # Act
    # Mock DraftSetupManager with AsyncMock for keep_connection_alive
    mock_draft_manager = MagicMock()
    mock_draft_manager.keep_connection_alive = AsyncMock()
    mock_draft_manager.socket_client = MagicMock()
    mock_draft_manager.socket_client.connected = False

    with patch('sessions.base_session.DraftSetupManager', return_value=mock_draft_manager), \
         patch('sessions.base_session.PersistentView'), \
         patch('sessions.base_session.make_message_sticky', new_callable=AsyncMock), \
         patch('sessions.base_session.get_session_deletion_hours', return_value=5), \
         patch('sessions.base_session.get_cube_thumbnail_url', return_value='https://example.com/thumb.jpg'):
        await premade_session.create_draft_session(interaction, interaction.client)

    # Assert - Database stores custom names
    draft = await get_draft_by_session_id(session_details.session_id, test_db)

    assert draft.team_a_name == "Dragons", "Should store custom team_a_name"
    assert draft.team_b_name == "Phoenixes", "Should store custom team_b_name"

    # Assert - Embed contains custom names
    with patch('sessions.base_session.get_cube_thumbnail_url', return_value='https://example.com/thumb.jpg'):
        embed = premade_session.create_embed()

    dragons_field = next((f for f in embed.fields if "Dragons" in f.name), None)
    phoenixes_field = next((f for f in embed.fields if "Phoenixes" in f.name), None)

    assert dragons_field is not None, "Embed should have Dragons field"
    assert phoenixes_field is not None, "Embed should have Phoenixes field"
    assert "No players yet" in dragons_field.value, "Dragons field should show 'No players yet'"
    assert "No players yet" in phoenixes_field.value, "Phoenixes field should show 'No players yet'"


@pytest.mark.asyncio
async def test_premade_draft_has_team_fields(test_db):
    """Verify premade draft has team fields, not generic Sign-Ups field."""
    # Arrange
    interaction = create_mock_interaction()
    session_details = create_session_details(interaction)
    premade_session = PremadeSession(session_details)

    # Act
    with patch('sessions.base_session.get_cube_thumbnail_url', return_value='https://example.com/thumb.jpg'):
        embed = premade_session.create_embed()

    # Assert - Premade drafts should have team-specific fields
    team_a_field = next((f for f in embed.fields if "Team A" in f.name), None)
    team_b_field = next((f for f in embed.fields if "Team B" in f.name), None)
    assert team_a_field is not None, "Premade drafts should have Team A field"
    assert team_b_field is not None, "Premade drafts should have Team B field"

    # Assert - Premade drafts should NOT have generic Sign-Ups field
    signup_field = next((f for f in embed.fields if f.name == "Sign-Ups:"), None)
    assert signup_field is None, "Premade drafts should NOT have Sign-Ups field"


# ============================================================================
# COMMON FUNCTIONALITY TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_draft_message_info_stored(test_db):
    """Verify message_id and channel_id are stored after creation."""
    # Arrange
    interaction = create_mock_interaction()
    mock_message = MagicMock()
    mock_message.id = "555666777"
    mock_message.channel = MagicMock()
    mock_message.channel.id = "888999000"
    interaction.original_response.return_value = mock_message

    session_details = create_session_details(interaction)
    random_session = RandomSession(session_details, session_factory=test_db)

    # Act
    # Mock DraftSetupManager with AsyncMock for keep_connection_alive
    mock_draft_manager = MagicMock()
    mock_draft_manager.keep_connection_alive = AsyncMock()
    mock_draft_manager.socket_client = MagicMock()
    mock_draft_manager.socket_client.connected = False

    with patch('sessions.base_session.DraftSetupManager', return_value=mock_draft_manager), \
         patch('sessions.base_session.PersistentView'), \
         patch('sessions.base_session.make_message_sticky', new_callable=AsyncMock), \
         patch('sessions.base_session.get_session_deletion_hours', return_value=5), \
         patch('sessions.base_session.get_cube_thumbnail_url', return_value='https://example.com/thumb.jpg'):
        await random_session.create_draft_session(interaction, interaction.client)

    # Assert
    draft = await get_draft_by_session_id(session_details.session_id, test_db)

    assert draft.message_id == "555666777", "message_id should be stored as string"
    assert draft.draft_channel_id == "888999000", "draft_channel_id should be stored as string"


@pytest.mark.asyncio
async def test_draft_deletion_time_set(test_db):
    """Verify deletion_time is set based on guild config."""
    # Arrange
    interaction = create_mock_interaction()
    session_details = create_session_details(interaction)
    random_session = RandomSession(session_details, session_factory=test_db)

    # Act - Mock config to return 5 hours
    # Mock DraftSetupManager with AsyncMock for keep_connection_alive
    mock_draft_manager = MagicMock()
    mock_draft_manager.keep_connection_alive = AsyncMock()
    mock_draft_manager.socket_client = MagicMock()
    mock_draft_manager.socket_client.connected = False

    with patch('sessions.base_session.DraftSetupManager', return_value=mock_draft_manager), \
         patch('sessions.base_session.PersistentView'), \
         patch('sessions.base_session.make_message_sticky', new_callable=AsyncMock), \
         patch('sessions.base_session.get_session_deletion_hours', return_value=5), \
         patch('sessions.base_session.get_cube_thumbnail_url', return_value='https://example.com/thumb.jpg'):
        await random_session.create_draft_session(interaction, interaction.client)

    # Assert
    draft = await get_draft_by_session_id(session_details.session_id, test_db)

    assert draft.deletion_time is not None, "deletion_time should be set"
    assert draft.deletion_time > draft.draft_start_time, "deletion_time should be after draft_start_time"

    # Check time difference is approximately 5 hours (with some tolerance)
    time_diff = draft.deletion_time - draft.draft_start_time
    expected_diff = timedelta(hours=5)

    # Allow 1 minute tolerance for test execution time
    assert abs(time_diff - expected_diff) < timedelta(minutes=1), "deletion_time should be ~5 hours after start"
