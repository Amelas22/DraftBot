"""The vault as a check on what a tix trade actually did.

The serve reports a job done or failed, and the wallet books on that word: a
failed withdraw is refunded, a done deposit is credited. On 2026-10-03 the
serve's trade events had silently stopped, a 160-tix withdraw left the vault,
the serve reported it failed, and the refund paid the player twice.

The vault's tix count is the fact both reports are about. So every tix trade
reads it just before going out, reads it again once the trade is over, and the
difference -- not the report -- decides the booking (see
mtgo_resolution_service._vault_verdict). Pure DraftBot: the serve's GET /vault
already returns the count and when it was read.

A reading only counts inside its WINDOW, proved by its own `at`:

* not before the trade it measures had settled into the collection. /vault
  serves a cached snapshot until a completed trade moves the serve's counter or
  60s pass -- and a trade the serve missed (the events case above) moves no
  counter, so the cache can still show the pre-trade count;
* not after the serve's NEXT job started. A reading that spans another trade
  measures both, and two trades can cancel out: a 100-tix withdraw and a
  100-tix deposit net to zero, which would read as "nothing left" and refund.

A reading that can't be had inside its window is no reading at all.

And a reading's `at` is when its scan FINISHED (TradeService stamps it after the
scan), so a slow scan stamped after the window opened can still have counted the
tix before it. The serve scans one at a time, under a lock, and only once the
last snapshot has expired -- so a scan that finished after an in-window reading
was finished must also have STARTED after it. That later scan is the one used.

That takes two scans, each behind the cache's minute. A serve with GET
/inventory?refresh=1 proves a start in one: its reply is `consistent` only when
the scan began after the request arrived, nothing was trading and no trade has
completed since. What is left to prove is that the request ARRIVED inside the
window, by the serve's clock -- every window is measured in it -- so the serve's
clock is read off its own replies (`at` + `ageSec`), never assumed to match ours.
A serve without the route, or whose replies can't be used, gets the two scans.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from loguru import logger

# How long after a trade ends before the collection reliably shows it. Measured on
# mtgo3: 4-8s from close to the change appearing in a scan; doubled for margin.
SETTLE_LAG = timedelta(seconds=15)
# /vault caches a snapshot for this long; a newer one can't be had sooner.
_CACHE_TTL = timedelta(seconds=60)
# Two scans after the window opens: one whose finish is in the window, then the next.
_WAIT_FOR_FRESH_S = 180.0
_MIN_POLL_S = 2.0

Moved = Literal["moved", "none", "other"]


@dataclass(frozen=True)
class VaultReading:
    tix: int
    at: datetime        # when the serve's scan of the collection FINISHED, UTC
    # The earliest the scan can have STARTED: the end of the scan before it (scans run one
    # at a time). None when unknown -- a single reading, whose scan could have begun any
    # time before ``at``.
    scan_from: Optional[datetime] = None


def parse_utc(raw: Any) -> Optional[datetime]:
    """An ISO timestamp from the serve, as aware UTC -- or None. A value without a
    timezone can't be placed against ours, so it doesn't count."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return ts.astimezone(timezone.utc) if ts.tzinfo else None


async def _read(client: Any) -> Optional[VaultReading]:
    v = await client.vault()
    if not isinstance(v, dict) or not v.get("available") or not isinstance(v.get("tix"), int):
        return None
    at = parse_utc(v.get("at"))
    return VaultReading(v["tix"], at) if at else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


_SANE_CLOCK = timedelta(days=1)


@dataclass(frozen=True)
class _Inventory:
    tix: int
    at: datetime
    # The serve's clock when it replied, at least -- or None when the reply can't say.
    # ageSec is truncated, so at + ageSec never overstates the clock; but the serve
    # CLAMPS a negative age to 0, and after its clock steps backwards a snapshot's `at`
    # is in its future. Only an age of 1s or more was certainly not clamped.
    serve_now: Optional[datetime]
    consistent: bool


def _parse_inventory(body: Any) -> Optional[_Inventory]:
    """An /inventory reply as far as a tix reading needs it, or None if it isn't one
    that can be used. A reply with unnamed printings is never consistent, whatever its
    scan, so it is no use here either."""
    if not isinstance(body, dict) or body.get("available") is not True:
        return None
    tix, age, unnamed = body.get("tix"), body.get("ageSec"), body.get("unnamed")
    at = parse_utc(body.get("at"))
    # type(...) is int, not isinstance: a bool is an int, and True tix is no count.
    if type(tix) is not int or type(age) is not int or age < 0 or unnamed != 0 or at is None:
        return None
    try:
        serve_now = at + timedelta(seconds=age) if age >= 1 else None
    except OverflowError:
        return None
    # A serve clock a day from ours is none to time a trade by (and near the end of
    # datetime's range, arithmetic on it would raise): unusable, so the two scans run.
    # `at` alone may be old -- a snapshot kept through failed scans -- and is only compared.
    if serve_now is not None and abs(serve_now - _now()) > _SANE_CLOCK:
        return None
    return _Inventory(tix, at, serve_now, body.get("consistent") is True)


