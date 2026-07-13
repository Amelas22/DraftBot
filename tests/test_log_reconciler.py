"""Tests for services/log_reconciler.py (Task 5 backup retry loop).

Uses a real sqlite-backed DB (same pattern as tests/test_stats_display.py:
configure the module-global `AsyncSessionLocal` used by
`database.db_session.db_session()` to point at a temp file) so the actual
`select(...).filter(...)` predicates in the reconciler run for real, instead
of being replaced by a call-order-keyed mock.
"""
import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from database.db_session import AsyncSessionLocal
from database.models_base import Base
from models.draft_session import DraftSession
from services.log_reconciler import reconcile_publish_and_team_logs


@pytest_asyncio.fixture
async def test_db():
    """Real temp sqlite DB wired into the module-global AsyncSessionLocal that
    database.db_session.db_session() uses, so the reconciler's real queries
    run against seeded rows."""
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    temp_db.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{temp_db.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    AsyncSessionLocal.configure(bind=engine)

    yield engine

    await engine.dispose()
    os.unlink(temp_db.name)


async def _seed(session_id, *, captured_at, unlock_at, team_posted_at, data_received):
    async with AsyncSessionLocal() as session:
        session.add(DraftSession(
            session_id=session_id,
            draft_id=f"d{session_id}",
            cube=f"c{session_id}",
            guild_id="1",
            session_type="team",
            logs_captured_at=captured_at,
            unlock_at=unlock_at,
            team_logs_posted_at=team_posted_at,
            data_received=data_received,
        ))
        await session.commit()


@pytest.mark.asyncio
async def test_reconcile_posts_pending_team_logs_and_publishes_due_embeds(test_db):
    """A: captured, team_logs_posted_at NULL, unlock_at in the future ->
    team-pending (post_team_logs only, publish not due yet).
    B: captured, team-posted already, unlock_at in the PAST, data_received
    False -> due-publish (publish_draft_log only).
    C: captured, team-posted already, unlock_at in the FUTURE, data_received
    False -> must NOT publish.

    This runs the REAL `select(...).filter(...)` against seeded rows, so it
    fails if the `unlock_at <= now` guard is ever removed or weakened (see
    task-5-report.md for the RED/GREEN demonstration)."""
    now = datetime.now()
    await _seed("A", captured_at=now, unlock_at=now + timedelta(hours=3),
                team_posted_at=None, data_received=False)
    await _seed("B", captured_at=now, unlock_at=now - timedelta(minutes=1),
                team_posted_at=now, data_received=False)
    await _seed("C", captured_at=now, unlock_at=now + timedelta(hours=3),
                team_posted_at=now, data_received=False)

    bot = MagicMock()
    fake_mgr = MagicMock()
    fake_mgr.publish_draft_log = AsyncMock()
    fake_mgr.set_bot_instance = MagicMock()
    MgrCls = MagicMock(return_value=fake_mgr)
    MgrCls.get_active_manager = MagicMock(return_value=None)

    with patch("services.log_reconciler.post_team_logs", AsyncMock()) as team, \
         patch("services.log_reconciler.DraftSetupManager", MgrCls):
        await reconcile_publish_and_team_logs(bot)

    team.assert_awaited_once_with("A", bot)              # only the team-pending one
    MgrCls.assert_called_once()                          # transient manager built once
    assert MgrCls.call_args.kwargs["session_id"] == "B"  # ...for B, not C
    fake_mgr.publish_draft_log.assert_awaited_once()      # only the due one (B, not C)


@pytest.mark.asyncio
async def test_reconcile_does_not_leak_transient_manager_into_active_managers(test_db):
    """Fix A: DraftSetupManager.__init__ unconditionally registers itself into
    the module-global ACTIVE_MANAGERS registry. A transient manager built only
    to call publish_draft_log() must be removed from that registry afterward,
    not left leaking there on every reconciler tick."""
    now = datetime.now()
    await _seed("T1", captured_at=now, unlock_at=now - timedelta(minutes=1),
                team_posted_at=now, data_received=False)

    fake_registry = {}

    def _construct(*, session_id, draft_id, cube_id, guild_id):
        m = MagicMock()
        m.publish_draft_log = AsyncMock()
        m.set_bot_instance = MagicMock()
        fake_registry[session_id] = m   # mimic real __init__'s ACTIVE_MANAGERS[session_id] = self
        return m

    MgrCls = MagicMock(side_effect=_construct)
    MgrCls.get_active_manager = MagicMock(side_effect=fake_registry.get)

    with patch("services.log_reconciler.DraftSetupManager", MgrCls), \
         patch("services.log_reconciler.ACTIVE_MANAGERS", fake_registry):
        await reconcile_publish_and_team_logs(MagicMock())

    MgrCls.get_active_manager.assert_any_call("T1")
    MgrCls.assert_called_once()
    assert "T1" not in fake_registry, "transient manager leaked into ACTIVE_MANAGERS"


@pytest.mark.asyncio
async def test_reconcile_reuses_active_manager_without_clobbering_registry(test_db):
    """Fix A: if a live manager is already registered for the session_id (e.g.
    still in its delay window before disconnecting), the reconciler must reuse
    it instead of constructing a transient one that would silently clobber the
    registry entry."""
    now = datetime.now()
    await _seed("T2", captured_at=now, unlock_at=now - timedelta(minutes=1),
                team_posted_at=now, data_received=False)

    live_manager = MagicMock()
    live_manager.publish_draft_log = AsyncMock()
    live_manager.set_bot_instance = MagicMock()
    fake_registry = {"T2": live_manager}

    MgrCls = MagicMock()
    MgrCls.get_active_manager = MagicMock(side_effect=fake_registry.get)

    with patch("services.log_reconciler.DraftSetupManager", MgrCls), \
         patch("services.log_reconciler.ACTIVE_MANAGERS", fake_registry):
        await reconcile_publish_and_team_logs(MagicMock())

    MgrCls.assert_not_called()                        # no transient construction
    live_manager.publish_draft_log.assert_awaited_once()
    assert fake_registry["T2"] is live_manager        # not clobbered or removed
