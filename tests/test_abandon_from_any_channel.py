"""`/abandon` has to work from the channel a stalled draft is actually sitting in.

It resolved the draft with get_by_channel_id, which matches draft_chat_channel and
nothing else. But a draft stalls in the TEAM channels -- that is where players are
when a match never gets reported -- and running it there told them to go somewhere
else. get_by_any_channel_id already exists for this: it also searches channel_ids.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.draft_control import DraftControlCog

DRAFT_CHAT = 100
RED_TEAM_CHAT = 101


def make_ctx(channel_id, invoker_id="99"):   # 99 is in no draft
    ctx = MagicMock()
    ctx.author.id = int(invoker_id)
    ctx.channel_id = channel_id
    ctx.channel.id = channel_id
    ctx.channel.send = AsyncMock()
    ctx.defer = AsyncMock()
    ctx.followup.send = AsyncMock()
    ctx.guild.get_member = lambda _id: None
    return ctx


def make_draft(session_stage="pairings"):
    return SimpleNamespace(
        session_id="sess_123",
        session_stage=session_stage,
        draft_chat_channel=str(DRAFT_CHAT),
        channel_ids=[DRAFT_CHAT, RED_TEAM_CHAT],
        sign_ups={"1": "One", "2": "Two"},
    )


@pytest.fixture
def cog():
    return DraftControlCog(bot=MagicMock())


def _sent(ctx):
    """Everything the command said back to the invoker, as one string."""
    return " ".join(str(c.args[0]) for c in ctx.followup.send.await_args_list if c.args)


@pytest.mark.asyncio
async def test_abandon_resolves_the_draft_from_a_team_channel(cog):
    """The failure this fixes: a stalled draft lives in the team channels, and
    running /abandon there used to claim there was no draft here."""
    ctx = make_ctx(RED_TEAM_CHAT)
    with patch("cogs.draft_control.DraftSession.get_by_any_channel_id",
               new=AsyncMock(return_value=make_draft())), \
         patch("cogs.draft_control.is_bot_manager", new=AsyncMock(return_value=False)):
        await cog._do_abandon(ctx)

    # Resolved: it got as far as the participant check instead of "no draft here".
    assert "Only draft participants" in _sent(ctx), (
        f"never resolved the draft from a team channel: {_sent(ctx)!r}")


@pytest.mark.asyncio
async def test_abandon_still_works_from_the_draft_chat(cog):
    """The channel it already supported must keep working."""
    ctx = make_ctx(DRAFT_CHAT)
    with patch("cogs.draft_control.DraftSession.get_by_any_channel_id",
               new=AsyncMock(return_value=make_draft())), \
         patch("cogs.draft_control.is_bot_manager", new=AsyncMock(return_value=False)):
        await cog._do_abandon(ctx)

    assert "Only draft participants" in _sent(ctx)


@pytest.mark.asyncio
async def test_a_channel_belonging_to_no_draft_is_still_refused(cog):
    """Widening the lookup must not make the command fire anywhere."""
    ctx = make_ctx(999)
    with patch("cogs.draft_control.DraftSession.get_by_any_channel_id",
               new=AsyncMock(return_value=None)):
        await cog._do_abandon(ctx)

    said = _sent(ctx).lower()
    assert "no draft here" in said, f"expected a no-draft refusal, got: {said!r}"
    assert "draft chat" in said and "team channel" in said, (
        f"the refusal should name the channels that DO work, got: {said!r}")
