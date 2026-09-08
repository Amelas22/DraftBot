"""Owner-only events must be confirmed, and losing ownership must stand the bot down.

Draftmancer registers ~40 events with prepareSocketCallback(fn, true), and
rejects a non-owner with {'code': 401, error: {'title': 'Unautorized', 'text':
'Must be session owner.'}} -- delivered ONLY through the acknowledgement
callback. /pause emitted without one, discarded the rejection, set draftPaused
itself and told the room "Draft paused" while the draft played on. Confirmed
against a real Draftmancer server.

The rule this pins down: if the bot is not the session owner, it is not running
this draft. It says so and disconnects, the way /mutiny does, rather than
carrying on issuing commands nobody applies.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import make_manager

# Draftmancer's own spelling; see prepareSocketCallback in src/server.ts.
REFUSED = {"code": 401, "error": {"title": "Unautorized",
                                  "text": "Must be session owner."}}


def _mgr():
    mgr = make_manager()
    mgr._notify_bot_no_longer_managing = AsyncMock()
    mgr._cleanup_and_disconnect = AsyncMock()
    return mgr


def _acks(response):
    """A socket whose emit invokes its ack callback with `response`."""
    async def emit(event, *args, callback=None, **kw):
        if callback and response is not None:
            callback(response)
        return True
    return emit


def test_a_401_is_recognised_however_it_is_spelled():
    from services.draft_setup_manager import is_ownership_error

    assert is_ownership_error(REFUSED)
    assert is_ownership_error({"error": {"title": "Unauthorized", "text": "x"}})
    assert is_ownership_error({"error": {"text": "Must be session owner."}})
    assert is_ownership_error({"code": 401, "error": {}})


def test_an_unrelated_error_is_not_an_ownership_error():
    from services.draft_setup_manager import is_ownership_error

    assert not is_ownership_error({"error": {"title": "Internal error"}})
    assert not is_ownership_error({"code": 0})
    assert not is_ownership_error(None)


@pytest.mark.asyncio
async def test_an_accepted_emit_reports_success():
    """Draftmancer acks only on error, so silence is acceptance."""
    mgr = _mgr()
    mgr.socket_client.emit = AsyncMock(side_effect=_acks(None))

    assert await mgr.emit_as_owner("pauseDraft", timeout=0.2) is True
    mgr._cleanup_and_disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_refused_emit_reports_failure():
    mgr = _mgr()
    mgr.socket_client.emit = AsyncMock(side_effect=_acks(REFUSED))

    assert await mgr.emit_as_owner("pauseDraft", timeout=1) is False


@pytest.mark.asyncio
async def test_losing_ownership_stands_the_bot_down():
    """The rule: not the owner means not managing this draft. Same ending as
    /mutiny -- tell the room, then disconnect for good."""
    mgr = _mgr()
    mgr.socket_client.emit = AsyncMock(side_effect=_acks(REFUSED))

    await mgr.emit_as_owner("pauseDraft", timeout=1)

    mgr._notify_bot_no_longer_managing.assert_awaited()
    mgr._cleanup_and_disconnect.assert_awaited()
    assert mgr._should_disconnect is True, (
        "the connection loop has to stop too, or it reconnects a draft the bot "
        "is not running")


@pytest.mark.asyncio
async def test_a_disconnected_socket_is_not_reported_as_success():
    """emit() returns False when the socket is down; that was ignored too."""
    mgr = _mgr()
    mgr.socket_client.emit = AsyncMock(return_value=False)

    assert await mgr.emit_as_owner("pauseDraft", timeout=0.2) is False
    mgr._cleanup_and_disconnect.assert_not_awaited()   # not an ownership loss


@pytest.mark.asyncio
async def test_the_bot_does_not_claim_a_pause_it_could_not_make():
    """draftPaused is read by the seating recovery and by /unpause, so setting it
    without a real pause has the rest of the bot believe a draft is paused while
    Draftmancer plays on."""
    mgr = _mgr()
    mgr.socket_client.emit = AsyncMock(side_effect=_acks(REFUSED))
    mgr.draftPaused = False

    await mgr.emit_as_owner("pauseDraft", timeout=1)

    assert mgr.draftPaused is False


@pytest.mark.asyncio
async def test_acceptance_is_seen_at_once_rather_than_waited_out():
    """Draftmancer acks only on ERROR, so an ack-only design waits the full
    timeout on every successful pause. The broadcast is what says yes."""
    import time
    mgr = _mgr()
    mgr.socket_client.emit = AsyncMock(side_effect=_acks(None))
    mgr.draftPaused = True                      # as _on_draft_paused would set it

    started = time.monotonic()
    assert await mgr.emit_as_owner("pauseDraft", confirmed=lambda: mgr.draftPaused,
                                   timeout=5) is True
    assert time.monotonic() - started < 0.5, "waited for a timeout instead of the broadcast"


@pytest.mark.asyncio
async def test_an_ignored_event_is_reported_as_failure():
    """pauseDraft on a session that is not drafting returns early in Draftmancer:
    no ack, no broadcast. Nothing happened, and nobody should be told it did."""
    mgr = _mgr()
    mgr.socket_client.emit = AsyncMock(side_effect=_acks(None))

    assert await mgr.emit_as_owner("pauseDraft", confirmed=lambda: False,
                                   timeout=0.3) is False
    mgr._cleanup_and_disconnect.assert_not_awaited()   # ignored != ownership loss


# ---- a bot that cannot own the session must not sit in it ----------------------

@pytest.mark.asyncio
async def test_a_bot_that_cannot_own_the_session_does_not_take_a_seat():
    """Measured against a real Draftmancer: a bot that joins a session it does not
    own has setSessionOwner refused, so _reclaim_ownership_as_spectator bails
    before setOwnerIsPlayer(False) -- and an ordinary connected user in
    Draftmancer IS a seat at the table. It took one of eight.

    _handle_reconnection already checks this result. The initial connect threw it
    away, so the bot sat down and stayed.
    """
    mgr = _mgr()
    mgr.socket_client.connected = False
    mgr.socket_client.disconnect = AsyncMock()
    mgr.socket_client.connect_with_retry = AsyncMock(return_value=True)
    mgr._handle_reconnection = AsyncMock(return_value=False)
    mgr._reclaim_ownership_as_spectator = AsyncMock(return_value=False)

    await mgr.keep_connection_alive()

    mgr._cleanup_and_disconnect.assert_awaited()
    assert mgr._should_disconnect is True, (
        "a bot that cannot own the session has no business holding a seat in it")


@pytest.mark.asyncio
async def test_a_bot_that_owns_the_session_carries_on():
    """The normal path must not be disturbed by the guard above."""
    mgr = _mgr()
    mgr.socket_client.connected = False
    mgr.socket_client.disconnect = AsyncMock()
    mgr.socket_client.connect_with_retry = AsyncMock(return_value=True)
    mgr._handle_reconnection = AsyncMock(return_value=False)
    mgr._reclaim_ownership_as_spectator = AsyncMock(return_value=True)

    await mgr.keep_connection_alive()

    mgr._notify_bot_no_longer_managing.assert_not_awaited()
