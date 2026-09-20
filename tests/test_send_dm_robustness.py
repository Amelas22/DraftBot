"""send_dm's contract is "returns False on failure" -- for every failure.

It converts the id with int() inside its try, but catches only Discord's own
exceptions, so a user id that is not a number raises ValueError straight
through a function whose callers are written to read a bool. Every caller in
the codebase is exposed to it, and the ones that treat the call as
fire-and-forget lose the message with no way to tell.

A Discord user id looks like something you could write as a name, which is
exactly why an operator setting one in .env gets this wrong.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from notification_service import send_dm

pytestmark = pytest.mark.asyncio


def _bot():
    bot = MagicMock()
    bot.get_user.return_value = None
    bot.fetch_user = AsyncMock()
    return bot


@pytest.mark.parametrize("bad", ["aberdasher", "@aber", "", "12 34", None])
async def test_an_id_that_is_not_a_number_is_a_failure_not_an_exception(bad):
    bot = _bot()

    assert await send_dm(bot, bad, "hello") is False
    bot.fetch_user.assert_not_awaited()


async def test_a_real_id_still_sends():
    bot = _bot()
    user = MagicMock()
    user.send = AsyncMock()
    bot.get_user.return_value = user

    assert await send_dm(bot, "144605755826372608", "hello") is True
    user.send.assert_awaited_once()


@pytest.mark.parametrize("boom", [
    __import__("aiohttp").ClientConnectorError.__new__(__import__("aiohttp").ClientConnectorError),
    __import__("asyncio").TimeoutError(),
    RuntimeError("Session is closed"),
])
async def test_discord_being_unreachable_is_a_failure_not_an_exception(boom):
    """The contract is a bool, and callers are written against it. Only
    Discord's own exception types were caught, so the REST API being slow or
    unreachable -- most likely at startup, which is exactly when a monitor
    sends its first message -- escaped instead of returning False.
    """
    bot = MagicMock()
    bot.get_user.return_value = None
    bot.fetch_user = AsyncMock(side_effect=boom)

    assert await send_dm(bot, "144605755826372608", "hello") is False


async def test_a_user_that_cannot_be_found_at_all_is_a_failure():
    """get_user and fetch_user can both come back with nothing; calling .send
    on that raised AttributeError out of a function promising a bool."""
    bot = MagicMock()
    bot.get_user.return_value = None
    bot.fetch_user = AsyncMock(return_value=None)

    assert await send_dm(bot, "144605755826372608", "hello") is False
