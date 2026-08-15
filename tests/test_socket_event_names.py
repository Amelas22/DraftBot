"""The bot must listen on the names Draftmancer actually emits.

Two subscriptions were never going to fire. Draftmancer emits "pauseDraft" and
"resumeDraft" (src/Session.ts pauseDraft/resumeDraft); the bot listened for
"draftPaused"/"draftResumed", so self.draftPaused could never become True — across
88MB of production logs covering 40 drafts, neither handler ran once. And
"storedSessionSettings" is not an event at all: it is a localStorage key in
Draftmancer's client.

That matters because players pause when someone's connection is struggling, which
production shows is routine — 58 same-session drop-and-return flaps in three days,
median 6 seconds apart. A paused draft is exactly when the bot must not touch the
seating, and it could not tell.

These names are a contract with another codebase, so they are asserted literally.
"""
from unittest.mock import MagicMock

from services.draft_setup_manager import DraftSetupManager

# What Draftmancer emits to connected users; see src/Session.ts.
DRAFTMANCER_EMITS = {
    "sessionUsers", "userDisconnected", "resumeOnReconnection", "pauseDraft",
    "resumeDraft", "endDraft", "draftLog", "setReady",
}
# Events Draftmancer declares as `() => void` in src/SocketType.ts. A handler that
# demands a payload for one of these raises TypeError on arrival, which fails exactly
# as silently as subscribing to a name nothing emits.
NO_PAYLOAD = {"pauseDraft", "resumeDraft", "endDraft"}
# socket.io transport events, not Draftmancer's.
TRANSPORT = {"connect", "connect_error", "disconnect"}


def _subscriptions():
    """(name, handler) for every subscription, so signatures can be checked too."""
    mgr = DraftSetupManager.__new__(DraftSetupManager)
    mgr.socket_client = MagicMock()
    seen = []
    mgr.socket_client.sio.on.side_effect = lambda name, handler: seen.append((name, handler))
    DraftSetupManager._register_socket_handlers(mgr)
    return seen


def _subscribed_events():
    mgr = DraftSetupManager.__new__(DraftSetupManager)
    mgr.socket_client = MagicMock()
    seen = []
    mgr.socket_client.sio.on.side_effect = lambda name, handler: seen.append(name)
    DraftSetupManager._register_socket_handlers(mgr)
    return seen


def test_every_subscription_is_something_draftmancer_actually_emits():
    for name in _subscribed_events():
        assert name in DRAFTMANCER_EMITS or name in TRANSPORT, (
            f"nothing ever emits {name!r}, so its handler can never run"
        )


def test_the_pause_events_use_draftmancers_names():
    events = _subscribed_events()
    assert "pauseDraft" in events and "resumeDraft" in events
    assert "draftPaused" not in events and "draftResumed" not in events


def test_a_mid_draft_disconnect_is_subscribed():
    """userDisconnected is the only signal naming WHO dropped.

    sessionUsers does fire on a mid-draft departure — removeUserFromSession ends
    `} else sess.notifyUserChange();` — but it cannot reveal one: the payload is built
    from getSortedHumanPlayersIDs(), which is `users` UNION `disconnectedUsers`
    (Session.ts:3759). Draftmancer holds the seat, so the departed player is still
    listed and the count is unchanged. Confirmed in production: across 19 complete
    draft windows, not one in-session user-count drop.
    """
    assert "userDisconnected" in _subscribed_events()


def test_the_last_player_returning_is_subscribed():
    """Session.reconnectUser only calls broadcastDisconnectedUsers() while someone is
    STILL missing; when the map empties it calls resumeOnReconnection instead. So an
    empty userDisconnected payload never arrives, and without this subscription the
    bot never learns that everyone is back."""
    assert "resumeOnReconnection" in _subscribed_events()


# ---- the handlers behind those names --------------------------------------------

import pytest

from conftest import make_manager


def _manager():
    # make_manager's mocked emit matters here: a disconnect now pauses the draft as
    # well as recording it — see test_pause_on_disconnect.py for that behaviour.
    return make_manager()


@pytest.mark.asyncio
async def test_pausing_a_draft_is_finally_visible_to_the_bot():
    """With the name corrected these actually arrive, which is what makes the
    seating recovery's paused-draft guard real rather than decorative.

    Called with NO arguments, because that is how they arrive: getting the name right
    and the signature wrong fails just as silently. An earlier version of this test
    passed `{}` and so proved nothing about the real contract.
    """
    mgr = _manager()
    assert mgr.draftPaused is False

    await mgr._on_draft_paused()
    assert mgr.draftPaused is True

    await mgr._on_draft_resumed()
    assert mgr.draftPaused is False


@pytest.mark.asyncio
async def test_the_no_payload_handlers_accept_no_payload():
    """Guards the whole class of arity mismatch rather than the two that had it."""
    import inspect

    mgr = _manager()
    for name in sorted(NO_PAYLOAD):
        handler = dict(_subscriptions())[name]
        sig = inspect.signature(handler)
        try:
            sig.bind()
        except TypeError:
            raise AssertionError(
                f"{name} arrives with no payload, but {handler.__name__} demands one"
            )


@pytest.mark.asyncio
async def test_the_last_player_returning_clears_the_record():
    mgr = _manager()
    mgr.disconnected_users = {"id-gregg": "gregg / keezles"}

    await mgr._on_resume_on_reconnection({"title": "Player reconnected", "text": "..."})

    assert mgr.disconnected_users == {}


@pytest.mark.asyncio
async def test_a_disconnect_records_who_went():
    """sessionUsers says the count changed; only this says whose connection dropped."""
    mgr = _manager()

    await mgr._on_user_disconnected({
        "owner": "someone",
        "disconnectedUsers": {"id-gregg": {"userName": "gregg / keezles"}},
    })
    assert mgr.disconnected_users == {"id-gregg": "gregg / keezles"}

    # everyone back
    await mgr._on_user_disconnected({"owner": "someone", "disconnectedUsers": {}})
    assert mgr.disconnected_users == {}


@pytest.mark.asyncio
async def test_a_malformed_disconnect_payload_is_survivable():
    """It arrives mid-draft; a shape surprise must not take out the handler."""
    mgr = _manager()
    await mgr._on_user_disconnected(None)
    assert mgr.disconnected_users == {}
