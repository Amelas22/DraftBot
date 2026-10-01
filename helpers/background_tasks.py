"""Background loops that must run exactly once, and keep running."""
import asyncio
from typing import Any, Callable, Coroutine


def ensure_running(bot: Any, attr: str,
                   start: Callable[[], Coroutine[Any, Any, Any]]) -> "asyncio.Task[Any]":
    """Start the loop held at `bot.<attr>` unless it is already running.

    on_ready refires on every gateway reconnect, so starting a loop there
    unconditionally adds a copy per reconnect. Checking done() rather than
    "ever started" also restarts a loop that has died.

    The task is kept on the bot because asyncio holds only a weak reference to
    a running task.
    """
    running: "asyncio.Task[Any] | None" = getattr(bot, attr, None)
    if running is not None and not running.done():
        return running
    task: "asyncio.Task[Any]" = bot.loop.create_task(start())
    setattr(bot, attr, task)
    return task
