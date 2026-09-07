"""A finished draft must let go of its manager.

DraftSetupManager.keep_connection_alive is a `while True` whose only graceful
exit is `_should_disconnect`. Production ran 14 loops in one day and stopped 2:
the survivors each held a live socket and `current_draft_log` -- the whole
Draftmancer log, 450-750 KB -- until the box OOM-killed the bot at 1.22 GB.

Two separate defects put them there, one group each below.
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import make_manager
import services.draft_setup_manager as dsm
from services.draft_setup_manager import ACTIVE_MANAGERS


@pytest.fixture(autouse=True)
def _clean_registry():
    """__init__ registers every manager globally; don't leak between tests."""
    ACTIVE_MANAGERS.clear()
    yield
    ACTIVE_MANAGERS.clear()


def loopable(mgr):
    """Mock the I/O keep_connection_alive does, leaving its control flow real.

    The socket starts DOWN and reconnection is made to fail, so the loop
    terminates whether or not the fix is in place. That matters: a leaked
    manager reconnects and `continue`s without ever reaching the sleep, so it
    never yields to the event loop and even asyncio.wait_for cannot interrupt
    it -- the suite would hang instead of failing. Termination must never
    depend on the code under test.
    """
    mgr.socket_client.connected = False
    mgr.socket_client.disconnect = AsyncMock()
    mgr.socket_client.connect_with_retry = AsyncMock(return_value=True)
    mgr._reclaim_ownership_as_spectator = AsyncMock(return_value=True)
    mgr._handle_reconnection = AsyncMock(return_value=False)
    return mgr


# ---- defect B: teardown that silently did nothing ---------------------------

@pytest.mark.asyncio
async def test_disconnect_safely_stops_the_loop_even_if_the_socket_already_dropped():
    """The guard `if not connected: return` sat ABOVE `_should_disconnect = True`.

    At the end of a draft the players have usually left, so the socket is
    typically already down -- exactly when this method is asked to tear the
    manager down, and exactly when it did nothing at all.
    """
    mgr = make_manager()
    mgr.socket_client.connected = False

    await mgr.disconnect_safely()

    assert mgr._should_disconnect is True, (
        "a manager asked to disconnect must stop its loop even when the socket "
        "is already gone -- otherwise the loop reconnects it and runs forever")


@pytest.mark.asyncio
async def test_a_manager_told_to_disconnect_does_not_reconnect_itself():
    """The consequence of the above, through the real loop.

    _handle_reconnection succeeds whenever Draftmancer is reachable, so a
    dropped socket plus an unset flag is an infinite resurrection loop.
    """
    mgr = loopable(make_manager())

    await mgr.disconnect_safely()
    await mgr.keep_connection_alive()

    mgr._handle_reconnection.assert_not_awaited()


# ---- defect A: the manager never stopped, because nothing bounded its life ---
#
# A finished draft's manager is NOT idle: the "release logs early" vote needs
# this socket and this in-memory log to call shareDraftLog, and Draftmancer
# keeps the log locked for DRAFT_LOG_UNLOCK_TIMER_MINUTES (180 in prod). So the
# manager legitimately outlives the draft -- it just must not outlive that job.


def _finished(mgr, *, data_received=False, unlock_at=None, ago_minutes=1):
    """A manager whose draft ended `ago_minutes` ago, with that DB row."""
    mgr.draft_finished = True
    mgr._finished_at = datetime.now() - timedelta(minutes=ago_minutes)
    row = MagicMock()
    row.data_received = data_received
    row.unlock_at = unlock_at
    return patch.object(type(mgr), "_load_log_state", AsyncMock(return_value=row))


@pytest.mark.asyncio
async def test_a_manager_stays_up_through_the_early_release_window():
    """The behaviour the leak was accidentally protecting. Do not regress it:
    without this socket the vote silently fails to unlock Draftmancer."""
    mgr = make_manager()
    with _finished(mgr, unlock_at=datetime.now() + timedelta(hours=2)):
        assert await mgr._should_stop() is False


@pytest.mark.asyncio
async def test_a_manager_stops_once_its_log_has_been_published():
    mgr = make_manager()
    with _finished(mgr, data_received=True):
        assert await mgr._should_stop() is True


@pytest.mark.asyncio
async def test_a_manager_stops_once_the_unlock_timer_has_passed():
    """After unlock_at there is nothing left to release early."""
    mgr = make_manager()
    with _finished(mgr, unlock_at=datetime.now() - timedelta(minutes=1)):
        assert await mgr._should_stop() is True


@pytest.mark.asyncio
async def test_a_manager_stops_even_if_the_row_never_unlocks():
    """The backstop, and the point of the whole change.

    unlock_at is only set when a log is captured; a draft whose capture failed
    would otherwise satisfy no stop condition and run for ever -- which is
    exactly the bug. Termination must not depend on external state.
    """
    mgr = make_manager()
    with _finished(mgr, unlock_at=None, ago_minutes=60 * 24):
        assert await mgr._should_stop() is True


