"""Cancelling a queue for inactivity must stop its draft manager.

The cleanup task reaps a queue that never filled -- releasing its pool and
deleting its row -- but left its DraftSetupManager running. With no row and no
draft, nothing could end it early: it held its Draftmancer socket until
MANAGER_MAX_LIFETIME_MINUTES, seven hours after it was created. The manual
cancel button already stops the manager; this is the same end, reached by
the clock instead of a click.
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import make_manager, run_until_sleep, seed_queue
from services.draft_setup_manager import ACTIVE_MANAGERS


pytestmark = pytest.mark.usefixtures("clean_manager_registry")


async def run_one_cleanup_pass():
    from utils import cleanup_sessions_task

    bot = MagicMock()
    bot.get_channel.return_value = None
    with patch("utils.release_draft_pool", AsyncMock()):
        await run_until_sleep(cleanup_sessions_task(bot), 600)


@pytest.mark.asyncio
async def test_an_expired_queue_stops_its_manager(test_db):
    await seed_queue("idle", deletion_time=datetime.now() - timedelta(minutes=1))
    manager = make_manager(session_id="idle", draft_id="d-idle")

    await run_one_cleanup_pass()

    assert manager._should_disconnect, "the manager's loop was left running"
    assert "idle" not in ACTIVE_MANAGERS
    assert manager.draft_cancelled, "log collection was not called off"


@pytest.mark.asyncio
async def test_a_queue_still_open_keeps_its_manager(test_db):
    """Only the reaped queue's manager goes; a live one is untouched."""
    await seed_queue("open")
    manager = make_manager(session_id="open", draft_id="d-open")

    await run_one_cleanup_pass()

    assert not manager._should_disconnect
    assert ACTIVE_MANAGERS["open"] is manager


@pytest.mark.asyncio
async def test_a_manager_left_registered_by_a_failed_disconnect_is_reported():
    """disconnect_safely swallows a socket disconnect that raises, and the
    manager stays registered; cancelling must at least say so."""
    from loguru import logger

    from services.draft_setup_manager import DraftSetupManager

    manager = make_manager(session_id="stuck", draft_id="d-stuck")
    manager.socket_client.disconnect = AsyncMock(side_effect=OSError("socket gone"))
    warnings = []
    sink = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
    try:
        assert await DraftSetupManager.cancel_for_session("stuck")
    finally:
        logger.remove(sink)

    assert ACTIVE_MANAGERS.get("stuck") is manager, "precondition: the disconnect failure leaves it registered"
    assert any("still registered" in w for w in warnings)