async def _sleep_capped(seconds: float, deadline: float) -> None:
    loop = asyncio.get_running_loop()
    await asyncio.sleep(max(0.0, min(seconds, deadline - loop.time())))


async def _read_inventory(client: Any, refresh: bool = False) -> "_Inventory | None | Literal[False]":
    """One /inventory read. None if the request failed, which says nothing about the
    route and is asked again; False if this serve has no /inventory, or answered with
    nothing a reading can be placed by."""
    body = await client.read_inventory(refresh=refresh)
    if isinstance(body, dict) and body.get("_missing"):
        return False
    reply = _parse_inventory(body)
    if body is not None and reply is None:
        return False
    return reply


async def _clock_floor(client: Any, deadline: float) -> "timedelta | None | Literal[False]":
    """A lower bound on (serve clock - ours), from plain reads: a reply's serve time taken
    against our clock AFTER the reply was in. None if none can be had by ``deadline``;
    False if the serve can't give a usable /inventory at all. A failed request, or a
    snapshot too new to date the clock (age 0), is read again."""
    loop = asyncio.get_running_loop()
    first = True
    while True:
        # The first read goes whatever the time: it is what says whether this serve can
        # refresh at all, and so whether the two-scan read is owed its own budget.
        if not first and loop.time() >= deadline:
            return None
        first = False
        reply = await _read_inventory(client)
        if reply is False:
            return False
        if reply is not None and reply.serve_now is not None:
            return reply.serve_now - _now()
        # A second makes a just-taken snapshot old enough to date the clock.
        await _sleep_capped(1.0 if reply is not None else _MIN_POLL_S, deadline)


async def _read_refreshed(client: Any, not_before: datetime,
                          deadline: float) -> "VaultReading | None | Literal[False]":
    """A reading whose scan provably began at or after ``not_before`` (serve clock), from
    one refreshed scan. None if none comes by ``deadline``; False if the serve can't give
    one (no /inventory, or a reply that can't be used) and the caller should fall back to
    the two-scan read. A failed request is neither: it is asked again.

    A refresh sent at our ``sent`` reaches the serve no earlier than ``sent + floor`` on
    its clock, so it is sent only once that is inside the window. The clock is read
    again once the reply is in, and the arrival is placed by the LOWER of the two: a
    serve clock stepped back before the request shows in the second reading, one
    stepped back after it in the first -- either way the bound still holds."""
    loop = asyncio.get_running_loop()
    floor = await _clock_floor(client, deadline)
    if floor is None or floor is False:
        return floor
    while True:
        # Differences of datetimes only: a baseline with no job behind it has a
        # not_before of datetime.min, which can't have a timedelta taken off it.
        wait = ((not_before - _now()) - floor).total_seconds()
        if wait > 0:
            await _sleep_capped(wait, deadline)
        if loop.time() >= deadline:
            logger.warning("vault: no refreshed reading begun after {}", not_before.isoformat())
            return None
        sent = _now()
        reply = await _read_inventory(client, refresh=True)
        if reply is False:
            return False
        # A scan begun inside the window can't have FINISHED before it opened: never so
        # with a sane clock, so a reply claiming it is not believed.
        if (reply is not None and reply.consistent and reply.at >= not_before
                and sent + floor >= not_before):
            if loop.time() >= deadline:
                return None
            after = await _clock_floor(client, deadline)
            if after is None or after is False:
                return after
            arrives = sent + min(floor, after)
            if arrives >= not_before:
                return VaultReading(reply.tix, reply.at, scan_from=arrives)
            floor = after
            continue
        if reply is None or sent + floor >= not_before:
            # A failed request, or a refresh in time but not usable -- a trade running,
            # or one completed during the scan: ask again shortly.
            await _sleep_capped(_MIN_POLL_S, deadline)
        # Otherwise sent too early (the wait was cut short): the top of the loop decides.


def _after_next_start(reading: VaultReading, not_after: Optional[datetime]) -> bool:
    if not_after is None or reading.at < not_after:
        return False
    logger.warning("vault: the only fresh reading ({}) is after the next trade "
                   "began ({})", reading.at.isoformat(), not_after.isoformat())
    return True


