"""Unit tests for DraftSetupManager.capture_draft_log (Slice 1)."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.draft_setup_manager import DraftSetupManager


def _manager():
    """A DraftSetupManager without running __init__ (skips socket setup)."""
    m = DraftSetupManager.__new__(DraftSetupManager)
    m.session_id = "sid"
    m.draft_id = "ABC123"
    m.cube_id = "MyCube"
    m.session_type = "premade"
    m.guild_id = "42"
    m.logger = MagicMock()
    return m


def _draft_data():
    return {
        "sessionID": "DBABC123",
        "time": 1700000000000,
        "users": {
            "u1": {"userName": "Alice", "seatNum": 0, "picks": [{"booster": [1]}]},
            "u2": {"userName": "Bob", "seatNum": 1, "picks": [{"booster": [2]}]},
        },
    }


def _mock_db_session(draft_session):
    """Patch target for `db_session()` -> async ctx mgr yielding a session whose
    execute().scalar_one_or_none() returns `draft_session`."""
    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = draft_session
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return MagicMock(return_value=ctx), session


@pytest.mark.asyncio
async def test_capture_stores_data_and_stamps_without_publishing():
    m = _manager()
    ds = SimpleNamespace(session_id="sid", sign_ups={"d1": "Alice", "d2": "Bob"},
                         draft_data=None, pack_first_picks=None, logs_captured_at=None,
                         data_received=False)
    db_factory, session = _mock_db_session(ds)
    with patch("services.draft_setup_manager.db_session", db_factory), \
         patch.object(DraftSetupManager, "save_to_digitalocean_spaces", AsyncMock(return_value=True)) as spaces, \
         patch.object(DraftSetupManager, "get_pack_first_picks", MagicMock(return_value={})), \
         patch.object(DraftSetupManager, "send_magicprotools_embed", AsyncMock()) as embed:
        ok = await m.capture_draft_log(_draft_data())

    assert ok is True
    spaces.assert_awaited_once()
    embed.assert_not_called()
    assert ds.data_received is False
    assert ds.draft_data is not None
    assert ds.logs_captured_at is not None
    session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_capture_is_idempotent():
    m = _manager()
    ds = SimpleNamespace(session_id="sid", sign_ups={}, draft_data=None,
                         pack_first_picks=None, logs_captured_at=datetime.now(),
                         data_received=False)
    db_factory, _ = _mock_db_session(ds)
    with patch("services.draft_setup_manager.db_session", db_factory), \
         patch.object(DraftSetupManager, "save_to_digitalocean_spaces", AsyncMock(return_value=True)) as spaces:
        ok = await m.capture_draft_log(_draft_data())

    assert ok is True
    spaces.assert_not_called()


@pytest.mark.asyncio
async def test_capture_no_data_is_noop():
    m = _manager()
    with patch.object(DraftSetupManager, "save_to_digitalocean_spaces", AsyncMock()) as spaces:
        ok = await m.capture_draft_log(None)
    assert ok is False
    spaces.assert_not_called()


@pytest.mark.asyncio
async def test_capture_spaces_failure_keeps_db_copy_without_stamp():
    """If the Spaces upload fails, the raw log is still saved to the DB, but
    logs_captured_at stays NULL so the capture is retryable."""
    m = _manager()
    ds = SimpleNamespace(session_id="sid", sign_ups={"d1": "Alice", "d2": "Bob"},
                         draft_data=None, pack_first_picks=None, logs_captured_at=None,
                         data_received=False)
    db_factory, session = _mock_db_session(ds)
    with patch("services.draft_setup_manager.db_session", db_factory), \
         patch.object(DraftSetupManager, "save_to_digitalocean_spaces", AsyncMock(return_value=False)) as spaces, \
         patch.object(DraftSetupManager, "get_pack_first_picks", MagicMock(return_value={})), \
         patch.object(DraftSetupManager, "send_magicprotools_embed", AsyncMock()) as embed:
        ok = await m.capture_draft_log(_draft_data())

    assert ok is False                      # upload failed
    spaces.assert_awaited_once()
    embed.assert_not_called()
    assert ds.draft_data is not None        # raw log still saved (data safety)
    assert ds.logs_captured_at is None      # NOT stamped -> retryable
