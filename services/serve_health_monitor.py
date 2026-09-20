"""Watch the MTGO custodian and say something when it stops answering.

Nothing watched it before. `/health` is read only when a command needs it --
`serve_busy_reason` before a trade, `custodian_name` when rendering a prompt --
so a wedged serve stayed invisible until somebody tried to use it and failed.
In the outage this exists for, the serve's last successful trade and the first
failed command were 38 hours apart, with no traffic in between: nobody could
say afterwards when it had actually broken, because nothing had asked.

The loop asks. What makes that worth anything is that it can also tell when it
has failed to ask, or failed to be heard -- a monitor whose own death looks
exactly like "everything is fine" gives false confidence, which is worse than
no monitor at all. So:

  * An alert counts as delivered only when send_dm says it was. Marking it
    sent at the moment of DECIDING to send lost every alert whenever the DM
    could not go out, while leaving the alerter certain it had reported.
  * A canary goes out at startup, so a broken alert channel is found by
    whoever is deploying rather than during the outage it was meant to catch.
  * A healthy serve still logs a heartbeat hourly, so a dead watcher shows up
    as the absence of something rather than only as silence.
  * The guard is released on the way out, so a cancelled watcher can be
    restarted by the next on_ready rather than being off for good.

Four distinctions it draws, each bought by a real failure:

  * A 401 is not an outage. A serve answering "unauthorized" is accepting
    connections and replying -- it is alive with the wrong credentials, and
    treating that as down sends somebody to restart a container that was fine.
  * Our own misconfiguration is not an outage either. A typo'd URL cannot be
    fixed by restarting the serve, so it must not produce a message saying to.
  * A reconnect is transient; a serve STUCK reconnecting is a total outage.
    serve_busy_reason refuses every deposit, withdrawal and trade while the
    serve reports reconnecting, so a wedged one means nobody can move anything
    -- reported as "ok" and logged nowhere, it reproduced the very incident
    this file exists to prevent.
  * One missed poll is not an outage. Timeouts happen, and alerting on a
    single one is how an alert becomes something its recipient filters out.

It alerts from INSIDE the bot rather than from cron, which needs no second
Discord credential on the box and covers the failure that actually happened --
serve wedged, bot healthy. The gap that leaves is deliberate: a bot that is
itself down alerts nobody here, but that case announces itself, because every
command in every server stops answering at once.

One serve per task. The lending library's custodian will want its own call;
the guard is keyed by URL so a second one is admitted rather than dropped.
"""
import asyncio
import time
from collections import deque
from typing import Any, Awaitable, Callable, Literal, Optional

import aiohttp
from loguru import logger

State = Literal["ok", "down", "unauthorized", "unhealthy", "reconnecting",
                "misconfigured"]

# How often to ask. Long enough that the poll is not itself load, short enough
# that an outage is hours old at worst rather than days.
CHECK_INTERVAL_S = 5 * 60

# Bad polls WITHIN THE WINDOW before saying anything, and how wide that window
# is. Counting consecutive failures instead only ever detected CONTINUOUS
# failure: a serve alternating bad and good reset the run on every good poll
# and was never reported, though it was refusing half of all money commands.
BAD_BEFORE_ALERT = 2
WINDOW_POLLS = 12

# Reconnects are ordinary and self-healing, so this one needs more evidence:
# six reconnecting polls in an hour is not a reconnect, it is a serve that
# cannot log back in, with every trade refused meanwhile.
RECONNECTING_BEFORE_ALERT = 6

# Consecutive healthy polls before recovery is announced. More than one, so a
# flapping serve cannot produce alternating outage and recovery messages.
SETTLED_POLLS = 2

# Polls between reminders, counted from the last message actually DELIVERED.
# Deriving it from a failure count meant a reminder whose DM failed was simply
# dropped, and an ongoing outage went quiet for another hour.
REMIND_AFTER = 12

# Healthy polls between heartbeats. Without it, a watcher that has died and a
# serve that is perfectly well produce identical logs: nothing.
HEARTBEAT_EVERY = 12

