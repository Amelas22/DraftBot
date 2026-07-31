"""refresh_open_staked_queues: re-render open staked queue messages so debt
markers reflect a just-completed settlement; always best-effort."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils import refresh_open_staked_queues


def _db_returning(sessions):
    result = MagicMock()
    result.scalars.return_value.all.return_value = sessions
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return MagicMock(return_value=ctx)


@pytest.mark.asyncio
async def test_refreshes_each_open_staked_session():
    sessions = [SimpleNamespace(session_id="s1"), SimpleNamespace(session_id="s2")]
    update = AsyncMock()
    with patch("utils.AsyncSessionLocal", _db_returning(sessions)), \
         patch("views.update_draft_message", update):
        await refresh_open_staked_queues(bot="BOT", guild_id="g1")
    assert update.await_count == 2
    assert {c.args[1] for c in update.await_args_list} == {"s1", "s2"}


@pytest.mark.asyncio
async def test_one_failed_refresh_does_not_stop_the_rest():
    sessions = [SimpleNamespace(session_id="s1"), SimpleNamespace(session_id="s2")]
    update = AsyncMock(side_effect=[RuntimeError("boom"), None])
    with patch("utils.AsyncSessionLocal", _db_returning(sessions)), \
         patch("views.update_draft_message", update):
        await refresh_open_staked_queues(bot="BOT", guild_id="g1")   # must not raise
    assert update.await_count == 2


@pytest.mark.asyncio
async def test_query_failure_is_swallowed():
    broken = MagicMock(side_effect=RuntimeError("db down"))
    with patch("utils.AsyncSessionLocal", broken):
        await refresh_open_staked_queues(bot="BOT", guild_id="g1")   # must not raise
