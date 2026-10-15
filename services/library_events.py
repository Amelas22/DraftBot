"""What changed in a library, told to whoever is showing it.

The library owns its state -- loans, card bookings, reservations, offers -- and
boards that show that state listen here instead of being refreshed from every
place that writes it.

An event carries no data. It says only "library N changed, read it again":
listeners recompute from the database, which is the truth. That is why this is
a dirty flag per library and not a message queue -- merging events is free, a
lost one is repaired by the next change or at startup, and order cannot change
a result. A consumer that needs the events themselves (a DM per change, an
audit log) is the point at which this should become a real queue.

In-process only: scripts/library_tool.py runs in its own process and cannot
publish; its changes show at the next event or restart.
"""
import asyncio
from typing import Awaitable, Callable

from loguru import logger

Listener = Callable[[str], Awaitable[None]]

_listeners: "dict[str, tuple[str, Listener]]" = {}
_running: "dict[str, asyncio.Task[None]]" = {}
_dirty: "set[str]" = set()
_resolving: "set[asyncio.Task[None]]" = set()


def subscribe(key: str, listener: Listener, library_id: str) -> None:
    """Listen to one library under ``key``; a second subscribe replaces the first."""
    _listeners[str(key)] = (str(library_id), listener)


def unsubscribe(key: str) -> None:
    _listeners.pop(str(key), None)


def _heard(library_id: str) -> bool:
    return any(lid == library_id for lid, _ in _listeners.values())


def publish(library_id: str) -> None:
    """Tell this library's listeners to look again. Never blocks, never raises.

    A delivery already running for the library only marks it dirty, and runs
    once more when it finishes -- so a burst of changes costs at most two."""
    try:
        lid = str(library_id)
        if not _heard(lid):
            return
        if lid in _running:
            _dirty.add(lid)
            return
        _running[lid] = asyncio.get_running_loop().create_task(_deliver(lid))
    except Exception:
        logger.opt(exception=True).error("library events: could not publish for {}",
                                         library_id)


def publish_all() -> None:
    """Every library with a listener. For a change that names no one library: a bulk
    statement, or the lending custodian, which all libraries share."""
    for lid in {lid for lid, _ in _listeners.values()}:
        publish(lid)


def publish_for_guild(guild_id: str) -> None:
    """The library this server draws on, if it has one. Never blocks, never raises."""
    if not _listeners:
        return
    try:
        task = asyncio.get_running_loop().create_task(_publish_for_guild(str(guild_id)))
        _resolving.add(task)
        task.add_done_callback(_resolving.discard)
    except Exception:
        logger.opt(exception=True).error("library events: could not publish for guild {}",
                                         guild_id)


async def _publish_for_guild(guild_id: str) -> None:
    from services import library_service
    try:
        lid = await library_service.library_id_for(guild_id)
    except Exception:
        logger.opt(exception=True).warning("library events: no library resolved for {}",
                                           guild_id)
        return
    if lid is not None:
        publish(lid)


async def _deliver(lid: str) -> None:
    try:
        while True:
            _dirty.discard(lid)
            for key, (owner, listener) in list(_listeners.items()):
                if owner != lid:
                    continue
                try:
                    await listener(lid)
                except Exception:
                    logger.opt(exception=True).warning(
                        "library events: listener {} failed for library {}", key, lid)
            if lid not in _dirty:
                return
    finally:
        _running.pop(lid, None)


async def drain() -> None:
    """Wait until nothing is pending. For tests, and for an orderly shutdown."""
    while _running or _resolving:
        await asyncio.gather(*_running.values(), *_resolving, return_exceptions=True)


def reset() -> None:
    """Forget every listener and pending delivery. For tests."""
    for task in list(_running.values()) + list(_resolving):
        try:
            task.cancel()
        except RuntimeError:
            pass        # its loop is already closed, so it can never run again
    _listeners.clear()
    _running.clear()
    _dirty.clear()
    _resolving.clear()