# Bounded well under CHECK_INTERVAL_S: a hung serve must not hold the loop.
PROBE_TIMEOUT_S = 10

# Who to tell. Absent means the watcher does not run at all -- alerting nobody
# on a schedule is just load. Set in .env, which is the developer's to write.
ALERT_ENV = "SERVE_ALERT_DISCORD_ID"

# Which serves are being watched, keyed by URL. on_ready refires on gateway
# reconnects, so a second watcher for the SAME serve must be refused -- but a
# different serve has to be admitted, which a single flag could not express.
_watching: "set[str]" = set()


def _claim(url: str) -> bool:
    """Register a watcher for this serve. False if one is already running."""
    if url in _watching:
        return False
    _watching.add(url)
    return True


def classify(status: Optional[int], body: Optional[dict[str, Any]],
             error: Optional[str]) -> "tuple[State, str]":
    """Turn one probe into (state, detail)."""
    if error is not None:
        return ("down", error)
    if status in (401, 403):
        return ("unauthorized", f"HTTP {status} — the serve is answering, but "
                                f"rejected our token")
    if status == 200 and not isinstance(body, dict):
        # Something is listening and it is not the serve: a proxy error page, a
        # captive portal, a tailnet login. Reporting that as "stopped answering
        # -- HTTP 200" read as a contradiction to whoever received it.
        return ("unhealthy", "HTTP 200 but the body was not JSON — something "
                             "other than the serve is answering there")
    if status != 200 or not isinstance(body, dict):
        return ("down", f"HTTP {status}")

    if not body.get("ok"):
        why = body.get("blocker") or body.get("blockerDetail") or "it reports ok=false"
        return ("unhealthy", str(why))
    # `ok` answers "can I reach MTGO", which a stuck or human-blocked serve can
    # still say yes to while being unable to do any actual work.
    for flag, said in (("stuck", "it reports itself stuck"),
                       ("needsHuman", "it is asking for a human")):
        if body.get(flag):
            detail = body.get("blocker") or body.get("advice") or said
            return ("unhealthy", str(detail))
    if body.get("reconnecting"):
        # Not an outage yet -- Alerter waits RECONNECTING_POLLS before saying
        # so, because most reconnects resolve themselves within one.
        return ("reconnecting", "it is reconnecting to the MTGO client")
    return ("ok", str(body.get("custodian") or "reachable"))


