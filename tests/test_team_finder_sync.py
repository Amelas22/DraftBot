from unittest.mock import AsyncMock, patch

import pytest

from database.db_session import db_session
from services import team_finder_sync
from services.team_finder import Notice, WRITE_LOCK


@pytest.mark.asyncio
async def test_flag_off_does_nothing(test_db):
    with patch.object(team_finder_sync, "scheduler_enabled", return_value=False), \
            patch.object(team_finder_sync.team_finder, "reconcile_registered",
                         new=AsyncMock()) as rec:
        await team_finder_sync.run_reconcile(object(), "g1")
    rec.assert_not_called()


@pytest.mark.asyncio
async def test_notices_sent_after_lock_released_and_failures_swallowed(test_db):
    seen = []

    async def fake_notify(bot, notice, guild_id):
        seen.append((notice.user_id, WRITE_LOCK.locked()))
        if notice.user_id == "a":
            raise RuntimeError("dm")
        return True

    with patch.object(team_finder_sync, "scheduler_enabled", return_value=True), \
            patch.object(team_finder_sync.team_finder, "reconcile_registered",
                         new=AsyncMock(return_value=[Notice("a", "x"), Notice("b", "y")])), \
            patch.object(team_finder_sync, "notify", new=fake_notify):
        await team_finder_sync.run_reconcile(object(), "g1")
    assert seen == [("a", False), ("b", False)]


@pytest.mark.asyncio
async def test_command_hook_never_raises():
    with patch.object(team_finder_sync, "run_reconcile",
                      new=AsyncMock(side_effect=RuntimeError("boom"))):
        await team_finder_sync.reconcile_after_command(object(), 5)
