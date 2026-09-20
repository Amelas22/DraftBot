"""Noticing that the MTGO custodian has stopped answering, without being asked.

Nothing watched it. /health is only read when a command needs it, so a wedged
serve stayed invisible until somebody tried to trade and failed -- and the gap
between the last successful trade and the first failed one was a day and a
half, which made it impossible to say afterwards when it had actually broken.

Two things this has to get right, both learned from that outage:

  * A 401 is NOT down. The serve answering "unauthorized" is a serve that is
    alive and misconfigured, and reading it as an outage sends somebody to
    restart a container that was working. Reading it as healthy is worse.
  * One missed poll is not an outage. A single timeout happens; alerting on it
    trains the recipient to ignore the alert.
"""
from services.serve_health_monitor import Alerter, classify


# --- reading what the serve said -------------------------------------------

def test_a_healthy_serve_reads_as_healthy():
    state, detail = classify(200, {"ok": True, "custodian": "Sealed01"}, None)

    assert state == "ok"
    assert "Sealed01" in detail


def test_no_answer_at_all_is_the_outage_shape():
    """A timeout or a refused connection. This is what a wedged serve looks
    like: the port is open, nothing accepts."""
    state, detail = classify(None, None, "timed out after 8s")

    assert state == "down"
    assert "timed out" in detail


def test_unauthorized_is_alive_but_misconfigured():
    """The distinction that cost an hour: an unauthenticated probe returns 401,
    which proves the serve is accepting and answering. Calling that an outage
    points the operator at the wrong thing entirely."""
    state, _ = classify(401, None, None)

    assert state == "unauthorized", "a 401 is a reply, not silence"


def test_a_serve_that_says_it_is_not_ok_is_not_ok():
    state, detail = classify(200, {"ok": False, "blocker": "client logged out"}, None)

    assert state == "unhealthy"
    assert "client logged out" in detail


def test_needing_a_human_counts_as_unhealthy_even_when_ok_is_true():
    """`ok` covers connectivity. A serve that is reachable but stuck, or
    flagged needsHuman, is not one anybody should assume is working."""
    state, _ = classify(200, {"ok": True, "stuck": True}, None)
    assert state == "unhealthy"

    state, _ = classify(200, {"ok": True, "needsHuman": True}, None)
    assert state == "unhealthy"


def test_reconnecting_is_its_own_state_rather_than_healthy():
    """This used to read as "ok", which was wrong in a way that mattered:
    serve_busy_reason refuses every deposit, withdrawal and trade while the
    serve reports reconnecting, so a serve wedged there is a total money
    outage that the monitor called healthy and never logged.

    It is still not alerted on immediately -- MTGO drops sessions routinely --
    but that is the Alerter's decision to make, not a fact to lose here."""
    state, _ = classify(200, {"ok": True, "reconnecting": True}, None)

    assert state == "reconnecting"


# --- deciding whether to say anything --------------------------------------

def test_a_single_missed_poll_says_nothing():
    a = Alerter(bad_before_alert=2)

    assert a.observe("down", "timed out") is None


def test_the_second_consecutive_failure_alerts():
    a = Alerter(bad_before_alert=2)
    a.observe("down", "timed out")

    said = a.observe("down", "timed out")

    assert said is not None and "timed out" in said


def test_it_does_not_repeat_itself_every_poll():
    """The outage lasts; the alert should not arrive every five minutes."""
    a = Alerter(bad_before_alert=2, remind_after=100)
    a.observe("down", "x")
    assert a.observe("down", "x") is not None, "the first alert"
    a.confirm("down")                      # and it was delivered

    assert [a.observe("down", "x") for _ in range(5)] == [None] * 5


def test_it_reminds_eventually_so_an_outage_is_not_forgotten():
    a = Alerter(bad_before_alert=2, remind_after=3)
    a.observe("down", "x")
    a.observe("down", "x")
    a.confirm("down")                      # alerted and delivered

    assert [a.observe("down", "x") for _ in range(3)][-1] is not None


def test_recovery_is_announced_once():
    a = Alerter(bad_before_alert=2)
    a.observe("down", "x")
    a.observe("down", "x")
    a.confirm("down")

    a.observe("ok", "Sealed01")             # one good poll is not yet settled
    back = a.observe("ok", "Sealed01")
    assert back is not None and "back" in back.lower()
    a.confirm("ok")
    assert a.observe("ok", "Sealed01") is None, "and not again on every later poll"


def test_recovery_is_silent_if_nothing_was_ever_reported():
    """A serve that was healthy all along must not announce itself."""
    a = Alerter(bad_before_alert=2)

    assert a.observe("ok", "Sealed01") is None
    assert a.observe("down", "x") is None
    assert a.observe("ok", "Sealed01") is None, "the blip never alerted, so nothing recovered"


def test_two_failures_far_apart_are_not_one_outage():
    """The window slides, so a blip today and a blip tomorrow never meet.

    Note what changed here, deliberately: two failures a few polls apart now
    DO report, because a serve failing intermittently is a serve refusing
    money commands intermittently. Only a counter that resets on every good
    poll would call that healthy, and that was the bug."""
    a = Alerter(bad_before_alert=2, window_polls=3)
    a.observe("down", "x")
    for _ in range(3):
        a.observe("ok", "Sealed01")         # the bad poll slides out of view

    assert a.observe("down", "x") is None, "nothing recent to pair it with"


def test_the_message_names_which_serve_and_what_is_wrong():
    a = Alerter(bad_before_alert=1, name="wallet custodian")

    said = a.observe("unauthorized", "HTTP 401")

    assert "wallet custodian" in said
    assert "401" in said
