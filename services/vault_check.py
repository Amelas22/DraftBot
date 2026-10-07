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
_WAIT_FOR_FRESH_S = 90.0
_MIN_POLL_S = 2.0

Moved = Literal["moved", "none", "other"]


@dataclass(frozen=True)
class VaultReading:
    tix: int
    at: datetime        # when the serve read the collection, UTC


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


async def _read_within(client: Any, not_before: datetime,
                       not_after: Optional[datetime] = None) -> Optional[VaultReading]:
    """A reading taken inside [not_before, not_after), waiting out the cache. None if
    none comes in time, or the window has closed."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _WAIT_FOR_FRESH_S
    while True:
        reading = await _read(client)
        if reading and reading.at >= not_before:
            if not_after is None or reading.at < not_after:
                return reading
            logger.warning("vault: the only fresh reading ({}) is after the next trade began ({})",
                           reading.at.isoformat(), not_after.isoformat())
            return None
        if loop.time() >= deadline:
            logger.warning("vault: no reading taken after {} (last: {})",
                           not_before.isoformat(), reading and reading.at.isoformat())
            return None
        # A cached snapshot doesn't change before it expires, so ask again when it does.
        wait = _MIN_POLL_S
        if reading:
            expires = reading.at + _CACHE_TTL
            wait = max(wait, (max(expires, not_before) - datetime.now(timezone.utc)).total_seconds())
        await asyncio.sleep(min(wait, max(0.0, deadline - loop.time())))


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
    if not await _quiet_through(client, reading.at):
        logger.warning("vault: another trade ran while the baseline was being read")
        return None
    return reading


async def _quiet_through(client: Any, at: datetime) -> bool:
    """Whether, as of the serve's job list NOW, nothing was trading at ``at`` or since
    and every finished job had settled into the collection by then."""
    jobs = await client.list_jobs()
    if jobs is None:
        return False
    if any(isinstance(j, dict) and str(j.get("state", "")).lower() in ("queued", "running") for j in jobs):
        return False
    finished = _finished_at_all(jobs)
    return finished is not None and all(f + SETTLE_LAG <= at for f in finished)


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
