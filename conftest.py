"""Root pytest configuration.

Provides a running asyncio event loop for synchronous tests that instantiate
discord.ui.View, which calls asyncio.get_running_loop() unconditionally in
py-cord.  We patch asyncio.get_running_loop only during sync tests so we don't
interfere with the pytest-asyncio managed loop used by async tests.
"""
import asyncio
import inspect
import pytest


@pytest.fixture(autouse=True)
def _sync_test_event_loop(request, monkeypatch):
    """Patch asyncio.get_running_loop() for sync tests only.

    Async tests already run inside a loop managed by pytest-asyncio, so we
    skip patching them to avoid cross-loop Future confusion.
    """
    # Check if the test function is a coroutine (async def); if so, skip.
    if inspect.iscoroutinefunction(request.function):
        yield
        return

    loop = asyncio.new_event_loop()
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: loop)
    try:
        yield
    finally:
        loop.close()
