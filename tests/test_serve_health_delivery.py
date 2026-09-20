"""The half of the monitor that was never tested: delivery and the loop.

classify() and Alerter were pure and covered; the path from "we decided to
alert" to "a human read it" had no tests at all, and every serious defect
found in review lived there. A monitor that cannot tell whether its own alert
arrived is a monitor that reports nothing while believing it reported.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import services.serve_health_monitor as mon
from services.serve_health_monitor import Alerter

# Most tests here are async; the pure-Alerter ones are marked
# individually rather than by a module-wide mark that warns on them.


# --- an alert is only "reported" once it has actually been delivered --------

def test_an_undelivered_alert_is_retried_rather_than_assumed_sent():
    """The failure this closes: the alerter marked itself reported as a side
    effect of DECIDING to alert, so a DM that never arrived -- blocked, closed
    DMs, a malformed id -- left it silent forever afterwards while the serve
    stayed down."""
    a = Alerter(bad_before_alert=2)
    a.observe("down", "no answer")
    said = a.observe("down", "no answer")
    assert said is not None, "it decided to alert"
    # ...and delivery failed, so confirm() is NOT called.

    again = a.observe("down", "no answer")

    assert again is not None, "it tries again rather than going quiet"


def test_a_delivered_alert_is_not_repeated():
    a = Alerter(bad_before_alert=2)
    a.observe("down", "no answer")
    said = a.observe("down", "no answer")
    a.confirm("down")                       # delivery succeeded

    assert said is not None
    assert a.observe("down", "no answer") is None, "already told them"


def test_recovery_only_fires_for_an_outage_they_were_actually_told_about():
    """If the outage alert never got through, a green checkmark is the first
    thing they would ever hear -- about a problem they never knew existed."""
    a = Alerter(bad_before_alert=2)
    a.observe("down", "x")
    a.observe("down", "x")                  # decided, never confirmed

    assert a.observe("ok", "Sealed01") is None, "nothing to recover from"


# --- a change of state while an alert is open ------------------------------

@pytest.mark.asyncio
async def test_a_state_change_re_describes_instead_of_waiting_for_the_reminder():
    """Down, then 401 after a restart, means the restart worked and the token
    is wrong. Under the old suppression the next message came up to an hour
    later in the "still not right" wording, so the one sentence that matters --
    restarting will not help -- was never said, and the operator kept
    restarting a container that was already fine."""
    a = Alerter(bad_before_alert=2, remind_after=12)
    a.observe("down", "no answer")
    a.observe("down", "no answer")
    a.confirm("down")

    said = a.observe("unauthorized", "HTTP 401")

    assert said is not None
    assert "restart" in said.lower() and "not help" in said.lower()


# --- a wedged reconnect is an outage; a passing one is not ------------------

@pytest.mark.asyncio
async def test_a_brief_reconnect_says_nothing():
    """MTGO drops sessions routinely and the serve re-establishes them."""
    a = Alerter(bad_before_alert=2)

    assert [a.observe("reconnecting", "x") for _ in range(3)] == [None, None, None]


@pytest.mark.asyncio
async def test_a_serve_stuck_reconnecting_is_eventually_reported():
    """The blind spot this closes. serve_busy_reason refuses every deposit,
    withdrawal and trade while the serve reports `reconnecting`, so a serve
    wedged in that state is a total money outage -- and the monitor used to
    classify it "ok", log nothing and send nothing. That is the original
    incident, reproduced inside the thing built to prevent it."""
    a = Alerter(bad_before_alert=2)

    said = [a.observe("reconnecting", "reconnecting to MTGO")
            for _ in range(mon.RECONNECTING_BEFORE_ALERT)]

    assert said[-1] is not None, "it escalates once the reconnect stops looking brief"
    assert "trade" in said[-1].lower() or "deposit" in said[-1].lower(), \
        "and names the user-visible symptom"


# --- our own misconfiguration is not the serve's fault ---------------------

@pytest.mark.asyncio
async def test_a_bad_url_is_not_reported_as_the_serve_being_down():
    """A typo'd MTGO_TRADEBOT_URL used to produce "has stopped answering ...
    restarting its container has fixed this before" -- about a serve that was
    working perfectly."""
    state, detail = await mon.probe("htp://not a url", "token")

    assert state == "misconfigured", detail
    said = Alerter(bad_before_alert=1).observe(state, detail)
    assert "restarting the serve will not help" in said, said


@pytest.mark.asyncio
async def test_a_200_that_is_not_the_serve_does_not_read_as_a_contradiction():
    """A proxy error page or a captive portal answering 200 used to render as
    "has stopped answering - HTTP 200"."""
    state, detail = mon.classify(200, None, None)

    assert state == "unhealthy"
    assert "200" in detail and "body" in detail.lower()


# --- the loop must not be able to die quietly ------------------------------

@pytest.mark.asyncio
async def test_a_cancelled_watcher_releases_its_guard_so_it_can_restart():
    """on_ready refires on gateway reconnects. If cancellation left the module
    guard set, the restart returned instantly and silently and the serve was
    never watched again for the life of the process -- and a dead loop logs
    exactly what a healthy serve logs, which is nothing."""
    mon._watching.clear()
    monitored = asyncio.Event()

    async def never_answers(_url, _token):
        monitored.set()
        return ("down", "no answer")

    original, mon.probe = mon.probe, never_answers
    try:
        bot = MagicMock()
        task = asyncio.create_task(mon.watch_serve_health(
            bot, url="http://serve", token="t", alert_to="1", canary=False))
        await asyncio.wait_for(monitored.wait(), timeout=2)
        assert mon._watching, "it registered itself while running"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert not mon._watching, "and released the guard on the way out"
    finally:
        mon.probe = original
        mon._watching.clear()


@pytest.mark.asyncio
async def test_two_serves_can_be_watched_at_once():
    """The lending library gets its own custodian. A single module-level flag
    would have accepted the first watcher and silently dropped the second."""
    mon._watching.clear()
    try:
        assert mon._claim("http://a") is True
        assert mon._claim("http://b") is True, "a different serve is admitted"
        assert mon._claim("http://a") is False, "the same one twice is not"
    finally:
        mon._watching.clear()


# --- a serve that fails HALF the time is still a broken serve --------------

def test_a_flapping_serve_is_reported():
    """The hole a consecutive-run counter cannot see. serve_busy_reason
    refuses a deposit whenever the serve reports reconnecting, so one that
    flaps between ok and reconnecting is refusing roughly half of all money
    commands -- a real, sustained outage. Counting only CONSECUTIVE failures
    reset on every healthy poll, so it never reached any threshold and the
    monitor stayed silent through 100 polls of it.
    """
    a = Alerter(bad_before_alert=2)

    said = [a.observe("down" if i % 2 == 0 else "ok", "x") for i in range(12)]

    assert any(s for s in said), "a serve failing every other poll gets reported"


def test_a_flapping_serve_does_not_announce_recovery_between_blips():
    """...and having reported it, it must not then send "it's back" on every
    good poll in between, which would be worse than saying nothing."""
    a = Alerter(bad_before_alert=2)
    for i in range(12):
        said = a.observe("down" if i % 2 == 0 else "ok", "x")
        if said:
            a.confirm("down")

    backs = [a.observe("down" if i % 2 == 0 else "ok", "x") for i in range(12)]

    assert not any(s and "back" in s for s in backs)


def test_one_blip_in_a_quiet_window_still_says_nothing():
    """The other side: a single dropped read is not an outage."""
    a = Alerter(bad_before_alert=2)

    said = [a.observe("ok", "Sealed01") for _ in range(5)]
    said.append(a.observe("down", "one timeout"))

    assert not any(said)


def test_recovery_needs_the_serve_to_actually_stay_up():
    """Announced only once it has been healthy for a couple of polls, so a
    flapping serve does not produce alternating outage/recovery messages."""
    a = Alerter(bad_before_alert=2, settled_polls=2)
    a.observe("down", "x"); a.observe("down", "x"); a.confirm("down")

    assert a.observe("ok", "Sealed01") is None, "one good poll is not recovery"
    back = a.observe("ok", "Sealed01")
    assert back is not None and "back" in back.lower()


def test_an_undelivered_reminder_is_retried_on_the_next_poll():
    """Cadence measured from the last DELIVERED message, not from a poll
    count: a reminder whose DM failed used to be dropped and the next attempt
    was a full interval later, so an ongoing outage went quiet for an hour."""
    a = Alerter(bad_before_alert=1, remind_after=3)
    assert a.observe("down", "x") is not None
    a.confirm("down")
    for _ in range(2):
        assert a.observe("down", "x") is None
    assert a.observe("down", "x") is not None, "the reminder is due"
    # ...delivery fails, so no confirm()
    assert a.observe("down", "x") is not None, "so it tries again immediately"


# --- whose fault is it? ----------------------------------------------------

@pytest.mark.parametrize("exc,expected", [
    ("ClientConnectionResetError", "down"),
    ("ServerDisconnectedError", "down"),
    ("ClientOSError", "down"),
    ("ClientPayloadError", "down"),
    ("ClientConnectorSSLError", "misconfigured"),
    ("ClientConnectorCertificateError", "misconfigured"),
    ("InvalidUrlClientError", "misconfigured"),
])
@pytest.mark.asyncio
async def test_the_blame_lands_on_whoever_is_actually_at_fault(monkeypatch, exc,
                                                               expected):
    """Two different messages hang off this, and each is wrong for the other
    case: "down" tells the operator to restart the container, "misconfigured"
    tells them not to bother because the problem is ours.

    Naming concrete exception classes got this exactly inverted -- a serve
    dying mid-request (a connection reset) missed the tuple and was excused as
    our configuration, while an expired certificate was blamed on the serve
    and sent somebody to restart it. Driven through the real request path, so
    it is aiohttp's own class hierarchy being tested and not a copy of it.
    """
    import aiohttp
    import services.serve_health_monitor as mon

    kind = getattr(aiohttp, exc, None)
    if kind is None:
        pytest.skip(f"{exc} is not in this aiohttp")

    class Boom(kind):
        """Constructs without aiohttp's required args, keeps its ancestry."""
        def __init__(self):
            Exception.__init__(self, "boom")

        def __str__(self):
            return "boom"

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, *a, **k):
            raise Boom()

    monkeypatch.setattr(mon.aiohttp, "ClientSession", lambda **k: FakeSession())

    state, detail = await mon.probe("http://serve", "token")

    assert state == expected, detail


