"""/scrap stops the Draftmancer session, not the draft.

A table that scraps usually restarts in the same session with /ready, teams
unchanged, and plays the draft to a result that settle_pool pays from the pool.
Refunding the entries at the scrap emptied that pool, so the restarted draft
completed paying nobody. Only the paths that really end a draft early return
entries: Cancel Draft, /abandon, the queue expiring, and cleanup abandoning a
draft that never got past drafting.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import make_draft_stub


def _ctx():
    ctx = MagicMock()
    ctx.author.id = 1
    ctx.channel.send = AsyncMock(return_value=MagicMock(edit=AsyncMock()))
    ctx.followup.send = AsyncMock()
    return ctx


def _manager(emitted=True):
    return SimpleNamespace(drafting=True, draftPaused=True, draft_finished=False,
                           mark_draft_cancelled=AsyncMock(),
                           socket_client=SimpleNamespace(emit=AsyncMock(return_value=emitted)))


async def _pass_scrap(cog, ctx, manager, held):
    """Run /scrap with the vote passing at once; return (release mock, final text)."""
    async def vote(*_a, on_pass, **_k):
        await on_pass()
        return True

    release = AsyncMock()
    draft = make_draft_stub("teams", guild_id="g")
    with patch.object(cog, "_get_manager_for_channel",
                      new=AsyncMock(return_value=(manager, draft))), \
            patch("cogs.draft_control.run_participant_vote", new=vote), \
            patch("cogs.draft_control._ping_text", return_value=""), \
            patch("cogs.draft_control.asyncio.sleep", new=AsyncMock()), \
            patch("cogs.draft_control.release_draft_pool", new=release), \
            patch("cogs.draft_control.pool_balance", new=AsyncMock(return_value=held)):
        await cog._do_scrap(ctx)
    final = ctx.channel.send.return_value.edit.await_args.kwargs["content"]
    return release, final


@pytest.mark.asyncio
async def test_a_scrap_leaves_the_entries_in_the_pool(draft_control_cog):
    ctx = _ctx()
    release, final = await _pass_scrap(draft_control_cog, ctx, _manager(), held=160)

    release.assert_not_awaited()
    assert "Draft canceled" in final
    assert "Cancel Draft" in final, f"never told a staked table how to get entries back: {final!r}"


@pytest.mark.asyncio
async def test_an_unstaked_scrap_says_nothing_about_entries(draft_control_cog):
    ctx = _ctx()
    release, final = await _pass_scrap(draft_control_cog, ctx, _manager(), held=0)

    release.assert_not_awaited()
    assert "Draft canceled" in final
    assert "prize pool" not in final, f"mentioned a pool the draft does not have: {final!r}"


@pytest.mark.asyncio
async def test_a_scrap_that_cannot_reach_draftmancer_still_moves_nothing(draft_control_cog):
    ctx = _ctx()
    manager = _manager(emitted=False)
    release, final = await _pass_scrap(draft_control_cog, ctx, manager, held=160)

    release.assert_not_awaited()
    assert manager.drafting is True
    assert "Could not reach Draftmancer" in final