class Alerter:
    """Decides whether a probe is worth a message, and what it should say.

    Kept apart from the polling so the awkward part -- when to speak, when to
    shut up, when to say it is over -- is testable without a serve or a bot.

    Counts bad polls in a sliding WINDOW rather than in a row. A consecutive
    run only ever detects continuous failure, and the failure that hides best
    is the intermittent one: a serve answering every other poll is refusing
    half of all deposits and withdrawals, and reset the run every time it
    answered.

    Deciding and recording are SEPARATE: observe() says what to send and
    confirm() records that it went. An alerter that recorded on decision went
    permanently quiet whenever a DM could not be delivered, which is the one
    circumstance in which staying quiet is worst. Reminder cadence is measured
    from the last DELIVERED message for the same reason.
    """

    def __init__(self, name: str = "MTGO custodian",
                 bad_before_alert: int = BAD_BEFORE_ALERT,
                 window_polls: int = WINDOW_POLLS,
                 remind_after: int = REMIND_AFTER,
                 settled_polls: int = SETTLED_POLLS,
                 now: Callable[[], float] = time.monotonic):
        self.name = name
        self.bad_before_alert = bad_before_alert
        self.remind_after = remind_after
        self.settled_polls = settled_polls
        self._now = now
        self._recent: "deque[Optional[str]]" = deque(maxlen=window_polls)
        self._polls = 0
        self._delivered_at: Optional[int] = None
        self._first_bad_at: Optional[float] = None
        # What we have SUCCESSFULLY told them about, or None for nothing
        # outstanding. Not a bool: a state that changes while an alert is open
        # -- down becoming 401 after a restart -- has to be re-described, and
        # that is exactly when the difference between them matters most.
        self._reported: Optional[str] = None
        self._ever_healthy = False

    def _threshold(self, state: str) -> int:
        return (max(self.bad_before_alert, RECONNECTING_BEFORE_ALERT)
                if state == "reconnecting" else self.bad_before_alert)

    def _bad_in_window(self) -> int:
        return sum(1 for s in self._recent if s is not None)

    def _settled(self) -> bool:
        """Healthy for long enough to call it over."""
        recent = list(self._recent)[-self.settled_polls:]
        return (len(recent) >= self.settled_polls
                and all(s is None for s in recent))

    def observe(self, state: str, detail: str) -> Optional[str]:
        """What to send now, or None. Does NOT record that it was sent."""
        self._polls += 1
        self._recent.append(None if state == "ok" else state)
        if state == "ok":
            self._ever_healthy = True
        elif self._first_bad_at is None:
            self._first_bad_at = self._now()

        if self._reported is None:
            if state != "ok" and self._bad_in_window() >= self._threshold(state):
                return self._describe(state, detail)
            return None

        if self._settled():
            self._first_bad_at = None
            return f"✅ The **{self.name}** is back — answering again as {detail}."
        if state != "ok" and state != self._reported:
            # It changed under us. Say so now rather than at the next reminder:
            # "down, then 401" means a restart already happened and did not
            # help, and the reminder wording would never tell them that.
            return "↪️ Correction — " + self._describe(state, detail)
        if (self._delivered_at is not None
                and self._polls - self._delivered_at >= self.remind_after):
            return (f"⏰ The **{self.name}** is still not right after "
                    f"~{self._elapsed_min()} minutes — {detail}")
        return None

    def confirm(self, state: str) -> None:
        """Record that the message about `state` reached somebody."""
        self._delivered_at = self._polls
        if state == "ok":
            self._reported = None
            # Start the window clean, so the outage just announced as over
            # cannot immediately re-trigger on polls it already covered.
            self._recent.clear()
        else:
            self._reported = state

    def _elapsed_min(self) -> int:
        """Measured, not inferred. A poll count times a nominal interval drifts
        low over a long outage, and this module exists because nobody could
        reconstruct a timeline afterwards."""
        if self._first_bad_at is None:
            return 0
        return int((self._now() - self._first_bad_at) // 60)

    def _describe(self, state: str, detail: str) -> str:
        match state:
            case "down" if not self._ever_healthy:
                # It has never answered in this process. A co-deploy restarts
                # bot and serve together and the serve is slower, so asserting
                # it "stopped" would be a guess -- and the restart advice below
                # would restart something that is already starting.
                return (f"🟠 The **{self.name}** has not answered since the bot "
                        f"started — {detail}.\nIf you have just deployed it may "
                        f"still be coming up; if not, it did not come back.")
            case "down":
                return (f"🔴 The **{self.name}** has stopped answering — {detail}.\n"
                        f"Nothing can deposit, withdraw or trade until it is back. "
                        f"Restarting its container has fixed this before.")
            case "unauthorized":
                return (f"🔑 The **{self.name}** is up but rejecting our token — "
                        f"{detail}.\nThis is a credentials problem, not an outage: "
                        f"restarting it will not help.")
            case "misconfigured":
                return (f"🧩 I cannot even ask the **{self.name}** — {detail}.\n"
                        f"That is this bot's own configuration, not the serve, so "
                        f"restarting the serve will not help.")
            case "reconnecting":
                return (f"⚠️ The **{self.name}** has been reconnecting to MTGO for "
                        f"~{self._elapsed_min()} minutes — {detail}.\nDeposits, "
                        f"withdrawals and trades are all being refused with \"try "
                        f"again in a few minutes\", which is not going to become "
                        f"true on its own. The client has probably been logged out.")
            case _:
                return (f"⚠️ The **{self.name}** is reachable but not working — "
                        f"{detail}.")


async def probe(url: str, token: str) -> "tuple[str, str]":
    """Ask one serve how it is. Never raises."""
    status, body, error, ours = await _ask(url, token)
    # classify() runs OUTSIDE the request, so a bug in here cannot be reported
    # as the network having failed -- which is what happened when it ran inside
    # the same try that was catching connection errors.
    if ours:
        return ("misconfigured", error or "")
    return classify(status, body, error)


async def _ask(url: str, token: str
               ) -> "tuple[Optional[int], Optional[dict[str, Any]], Optional[str], bool]":
    """(status, body, error, ours) from one GET.

    `ours` says the request could not be made at all for a reason on this
    side. That is not an outage and must not be described as one: restarting
    the serve fixes nothing when the URL is wrong.
    """
    timeout = aiohttp.ClientTimeout(total=PROBE_TIMEOUT_S)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{url.rstrip('/')}/health",
                                   headers={"Authorization": f"Bearer {token}"}) as resp:
                body = None
                if resp.status == 200:
                    try:
                        body = await resp.json(content_type=None)
                    except Exception:
                        try:
                            text = (await resp.text())[:200]
                        except Exception:
                            # Reading the body can fail for the same reason
                            # parsing it did -- a truncated response -- and
                            # that must not escape as a different fault.
                            text = "<unreadable>"
                        logger.warning("serve health: /health returned 200 with an "
                                       "unreadable body: {!r}", text)
                return (resp.status, body, None, False)
    except asyncio.TimeoutError:
        return (None, None, f"no answer within {PROBE_TIMEOUT_S}s", False)
    except aiohttp.ClientSSLError as e:
        # Ahead of ClientConnectionError, which it subclasses. A bad or expired
        # certificate is not the serve falling over, and restarting the
        # container -- which the "down" wording advises -- fixes nothing.
        return (None, None, f"TLS problem reaching it — {e}", True)
    except aiohttp.ClientConnectionError as e:
        # The common base: connector, OS-level, reset and disconnected alike.
        # Naming them individually missed ClientConnectionResetError, so a
        # serve dying mid-request was reported as our own misconfiguration.
        return (None, None, f"could not reach it — {e}", False)
    except aiohttp.ClientPayloadError as e:
        # A truncated body is the serve dying mid-response, not a config fault.
        return (None, None, f"the answer was cut short — {e}", False)
    except Exception as e:
        # We could not even form the request: a bad URL, a bug in here. None of
        # those are fixed by restarting the serve, so none may read as an outage.
        logger.opt(exception=True).error(
            "serve health: could not make the probe at all")
        return (None, None, f"{type(e).__name__}: {e}", True)


