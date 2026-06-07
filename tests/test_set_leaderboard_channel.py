"""TDD tests for the /set_leaderboard_channel enhancement: support
creating the channel if it doesn't already exist.

Behavior:
  - existing channel only         → persist record, no creation
  - new_channel_name only         → create channel, then persist record
  - both, or neither              → error message, no DB write
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def make_ctx(guild_id=1399789864961970337, guild_name="ArenaMax"):
    ctx = MagicMock()
    ctx.guild.id = guild_id
    ctx.guild.name = guild_name
    ctx.author.name = "admin"
    ctx.defer = AsyncMock()
    ctx.followup.send = AsyncMock()
    return ctx


def make_db_session_mocks(existing_record=None):
    """Build the async-context-manager + execute mocks used by the command."""
    mock_session = MagicMock()
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    select_result = MagicMock()
    select_result.scalar_one_or_none.return_value = existing_record
    mock_session.execute = AsyncMock(return_value=select_result)

    db_session_cm = MagicMock()
    db_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    db_session_cm.__aexit__ = AsyncMock(return_value=None)
    return mock_session, db_session_cm


@pytest.mark.asyncio
async def test_existing_channel_persists_record_without_creation():
    """Passing an existing channel preserves the original behavior — no creation call."""
    from cogs.admin_commands import AdminCommands

    ctx = make_ctx()
    ctx.guild.create_text_channel = AsyncMock()

    existing_channel = MagicMock()
    existing_channel.id = 555
    existing_channel.name = "existing-leaderboard"
    existing_channel.mention = "<#555>"

    mock_session, db_session_cm = make_db_session_mocks(existing_record=None)
    cog = AdminCommands(MagicMock())

    with patch("database.db_session.db_session", MagicMock(return_value=db_session_cm)):
        await cog.set_leaderboard_channel.callback(
            cog, ctx, channel=existing_channel, new_channel_name=None
        )

    ctx.guild.create_text_channel.assert_not_called()
    mock_session.add.assert_called_once()
    new_record = mock_session.add.call_args.args[0]
    assert new_record.channel_id == "555"


@pytest.mark.asyncio
async def test_new_channel_name_creates_channel_and_persists():
    """When only new_channel_name is given, the bot creates the channel and persists it."""
    from cogs.admin_commands import AdminCommands

    ctx = make_ctx()
    created_channel = MagicMock()
    created_channel.id = 999
    created_channel.name = "leaderboard"
    created_channel.mention = "<#999>"
    ctx.guild.create_text_channel = AsyncMock(return_value=created_channel)

    mock_session, db_session_cm = make_db_session_mocks(existing_record=None)
    cog = AdminCommands(MagicMock())

    with patch("database.db_session.db_session", MagicMock(return_value=db_session_cm)):
        await cog.set_leaderboard_channel.callback(
            cog, ctx, channel=None, new_channel_name="leaderboard"
        )

    ctx.guild.create_text_channel.assert_called_once_with(name="leaderboard")
    mock_session.add.assert_called_once()
    new_record = mock_session.add.call_args.args[0]
    assert new_record.channel_id == "999"


@pytest.mark.asyncio
async def test_both_arguments_errors_without_writing():
    """Providing both channel and new_channel_name is ambiguous → error, no DB writes."""
    from cogs.admin_commands import AdminCommands

    ctx = make_ctx()
    ctx.guild.create_text_channel = AsyncMock()

    existing_channel = MagicMock(); existing_channel.id = 555; existing_channel.mention = "<#555>"
    mock_session, db_session_cm = make_db_session_mocks(existing_record=None)
    cog = AdminCommands(MagicMock())

    with patch("database.db_session.db_session", MagicMock(return_value=db_session_cm)):
        await cog.set_leaderboard_channel.callback(
            cog, ctx, channel=existing_channel, new_channel_name="leaderboard",
        )

    ctx.guild.create_text_channel.assert_not_called()
    mock_session.add.assert_not_called()
    mock_session.commit.assert_not_called()
    ctx.followup.send.assert_called_once()
    sent_text = ctx.followup.send.call_args.args[0]
    assert "❌" in sent_text or "either" in sent_text.lower()


@pytest.mark.asyncio
async def test_neither_argument_errors_without_writing():
    """Providing neither argument → error, no DB writes."""
    from cogs.admin_commands import AdminCommands

    ctx = make_ctx()
    ctx.guild.create_text_channel = AsyncMock()
    mock_session, db_session_cm = make_db_session_mocks(existing_record=None)
    cog = AdminCommands(MagicMock())

    with patch("database.db_session.db_session", MagicMock(return_value=db_session_cm)):
        await cog.set_leaderboard_channel.callback(
            cog, ctx, channel=None, new_channel_name=None,
        )

    ctx.guild.create_text_channel.assert_not_called()
    mock_session.add.assert_not_called()
    mock_session.commit.assert_not_called()
    ctx.followup.send.assert_called_once()
    sent_text = ctx.followup.send.call_args.args[0]
    assert "❌" in sent_text or "either" in sent_text.lower()


@pytest.mark.asyncio
async def test_new_channel_name_handles_forbidden_gracefully():
    """If the bot lacks Manage Channels, surface a clean error instead of crashing."""
    import discord
    from cogs.admin_commands import AdminCommands

    ctx = make_ctx()
    ctx.guild.create_text_channel = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "Missing Permissions")
    )
    mock_session, db_session_cm = make_db_session_mocks(existing_record=None)
    cog = AdminCommands(MagicMock())

    with patch("database.db_session.db_session", MagicMock(return_value=db_session_cm)):
        await cog.set_leaderboard_channel.callback(
            cog, ctx, channel=None, new_channel_name="leaderboard",
        )

    mock_session.add.assert_not_called()
    ctx.followup.send.assert_called_once()
    sent_text = ctx.followup.send.call_args.args[0]
    assert "permission" in sent_text.lower() or "❌" in sent_text