# --- the loop's own wiring, which nothing covered -------------------------

@pytest.mark.asyncio
async def test_the_loop_retries_an_alert_whose_dm_failed(monkeypatch):
    """The single line the whole delivery fix hangs on -- confirm() only when
    send_dm says yes -- had no test. Without it the alerter believes it
    reported, and a serve that is down stays unreported for good."""
    import services.serve_health_monitor as mon

    async def always_down(_url, _token):
        return ("down", "no answer")
    monkeypatch.setattr(mon, "probe", always_down)
    monkeypatch.setattr(mon.asyncio, "sleep", AsyncMock())

    sent: "list[str]" = []

    async def refuses(_bot, _to, message, label=None):
        sent.append(message)
        if len(sent) < 3:
            return False            # the DM cannot be delivered yet
        raise _Stop()               # third attempt lands; end the test

    with pytest.raises(_Stop):
        await mon._poll_forever(MagicMock(), "custodian", "http://s", "t",
                                "1", refuses)

    assert len(sent) == 3, "it kept trying rather than believing it had reported"


@pytest.mark.asyncio
async def test_the_loop_stops_repeating_once_the_dm_lands(monkeypatch):
    import services.serve_health_monitor as mon

    async def always_down(_url, _token):
        return ("down", "no answer")
    monkeypatch.setattr(mon, "probe", always_down)

    polls = {"n": 0}

    async def tick(_s):
        polls["n"] += 1
        if polls["n"] > 6:
            raise _Stop()
    monkeypatch.setattr(mon.asyncio, "sleep", tick)

    sent: "list[str]" = []

    async def delivers(_bot, _to, message, label=None):
        sent.append(message)
        return True

    with pytest.raises(_Stop):
        await mon._poll_forever(MagicMock(), "custodian", "http://s", "t",
                                "1", delivers)

    assert len(sent) == 1, "one alert for one continuous outage"


@pytest.mark.asyncio
async def test_a_probe_that_explodes_does_not_kill_the_loop(monkeypatch):
    """The watcher must outlive anything it touches."""
    import services.serve_health_monitor as mon

    calls = {"n": 0}

    async def erratic(_url, _token):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("something unexpected")
        raise _Stop()
    monkeypatch.setattr(mon, "probe", erratic)
    monkeypatch.setattr(mon.asyncio, "sleep", AsyncMock())

    with pytest.raises(_Stop):
        await mon._poll_forever(MagicMock(), "custodian", "http://s", "t",
                                "1", AsyncMock(return_value=True))

    assert calls["n"] == 2, "it polled again after the exception"


class _Stop(BaseException):
    """Ends a forever-loop from inside a stub, without a timeout.

    BaseException on purpose: the loop catches Exception so that nothing it
    touches can kill it, which is the property under test here -- an ordinary
    exception would be swallowed and the test would hang rather than fail.
    """
