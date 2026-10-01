"""A background loop is started once, and started again only if it has died.

on_ready refires on every gateway reconnect. Starting its loops unconditionally
added a copy per reconnect; starting them only once meant a loop that died
stayed dead until the next restart.
"""
import asyncio
from types import SimpleNamespace

import pytest

from helpers.background_tasks import ensure_running


@pytest.mark.asyncio
async def test_a_running_loop_is_not_started_again():
    bot = SimpleNamespace(loop=asyncio.get_running_loop())
    stop = asyncio.Event()
    starts = []

    def start():
        starts.append(1)
        return stop.wait()

    first = ensure_running(bot, "_watcher_task", start)
    second = ensure_running(bot, "_watcher_task", start)

    assert second is first
    assert len(starts) == 1
    stop.set()
    await first


@pytest.mark.asyncio
async def test_a_loop_that_died_is_started_again():
    bot = SimpleNamespace(loop=asyncio.get_running_loop())

    async def dies():
        raise RuntimeError("database is locked")

    dead = ensure_running(bot, "_watcher_task", dies)
    with pytest.raises(RuntimeError):
        await dead

    stop = asyncio.Event()
    revived = ensure_running(bot, "_watcher_task", stop.wait)

    assert revived is not dead and not revived.done()
    stop.set()
    await revived
