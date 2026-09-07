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
import os
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from services.draft_setup_manager import DraftSetupManager

# What Draftmancer emits to connected users; see src/Session.ts, plus
# "alreadyConnected" which comes from the connection handler in src/server.ts when
# our userID was already in use and the server assigned us a new one.
DRAFTMANCER_EMITS = {
    "sessionUsers", "userDisconnected", "resumeOnReconnection", "pauseDraft",
    "resumeDraft", "endDraft", "draftLog", "setReady", "alreadyConnected",
}

# Events the CURRENT Draftmancer source no longer declares, which the DEPLOYED
# draftmancer.com still emits. Subscribing to one is right until the site ships
# the removal, and wrong after -- so each is listed with the evidence that it is
# still needed, and the cross-check below reports it rather than failing.
LEGACY_UNTIL_DEPLOYED = {
    "resumeOnReconnection":
        "removed upstream in Draftmancer 51d4ec1e (2026-08-31), but the deployed "
        "draftmancer.com bundle still contains the name. Verified by driving both "
        "builds locally: against the old server the bot only learns a player "
        "returned from this event, and dropping it left a draft paused for ever.",
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


def test_the_last_player_returning_is_heard_on_both_draftmancer_versions():
    """Two servers are in play, and the bot has to work on either.

    The deployed draftmancer.com emits resumeOnReconnection when the last player
    returns. Current Draftmancer removed that and broadcasts an empty
    userDisconnected instead. Exactly one arrives per version -- the old server
    never sends an empty payload, the new one has no other event -- so keeping
    both subscriptions cannot double-resume.
    """
    events = _subscribed_events()
    assert "userDisconnected" in events, "the current server's signal"
    assert "resumeOnReconnection" in events, "the deployed server's signal"


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
async def test_the_last_player_returning_resumes_the_draft():
    """The empty payload is now the ONLY signal that everyone is back, so it has
    to do what the removed resumeOnReconnection handler used to do."""
    mgr = _manager()
    mgr.disconnected_users = {"id-gregg": "gregg / keezles"}
    resumed = []
    mgr._resume_after_disconnect = lambda: resumed.append(True) or _noop()

    await mgr._on_user_disconnected({"owner": "x", "disconnectedUsers": {}})

    assert mgr.disconnected_users == {}
    assert resumed, "an empty payload must resume a draft that was paused for a drop"


async def _noop():
    return None


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


# ---- the same contract, checked against Draftmancer itself ----------------------
#
# Everything above asserts the bot against DRAFTMANCER_EMITS -- a list maintained
# by hand, which is only ever as current as the last person to read Draftmancer's
# source. It cannot notice the other side REMOVING an event, which is exactly what
# happened: Draftmancer 51d4ec1e deleted resumeOnReconnection, and the suite went
# on passing while defending a handler that could never run again.
#
# So when a Draftmancer checkout is available, read the declarations instead of
# trusting the copy. Skipped when it is not, since it is not part of this repo:
# a developer without it loses this check, not the suite.

DRAFTMANCER_SRC = Path(os.environ.get(
    "DRAFTMANCER_SRC",
    Path(__file__).resolve().parents[2] / "Draftmancer" / "src" / "SocketType.ts"))

EVENT_DECL = re.compile(r"^\t(\w+)\s*:", re.M)


def _declared_by_draftmancer():
    """Event names in Draftmancer's ServerToClientEvents interface.

    One tab of indentation is the interface's own members; a continuation line
    inside a multi-line signature is indented further, so it is not mistaken for
    an event of its own.
    """
    src = DRAFTMANCER_SRC.read_text()
    body = src.split("export interface ServerToClientEvents {", 1)[1].split("\n}", 1)[0]
    return set(EVENT_DECL.findall(body))


needs_draftmancer = pytest.mark.skipif(
    not DRAFTMANCER_SRC.exists(),
    reason=f"no Draftmancer checkout at {DRAFTMANCER_SRC}; set DRAFTMANCER_SRC")


@needs_draftmancer
def test_draftmancer_still_declares_everything_the_bot_listens_for():
    """The check the hand-maintained list cannot do: catch a REMOVAL upstream.

    A subscription to an event Draftmancer no longer declares fails in the
    quietest way there is -- the handler simply never runs again.
    """
    declared = _declared_by_draftmancer()
    for name in _subscribed_events():
        if name in TRANSPORT or name in LEGACY_UNTIL_DEPLOYED:
            continue
        assert name in declared, (
            f"{name!r} is not in Draftmancer's ServerToClientEvents any more, so "
            f"its handler can never run. Check what replaced it before deleting, "
            f"and whether the DEPLOYED draftmancer.com still emits it -- if it "
            f"does, the subscription stays and belongs in LEGACY_UNTIL_DEPLOYED."
        )


@needs_draftmancer
def test_the_hand_written_list_still_matches_draftmancer():
    """Keeps DRAFTMANCER_EMITS honest, so the tests above stay meaningful for
    anyone without the checkout."""
    declared = _declared_by_draftmancer()
    stale = DRAFTMANCER_EMITS - declared - set(LEGACY_UNTIL_DEPLOYED)
    assert not stale, f"DRAFTMANCER_EMITS lists events Draftmancer no longer has: {stale}"


@needs_draftmancer
def test_every_legacy_subscription_is_genuinely_legacy():
    """Keeps the exemption list from outliving its reason.

    A name here that upstream still declares is not legacy at all -- it is an
    exemption hiding a subscription the strict check should be covering.
    """
    declared = _declared_by_draftmancer()
    wrong = set(LEGACY_UNTIL_DEPLOYED) & declared
    assert not wrong, (
        f"{wrong} are still declared upstream, so they need no exemption")
