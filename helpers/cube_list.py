"""Reading a cube's card list off CubeCobra.

The bot already stores a cube's NAME on every draft session and renders it as a
cubecobra.com link, so a cube is already a first-class identifier here -- this
adds the one thing that was missing, which is the list behind it.

`/cube/api/cubelist/<id>` returns plain names, one per line, and that is all a
deposit needs: the TradeBot records which printings actually crossed and hands
those exact copies back, so naming a printing here would be a promise this side
cannot keep and does not need to. The JSON endpoint carries printings and is
170x larger (766 KB against 4.5 KB for a 300-card cube); its per-card `mtgo_id`
is also per-PRINTING, so a card whose cube entry is a promo would read as "not
on MTGO" when the card is on MTGO perfectly well under another printing.

Lists are cached for the process and re-checked in the background with their
ETag once 12 hours old, because a cube changes about once a fortnight and never
mid-run; only a cube never read before waits on the network.

The plain list also leaves basics out -- they live on the cube's own `basics`
board -- which is the right default for a bot that already holds plenty.
"""
import asyncio
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp
from loguru import logger

CUBE_LIST_URL = "https://cubecobra.com/cube/api/cubelist/{}"
_TIMEOUT = aiohttp.ClientTimeout(total=30)


REFRESH_AFTER_S = 12 * 3600
# A failed refresh is retried no sooner than this, so a CubeCobra outage costs one
# request per cube per few minutes rather than one per read.
_RETRY_AFTER_S = 5 * 60


@dataclass
class _Entry:
    cards: "list[dict[str, Any]]"
    etag: Optional[str]
    checked_at: float


_cache: "dict[str, _Entry]" = {}
_first_reads: "dict[str, asyncio.Future[Optional[list[dict[str, Any]]]]]" = {}
_refreshing: "dict[str, asyncio.Task[None]]" = {}


async def fetch_cube(cube: str) -> "Optional[list[dict[str, Any]]]":
    """`[{"name": str, "qty": int}]` for a cube, or None if it cannot be read.

    None rather than an empty list for a failure, because "the cube is empty"
    and "CubeCobra did not answer" lead to different messages -- one is a cube
    to fix, the other is a thing to retry.

    Cached for the process: a cached list is returned at once whatever its age,
    and one past REFRESH_AFTER_S is re-checked in the background with its ETag.
    Only a cube never read before waits on the network.
    """
    key = cube.strip()
    entry = _cache.get(key)
    if entry is not None:
        if time.monotonic() - entry.checked_at > REFRESH_AFTER_S and key not in _refreshing:
            _refreshing[key] = asyncio.get_running_loop().create_task(_refresh(key))
        return entry.cards
    pending = _first_reads.get(key)
    if pending is not None:
        return await asyncio.shield(pending)
    future: "asyncio.Future[Optional[list[dict[str, Any]]]]" = (
        asyncio.get_running_loop().create_future())
    _first_reads[key] = future
    try:
        got = await _get(key, None)
        cards = got[1] if got is not None and got[0] == "new" else None
        if cards is not None:
            _cache[key] = _Entry(cards, got[2] if got else None, time.monotonic())
        future.set_result(cards)
        return cards
    except BaseException:
        future.set_result(None)
        raise
    finally:
        _first_reads.pop(key, None)


async def _get(cube: str, etag: Optional[str]
               ) -> "Optional[tuple[str, Optional[list[dict[str, Any]]], Optional[str]]]":
    """One request. ("new", cards, etag) | ("same", None, etag) | None on failure."""
    url = CUBE_LIST_URL.format(cube)
    headers: "dict[str, str]" = {"If-None-Match": etag} if etag else {}
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 304:
                    return ("same", None, etag)
                if resp.status != 200:
                    logger.warning("cube list {} -> HTTP {}", cube, resp.status)
                    return None
                text = await resp.text()
                return ("new", parse_cube_list(text), resp.headers.get("ETag"))
    except Exception as e:
        logger.warning("cube list {} could not be read: {}", cube, e)
        return None


async def _refresh(key: str) -> None:
    try:
        entry = _cache.get(key)
        if entry is None:
            return
        got = await _get(key, entry.etag)
        if got is None:
            entry.checked_at = time.monotonic() - REFRESH_AFTER_S + _RETRY_AFTER_S
            return
        await _store(key, entry, got)
    except Exception:
        logger.opt(exception=True).warning("cube list {}: background refresh failed", key)
    finally:
        _refreshing.pop(key, None)


async def _store(key: str, entry: Optional[_Entry],
                 got: "tuple[str, Optional[list[dict[str, Any]]], Optional[str]]") -> bool:
    """Record a refresh's answer. True if the list changed (and was published)."""
    kind, cards, etag = got
    now = time.monotonic()
    if kind == "same" and entry is not None:
        entry.checked_at = now
        return False
    if cards is None:
        return False
    changed = entry is None or _counts(cards) != _counts(entry.cards)
    _cache[key] = _Entry(cards, etag, now)
    if changed and entry is not None:
        await _publish_cube(key)
    return changed and entry is not None


def _counts(cards: "list[dict[str, Any]]") -> "dict[str, int]":
    return {c["name"]: c["qty"] for c in cards}


async def _libraries_offering(cube: str) -> "list[str]":
    from sqlalchemy import select
    from database.db_session import AsyncSessionLocal
    from models.library_cube import LibraryCube
    async with AsyncSessionLocal() as session:
        rows = await session.scalars(select(LibraryCube.library_id)
                                     .where(LibraryCube.cube_id == cube))
        return [str(r) for r in rows]


async def _publish_cube(cube: str) -> None:
    from services import library_events
    for lid in await _libraries_offering(cube):
        library_events.publish(lid)


async def refresh_now(cube: str) -> "tuple[Optional[list[dict[str, Any]]], bool]":
    """Re-read a cube unconditionally (an admin who just edited it). (cards, changed);
    cards None if CubeCobra could not be read, in which case the entry is kept."""
    key = cube.strip()
    got = await _get(key, None)
    if got is None or got[1] is None:
        return (None, False)
    changed = await _store(key, _cache.get(key), got)
    return (got[1], changed)


async def wait_for_refreshes() -> None:
    """For tests: wait until no background refresh is running."""
    while _refreshing:
        await asyncio.gather(*_refreshing.values(), return_exceptions=True)


def clear_cache() -> None:
    """For tests: forget every cube."""
    for task in _refreshing.values():
        task.cancel()
    _cache.clear()
    _first_reads.clear()
    _refreshing.clear()

def parse_cube_list(text: str) -> "list[dict[str, Any]]":
    """The response body, as items the serve will take.

    Repeats are counted rather than listed: a cube that runs four Lightning
    Bolt sends them as one item with a quantity, which is what the serve's
    items[] wants and what stops a deck of four becoming four line items.

    Separate from the fetch so it can be read and tested without a network:
    the counting is the part with a decision in it.
    """
    names = [line.strip() for line in text.splitlines() if line.strip()]
    return [{"name": n, "qty": q} for n, q in Counter(names).items()]
