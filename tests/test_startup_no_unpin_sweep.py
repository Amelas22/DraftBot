"""Startup does not go looking for old quiz messages to unpin.

The sweep walked every quiz session older than the re-register cutoff and tried
to unpin each one. In production that was 959 messages, every one failing on
Manage Messages in a single channel, at roughly 39 a minute -- half an hour of
Discord calls that could not succeed, whose rate limiting slowed everything else
the bot was doing, and which delayed every restoration queued behind it.

Asserted behaviourally rather than by reading the source: an old quiz session is
seeded and startup is run against it, and the test fails if anything reaches for
its message at all.
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import AsyncSessionLocal
from models.quiz_session import QuizSession


@pytest.mark.asyncio
async def test_old_quiz_messages_are_left_alone_at_startup(test_db):  # noqa: F811
    """An old quiz message must not be fetched, let alone unpinned."""
    from utils import QUIZ_REREGISTER_DAYS, re_register_views

    async with AsyncSessionLocal() as session:
        session.add(QuizSession(
            quiz_id="g1-1000", display_id=1, guild_id="g1",
            channel_id="4242", message_id="9999", draft_session_id="s-old",
            pack_trace_data={}, correct_answers=[], starting_seat=0, posted_by="1",
            posted_at=datetime.now() - timedelta(days=QUIZ_REREGISTER_DAYS + 30),
        ))
        await session.commit()

    channel = MagicMock()
    channel.fetch_message = AsyncMock(side_effect=AssertionError(
        "startup fetched an old quiz message; the unpin sweep is back"))
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)
    bot.add_view = MagicMock()

    await re_register_views(bot)

    channel.fetch_message.assert_not_awaited()
