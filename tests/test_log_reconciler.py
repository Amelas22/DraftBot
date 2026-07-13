from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from services.log_reconciler import reconcile_publish_and_team_logs


def _rows():
    now = datetime.now()
    # A: captured, team logs not posted -> should post team logs
    a = SimpleNamespace(session_id="A", logs_captured_at=now, team_logs_posted_at=None,
                        data_received=False, unlock_at=now + timedelta(hours=3),
                        draft_id="da", cube="ca", guild_id="1", session_type="team")
    # B: captured, unlock passed, not published -> should publish
    b = SimpleNamespace(session_id="B", logs_captured_at=now, team_logs_posted_at=now,
                        data_received=False, unlock_at=now - timedelta(minutes=1),
                        draft_id="db", cube="cb", guild_id="1", session_type="team")
    # C: captured, unlock in future -> should NOT publish
    c = SimpleNamespace(session_id="C", logs_captured_at=now, team_logs_posted_at=now,
                        data_received=False, unlock_at=now + timedelta(hours=3),
                        draft_id="dc", cube="cc", guild_id="1", session_type="team")
    return a, b, c


@pytest.mark.asyncio
async def test_reconcile_posts_pending_team_logs_and_publishes_due_embeds():
    a, b, c = _rows()

    def _scalars_for(query_tag):
        m = MagicMock()
        m.all.return_value = {"team": [a], "publish": [b]}[query_tag]
        return m

    # Two selects per tick: pending-team-logs, then due-publish.
    calls = {"n": 0}
    async def _execute(stmt):
        calls["n"] += 1
        tag = "team" if calls["n"] == 1 else "publish"
        r = MagicMock(); r.scalars.return_value = _scalars_for(tag)
        return r
    session = MagicMock(); session.execute = _execute
    ctx = MagicMock(); ctx.__aenter__ = AsyncMock(return_value=session); ctx.__aexit__ = AsyncMock(return_value=None)

    bot = MagicMock()
    published = []
    fake_mgr = MagicMock(); fake_mgr.publish_draft_log = AsyncMock()

    with patch("services.log_reconciler.db_session", MagicMock(return_value=ctx)), \
         patch("services.log_reconciler.post_team_logs", AsyncMock()) as team, \
         patch("services.log_reconciler.DraftSetupManager", return_value=fake_mgr) as MgrCls:
        await reconcile_publish_and_team_logs(bot)

    team.assert_awaited_once_with("A", bot)          # only the pending one
    MgrCls.assert_called_once()                       # transient manager for publish
    fake_mgr.publish_draft_log.assert_awaited_once()  # only the due one (B, not C)