async def _read_within(client: Any, not_before: datetime,
                       not_after: Optional[datetime] = None) -> Optional[VaultReading]:
    """A reading whose whole scan ran inside [not_before, not_after), waiting out the
    cache. None if none comes in time, or the window has closed.

    One refreshed scan where the serve can give it (_read_refreshed). Otherwise: the
    first reading stamped inside the window only proves its scan ENDED there; the one
    returned is the next scan, which can only have begun after that end."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _WAIT_FOR_FRESH_S
    fresh = await _read_refreshed(client, not_before, deadline)
    if fresh is not False:
        return None if fresh is None or _after_next_start(fresh, not_after) else fresh
    # Its own budget: a refresh path that gave up late must not cost the two scans theirs.
    deadline = loop.time() + _WAIT_FOR_FRESH_S
    in_window_end: Optional[datetime] = None
    while True:
        reading = await _read(client)
        if reading and reading.at >= not_before:
            if _after_next_start(reading, not_after):
                return None
            if in_window_end is None:
                in_window_end = reading.at          # its scan may have begun before the window
            elif reading.at > in_window_end:
                # Began after in_window_end, so wholly inside the window.
                return VaultReading(reading.tix, reading.at, scan_from=in_window_end)
        if loop.time() >= deadline:
            logger.warning("vault: no reading taken after {} (last: {})",
                           not_before.isoformat(), reading and reading.at.isoformat())
            return None
        # A cached snapshot doesn't change before it expires, so ask again when it does.
        wait = _MIN_POLL_S
        if reading:
            expires = reading.at + _CACHE_TTL
            wait = max(wait, (max(expires, not_before) - _now()).total_seconds())
        await _sleep_capped(wait, deadline)


def _finished_at_all(jobs: list[Any]) -> Optional[list[datetime]]:
    """Every finished job's finish time -- or None if any finished job doesn't give one
    we can read, since its trade could then have moved the vault at any time."""
    out: list[datetime] = []
    for j in jobs:
        if not isinstance(j, dict) or str(j.get("state", "")).lower() not in ("done", "failed"):
            continue
        at = parse_utc(j.get("finishedAt"))
        if at is None:
            return None
        out.append(at)
    return out


async def baseline(client: Any) -> Optional[VaultReading]:
    """The vault count just before a trade: a reading taken after the serve's most
    recent job finished and settled into the collection. None if one can't be had --
    and then the trade must not start, because its outcome couldn't be checked."""
    jobs = await client.list_jobs()
    if jobs is None:
        return None
    if any(isinstance(j, dict) and str(j.get("state", "")).lower() in ("queued", "running") for j in jobs):
        return None   # another trade is moving the vault right now
    finished = _finished_at_all(jobs)
    if finished is None:
        return None
    newest = max(finished, default=None)
    not_before = newest + SETTLE_LAG if newest else datetime.min.replace(tzinfo=timezone.utc)
    reading = await _read_within(client, not_before)
    if reading is None:
        return None
    # AGAIN, after the reading: waiting out the cache takes up to a minute, and a trade
    # that ran during it would sit between this baseline and the trade it is for.
    if not await _quiet_through(client, reading):
        logger.warning("vault: another trade ran while the baseline was being read")
        return None
    return reading


async def _quiet_through(client: Any, reading: VaultReading) -> bool:
    """Whether, as of the serve's job list NOW, nothing is trading and every finished job
    had settled into the collection before the reading's scan can have started."""
    jobs = await client.list_jobs()
    if jobs is None:
        return False
    if any(isinstance(j, dict) and str(j.get("state", "")).lower() in ("queued", "running") for j in jobs):
        return False
    finished = _finished_at_all(jobs)
    began = reading.scan_from or reading.at
    return finished is not None and all(f + SETTLE_LAG <= began for f in finished)


async def after(client: Any, job: dict[str, Any], since: datetime) -> Optional[VaultReading]:
    """The vault count once THIS job's trade has settled -- with no other job having
    traded since the baseline (taken at ``since``). None if the job doesn't say when it
    finished, the job list can't be read, or no reading lands inside that window."""
    finished = parse_utc(job.get("finishedAt"))
    if finished is None:
        return None
    window_end = await _next_start(client, job, since)
    if window_end is _UNKNOWN:
        return None
    reading = await _read_within(client, finished + SETTLE_LAG, window_end)
    if reading is None:
        return None
    # AGAIN, after the reading: waiting out the cache takes up to a minute, and a job
    # that started during it -- before the reading was taken -- is inside the reading.
    window_end = await _next_start(client, job, since)
    if window_end is _UNKNOWN or (window_end is not None and window_end <= reading.at):
        logger.warning("vault: another trade began before the reading for {} was taken", job.get("id"))
        return None
    return reading


_UNKNOWN = datetime.max.replace(tzinfo=timezone.utc)   # sentinel: can't tell


async def _next_start(client: Any, job: dict[str, Any], since: datetime) -> Optional[datetime]:
    """The earliest start of any OTHER job that could have moved the vault after the
    baseline (taken at ``since``) -- before this job as well as after it; None if none
    did; _UNKNOWN if the list can't be read or such a job gives no start we can place."""
    jobs = await client.list_jobs()
    if jobs is None:
        return _UNKNOWN
    starts: list[datetime] = []
    for j in jobs:
        if not isinstance(j, dict) or j.get("id") == job.get("id"):
            continue
        state = str(j.get("state", "")).lower()
        if state == "queued":
            continue   # not started: it moves nothing yet
        ended = parse_utc(j.get("finishedAt"))
        if state in ("done", "failed") and ended is not None and ended <= since:
            continue   # over before the baseline, which already includes it
        started = parse_utc(j.get("startedAt"))
        if started is None:
            return _UNKNOWN   # it may have traded anywhere inside the window
        starts.append(started)
    return min(starts, default=None)


def classify(before: int, after_tix: int, expected_delta: int) -> Moved:
    """What the vault says the trade did: the full expected change, nothing, or
    something else (a partial move, or another movement inside the window)."""
    delta = after_tix - before
    if delta == expected_delta:
        return "moved"
    if delta == 0:
        return "none"
    return "other"
