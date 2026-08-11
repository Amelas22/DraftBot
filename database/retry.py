"""Shared retry-with-backoff for transient SQLite write-lock errors.

One home for the "database is locked" backoff loop that money-path services need
around their transactions (wallet, resolution, escrow). 3 attempts, 1s initial
delay, doubling.
"""
import asyncio

from loguru import logger
from sqlalchemy.exc import OperationalError

_MAX_RETRIES = 3
_INITIAL_DELAY_S = 1.0


async def with_db_retry(thunk):
    """Run an async thunk, retrying on transient 'database is locked' with backoff."""
    delay = _INITIAL_DELAY_S
    for attempt in range(_MAX_RETRIES):
        try:
            return await thunk()
        except OperationalError as e:
            if "database is locked" in str(e) and attempt < _MAX_RETRIES - 1:
                logger.warning(
                    f"DB locked on attempt {attempt + 1}/{_MAX_RETRIES}, retrying in {delay}s..."
                )
                await asyncio.sleep(delay)
                delay *= 2
            else:
                raise
