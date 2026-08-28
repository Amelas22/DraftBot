"""Two commands cancel a draft, at different points in its life, and each has to
point at the other.

/scrap stops the live Draftmancer session; /abandon voids the draft record and its
results afterwards. A player whose draft has stalled cannot tell which one they
want, and /scrap is what the bot advertises while a draft is running -- so the
player who most needs /abandon is the one most likely to type /scrap.

The sharp edge is /scrap's "Draft hasn't started yet." It fires on `not
manager.drafting`, which is true BOTH before the draft begins and after it ends.
Told "hasn't started yet" about a draft they just finished playing, a player has
no idea what to do next.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import make_draft_stub, sent_to_invoker


def make_ctx():
    ctx = MagicMock()
    ctx.author.id = 1
    ctx.channel_id = 100
    ctx.channel.id = 100
    ctx.channel.send = AsyncMock()
    ctx.defer = AsyncMock()
    ctx.followup.send = AsyncMock()
    return ctx


def make_scrap_manager(drafting=False, paused=True, draft_finished=False):
    return SimpleNamespace(drafting=drafting, draftPaused=paused,
                           draft_finished=draft_finished)


async def _run_scrap(cog, ctx, draft, draft_finished=False):
    manager = make_scrap_manager(draft_finished=draft_finished)
    with patch.object(cog, "_get_manager_for_channel",
                      new=AsyncMock(return_value=(manager, draft))):
        await cog._do_scrap(ctx)


@pytest.mark.asyncio
async def test_scrap_on_a_finished_draft_points_at_abandon(draft_control_cog):
    """The case that matters: they have played the draft and want it voided."""
    ctx = make_ctx()
    await _run_scrap(draft_control_cog, ctx, make_draft_stub("pairings"), draft_finished=True)

    said = sent_to_invoker(ctx)
    assert "/abandon" in said, f"never mentioned the command they need: {said!r}"
    assert "hasn't started" not in said, (
        f"told a finished draft it hasn't started: {said!r}")


@pytest.mark.asyncio
async def test_scrap_before_the_draft_starts_still_says_so(draft_control_cog):
    """The REACHABLE not-started state: teams exist but Draftmancer never got
    going, so session_stage is 'teams' and draft_finished is False. Driving
    session_stage=None here would assert on a state that cannot occur --
    _get_manager_for_channel filters NULL stages out before /scrap sees them."""
    ctx = make_ctx()
    await _run_scrap(draft_control_cog, ctx, make_draft_stub("teams"), draft_finished=False)

    said = sent_to_invoker(ctx)
    assert "hasn't started" in said, f"lost the not-started message: {said!r}"
    assert "/abandon" not in said, (
        f"pointed at /abandon for a draft with nothing to abandon: {said!r}")


@pytest.mark.asyncio
async def test_abandon_on_a_live_draft_points_at_scrap(draft_control_cog):
    """The mirror: a draft still running in Draftmancer is /scrap's business, and
    /abandon should say so rather than voiding a draft people are mid-pick in."""
    ctx = make_ctx()
    ctx.author.id = 99  # not a participant, so nothing proceeds to a vote
    draft = make_draft_stub("teams")
    with patch("cogs.draft_control.DraftSession.get_by_any_channel_id",
               new=AsyncMock(return_value=draft)), \
         patch("cogs.draft_control.is_bot_manager", new=AsyncMock(return_value=False)), \
         patch("cogs.draft_control.DraftSetupManager.is_drafting",
               new=MagicMock(return_value=True)):
        await draft_control_cog._do_abandon(ctx)

    said = sent_to_invoker(ctx)
    assert "/scrap" in said, f"never pointed at the command for a live draft: {said!r}"
