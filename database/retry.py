"""Shared retry-with-backoff for transient SQLite write-lock errors.

One home for the "database is locked" backoff loop that money-path services need
around their transactions (wallet, resolution, escrow). 3 attempts, 1s initial
delay, doubling.
"""
import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from loguru import logger
from sqlalchemy.exc import OperationalError

_MAX_RETRIES = 3
_INITIAL_DELAY_S = 1.0

_T = TypeVar("_T")


async def with_db_retry(thunk: Callable[[], Awaitable[_T]]) -> _T:
    """Run an async thunk, retrying on transient 'database is locked' with backoff.

    Generic in the thunk's result, so a caller keeps the type it handed in --
    a money path that returns an EntryResult gets an EntryResult back, without
    asserting it with a cast.

    The final attempt sits outside the loop rather than inside it behind an
    `attempt < _MAX_RETRIES - 1` guard. Same three attempts and the same
    backoff, but the function now provably either returns the thunk's value or
    raises: there is no path that falls off the end and returns None, which is
    what made the old return type unprovable and indistinguishable from a thunk
    that legitimately returns None.
    """
    delay = _INITIAL_DELAY_S
    for attempt in range(_MAX_RETRIES - 1):
        try:
            return await thunk()
        except OperationalError as e:
            if "database is locked" not in str(e):
                raise
            logger.warning(
                f"DB locked on attempt {attempt + 1}/{_MAX_RETRIES}, retrying in {delay}s..."
            )
            await asyncio.sleep(delay)
            delay *= 2

    # The last attempt is not retried, so whatever it does is the caller's.
    return await thunk()