async def watch_serve_health(bot: Any, *, name: str = "MTGO custodian",
                             url: Optional[str] = None,
                             token: Optional[str] = None,
                             alert_to: Optional[str] = None,
                             canary: bool = True) -> None:
    """Poll one serve forever and DM the maintainer when it is not right.

    Configuration comes from the trade client by default, so the two cannot
    drift: a renamed env var or an added fallback fixes the trade path and
    would otherwise leave the watcher probing an address nobody uses.
    """
    try:
        import os
        from notification_service import send_dm

        if url is None or token is None:
            from services.mtgo_tradebot_client import get_client
            client = get_client()
            url, token = url or client.url, token or client.token
        alert_to = (alert_to if alert_to is not None
                    else os.getenv(ALERT_ENV, "").strip())

        missing = [n for n, v in ((ALERT_ENV, alert_to),
                                  ("MTGO_TRADEBOT_URL", url),
                                  ("MTGO_TRADEBOT_TOKEN", token)) if not v]
        if missing:
            # WARNING, not INFO: "the thing that watches the money service is
            # not running" is not a routine fact, and it names which value is
            # missing rather than leaving the reader to guess among three.
            logger.warning("serve health: NOT WATCHING — {} not set. The {} has "
                           "no monitor.", ", ".join(missing), name)
            return
        try:
            int(alert_to)
        except (TypeError, ValueError):
            # int(), not isdigit(): they disagree on non-ASCII digits, and the
            # one that matters is the one send_dm will actually perform.
            logger.error("serve health: NOT WATCHING — {}={!r} is not a Discord "
                         "user id (Developer Mode, right-click the user, Copy "
                         "User ID). The {} has no monitor.", ALERT_ENV, alert_to,
                         name)
            return
        if not _claim(url):
            logger.warning("serve health: already watching {} — not starting a "
                           "second watcher for it", name)
            return
    except Exception:
        # Setup lives inside the guard too. An import or a client construction
        # failing out here used to kill the task before the handlers below
        # could say so, leaving asyncio to swallow it at collection time.
        logger.opt(exception=True).critical(
            "serve health: could not start the watcher for {} — it has no "
            "monitor", name)
        return

    try:
        if canary:
            # Prove the channel now, while a human is deploying and watching,
            # rather than discovering it is broken during the outage it exists
            # to report. Its own try: a diagnostic must never be a
            # precondition for the loop it precedes.
            try:
                if await send_dm(bot, alert_to,
                                 f"👀 Now watching the **{name}** every "
                                 f"{CHECK_INTERVAL_S // 60} min. You'll hear from "
                                 f"me if it stops answering.",
                                 label="serve health"):
                    logger.info("serve health: watching {} every {}s", name,
                                CHECK_INTERVAL_S)
                else:
                    logger.error("serve health: cannot DM {} — alerts WILL be "
                                 "lost. Check the id, and that the bot shares a "
                                 "server with them and may DM them.", alert_to)
            except Exception:
                logger.opt(exception=True).error(
                    "serve health: the startup check-in to {} failed; watching "
                    "anyway", alert_to)
        await _poll_forever(bot, name, url, token, alert_to, send_dm)
    except asyncio.CancelledError:
        logger.warning("serve health: watcher for {} cancelled — it is no longer "
                       "being monitored", name)
        raise
    except BaseException:
        logger.opt(exception=True).critical(
            "serve health: watcher for {} DIED — it is no longer being monitored",
            name)
        raise
    finally:
        # Released so the next on_ready can start it again. Left set, a single
        # cancellation disarmed the monitor for the life of the process.
        _watching.discard(url)