@pytest.mark.asyncio
async def test_a_draft_still_in_progress_is_never_torn_down():
    mgr = make_manager()
    mgr.draft_finished = False
    assert await mgr._should_stop() is False


@pytest.mark.asyncio
async def test_the_loop_actually_stops_a_finished_manager():
    """The wiring: the decision above has to be reachable from the loop."""
    mgr = loopable(make_manager(session_id="done"))
    mgr.socket_client.connected = True     # so the loop reaches the stop check
    mgr.current_draft_log = {"big": "x" * 1000}
    mgr._handle_connected_state = AsyncMock()

    with _finished(mgr, data_received=True):
        await mgr.keep_connection_alive()

    assert "done" not in ACTIVE_MANAGERS, "a stopped manager must leave the registry"
    assert not mgr.current_draft_log, "and must release the draft log it was holding"


# ---- the bound must not depend on an event arriving -------------------------

@pytest.mark.asyncio
async def test_a_manager_that_never_sees_enddraft_is_still_bounded():
    """spawn_for_existing_session (the log capture-retry) joins a session that
    ended BEFORE the manager existed, so Draftmancer never fires endDraft at it
    and draft_finished stays False for ever.

    Both of the tighter conditions key off that event, so without an absolute
    cap these managers -- the ones the reconciler creates repeatedly -- leak
    exactly as before. A bound keyed to an event is not a bound.
    """
    mgr = make_manager()
    mgr.drafting = False
    mgr.draft_finished = False             # endDraft never arrived
    mgr._created_at = datetime.now() - timedelta(
        minutes=dsm.MANAGER_MAX_LIFETIME_MINUTES + 1)

    assert await mgr._should_stop() is True


@pytest.mark.asyncio
async def test_the_cap_never_takes_down_a_live_table():
    """The cap is generous, but `drafting` is checked first so a long or stalled
    draft can never be disconnected out from under the players."""
    mgr = make_manager()
    mgr.drafting = True
    mgr._created_at = datetime.now() - timedelta(days=7)

    assert await mgr._should_stop() is False


@pytest.mark.asyncio
async def test_an_unreadable_row_does_not_disconnect_a_table():
    """A database blip must not look like "you are finished". The absolute cap
    is what guarantees termination, so this branch can safely keep going."""
    mgr = make_manager()
    with _finished(mgr, unlock_at=datetime.now() + timedelta(hours=2)):
        mgr._load_log_state = AsyncMock(return_value=None)
        assert await mgr._should_stop() is False


@pytest.mark.asyncio
async def test_a_log_published_early_stops_the_manager_promptly():
    """Regression: caching unlock_at as a deadline looks free, but then
    data_received is never consulted again and an early publish -- the manual
    release vote, or the reconciler -- no longer stops the manager."""
    mgr = make_manager()
    mgr.draft_finished = True
    mgr._finished_at = datetime.now()
    far_off = datetime.now() + timedelta(hours=2)

    mgr._load_log_state = AsyncMock(return_value=MagicMock(
        data_received=False, unlock_at=far_off))
    assert await mgr._should_stop() is False

    mgr._load_log_state = AsyncMock(return_value=MagicMock(
        data_received=True, unlock_at=far_off))
    assert await mgr._should_stop() is True


@pytest.mark.asyncio
async def test_the_manager_is_up_for_the_whole_unlock_window_not_just_the_start():
    """The window is DRAFT_LOG_UNLOCK_TIMER_MINUTES (180 in prod) wide, and the
    vote can come at any point in it -- checking a minute after the draft ends
    would pass on a manager that quit after five."""
    mgr = make_manager()
    minutes = dsm.DRAFT_LOG_UNLOCK_TIMER_MINUTES
    with _finished(mgr, ago_minutes=minutes - 1,
                   unlock_at=datetime.now() + timedelta(minutes=1)):
        assert await mgr._should_stop() is False


@pytest.mark.asyncio
async def test_a_loop_that_raises_still_releases_the_manager():
    """The loop is the whole of a manager's life; an exception out of it used to
    end the task while the registry entry and the draft log stayed behind."""
    mgr = loopable(make_manager(session_id="boom"))
    mgr.socket_client.connected = True
    mgr.current_draft_log = {"big": "x" * 1000}
    mgr._handle_connected_state = AsyncMock(side_effect=RuntimeError("kaboom"))

    await mgr.keep_connection_alive()

    assert "boom" not in ACTIVE_MANAGERS
    assert not mgr.current_draft_log


@pytest.mark.asyncio
async def test_a_manager_that_never_connects_does_not_stay_registered():
    mgr = loopable(make_manager(session_id="never"))
    mgr.socket_client.connect_with_retry = AsyncMock(return_value=False)

    await mgr.keep_connection_alive()

    assert "never" not in ACTIVE_MANAGERS
