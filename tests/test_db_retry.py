"""The retry every money path wraps its transaction in.

wallet_service, the escrow service, the resolution service and the draft pool
all route their writes through with_db_retry, so its control flow is load
bearing in ten places and was covered by nothing. These pin the behaviour: how
many attempts, which errors are retried, and what comes back.
"""
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.exc import OperationalError

from database.retry import with_db_retry


def _locked():
    return OperationalError("stmt", {}, Exception("database is locked"))


def _other():
    return OperationalError("stmt", {}, Exception("no such table: widgets"))


@pytest.mark.asyncio
async def test_a_thunk_that_works_runs_once_and_returns_its_value():
    thunk = AsyncMock(return_value={"ok": True, "deficit": 0})

    assert await with_db_retry(thunk) == {"ok": True, "deficit": 0}
    assert thunk.await_count == 1


@pytest.mark.asyncio
async def test_a_locked_database_is_retried_and_the_value_still_comes_back():
    thunk = AsyncMock(side_effect=[_locked(), "settled"])

    with patch("database.retry.asyncio.sleep", new=AsyncMock()):
        assert await with_db_retry(thunk) == "settled"
    assert thunk.await_count == 2


@pytest.mark.asyncio
async def test_a_database_locked_all_the_way_through_raises_after_three_tries():
    """Three attempts, then the caller hears about it -- a money path must not
    retry forever, and must not swallow the failure either."""
    thunk = AsyncMock(side_effect=[_locked(), _locked(), _locked()])

    with patch("database.retry.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(OperationalError):
            await with_db_retry(thunk)
    assert thunk.await_count == 3


@pytest.mark.asyncio
async def test_the_backoff_doubles_between_attempts():
    thunk = AsyncMock(side_effect=[_locked(), _locked(), "done"])
    slept = []

    async def _sleep(n):
        slept.append(n)

    with patch("database.retry.asyncio.sleep", new=_sleep):
        assert await with_db_retry(thunk) == "done"
    assert slept == [1.0, 2.0], slept


@pytest.mark.asyncio
async def test_an_unrelated_database_error_is_not_retried():
    """Retrying a schema error just delays the same failure three times."""
    thunk = AsyncMock(side_effect=_other())

    with pytest.raises(OperationalError):
        await with_db_retry(thunk)
    assert thunk.await_count == 1


@pytest.mark.asyncio
async def test_a_non_database_error_is_not_retried():
    thunk = AsyncMock(side_effect=ValueError("boom"))

    with pytest.raises(ValueError):
        await with_db_retry(thunk)
    assert thunk.await_count == 1


@pytest.mark.asyncio
async def test_none_is_a_real_return_value_not_a_fallthrough():
    """A thunk that legitimately returns None must be indistinguishable from
    success -- the old shape could also fall off the end of its loop and return
    None implicitly, which is what made the return type unprovable."""
    thunk = AsyncMock(return_value=None)

    assert await with_db_retry(thunk) is None
    assert thunk.await_count == 1