async def _poll_forever(bot: Any, name: str, url: str, token: str, alert_to: str,
                        send_dm: Callable[..., Awaitable[bool]]) -> None:
    alerter = Alerter(name=name)
    polls = 0
    while True:
        try:
            state, detail = await probe(url, token)
            polls += 1
            if state != "ok":
                logger.warning("serve health: {} — {}", state, detail)
            if polls % HEARTBEAT_EVERY == 0:
                # Every N polls whatever the answer, so that a dead watcher is
                # an absence somebody can notice. Counting only HEALTHY polls
                # meant the flaky serve -- the one most worth watching -- never
                # produced a heartbeat at all, which is when the absence of one
                # stops meaning anything.
                logger.info("serve health: {} is {} ({}) — {} polls so far",
                            name, state, detail, polls)

            said = alerter.observe(state, detail)
            if said:
                logger.info("serve health: alerting — {}", said.replace("\n", " "))
                if await send_dm(bot, alert_to, said, label="serve health"):
                    alerter.confirm(state)
                else:
                    # Deliberately NOT confirmed, so the next poll tries again.
                    # Retrying an undeliverable address is noisy; staying silent
                    # about an outage nobody has been told of is worse.
                    logger.error("serve health: ALERT UNDELIVERED to {} — the {} is "
                                 "{} ({}) and nobody has been told", alert_to, name,
                                 state, detail)
        except Exception:
            # The watcher must outlive anything it touches, including the DM.
            logger.opt(exception=True).error("serve health: poll failed")
        await asyncio.sleep(CHECK_INTERVAL_S)
