from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from helpers.quiz_threads import spawn_discussion_thread, post_quiz_share


@pytest.mark.asyncio
async def test_spawn_creates_thread_and_posts_starter():
    thread = SimpleNamespace(send=AsyncMock())
    message = SimpleNamespace(create_thread=AsyncMock(return_value=thread))
    out = await spawn_discussion_thread(message, "Quiz #1 — Discussion (spoilers)", "hello")
    assert out is thread
    message.create_thread.assert_awaited_once()
    assert message.create_thread.await_args.kwargs.get("name") == "Quiz #1 — Discussion (spoilers)"
    thread.send.assert_awaited_once_with("hello")


@pytest.mark.asyncio
async def test_spawn_returns_none_and_swallows_on_failure():
    message = SimpleNamespace(create_thread=AsyncMock(side_effect=RuntimeError("no perms")))
    out = await spawn_discussion_thread(message, "n", "s")
    assert out is None   # swallowed, no raise


@pytest.mark.asyncio
async def test_post_share_goes_to_thread_when_resolved():
    thread = SimpleNamespace(send=AsyncMock())
    guild = SimpleNamespace(get_thread=lambda tid: thread)
    channel = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(guild=guild, channel=channel,
                                  client=SimpleNamespace(get_channel=lambda tid: None))
    await post_quiz_share(interaction, "555", "my score")
    thread.send.assert_awaited_once_with("my score")
    channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_post_share_falls_back_to_channel_when_no_thread():
    guild = SimpleNamespace(get_thread=lambda tid: None)
    channel = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(guild=guild, channel=channel,
                                  client=SimpleNamespace(get_channel=lambda tid: None))
    await post_quiz_share(interaction, "555", "my score")
    channel.send.assert_awaited_once_with("my score")


@pytest.mark.asyncio
async def test_post_share_falls_back_when_no_message_id():
    channel = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(guild=None, channel=channel,
                                  client=SimpleNamespace(get_channel=lambda tid: None))
    await post_quiz_share(interaction, None, "my score")
    channel.send.assert_awaited_once_with("my score")


@pytest.mark.asyncio
async def test_post_share_falls_back_to_channel_when_thread_send_raises():
    """If the resolved thread's send fails (e.g. archived+locked), fall back to
    the channel rather than propagate to the Share button."""
    thread = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("thread locked")))
    guild = SimpleNamespace(get_thread=lambda tid: thread)
    channel = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(guild=guild, channel=channel,
                                  client=SimpleNamespace(get_channel=lambda tid: None))
    await post_quiz_share(interaction, "555", "my score")
    channel.send.assert_awaited_once_with("my score")


@pytest.mark.asyncio
async def test_post_share_swallows_when_both_targets_raise():
    """Never raises: if even the channel send fails, swallow (share is best-effort)."""
    thread = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("thread")))
    guild = SimpleNamespace(get_thread=lambda tid: thread)
    channel = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("channel")))
    interaction = SimpleNamespace(guild=guild, channel=channel,
                                  client=SimpleNamespace(get_channel=lambda tid: None))
    await post_quiz_share(interaction, "555", "my score")  # must not raise
