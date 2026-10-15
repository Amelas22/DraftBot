"""The library's change bus: a dirty flag per library, not a message queue."""
import asyncio

import pytest

from services import library_events as bus


@pytest.mark.asyncio
async def test_a_burst_merges_into_at_most_two_deliveries():
    calls = []
    gate = asyncio.Event()

    async def listener(lid):
        calls.append(lid)
        if len(calls) == 1:
            await gate.wait()

    bus.subscribe("b1", listener, "lib")
    bus.publish("lib")
    await asyncio.sleep(0)      # first delivery starts and blocks on the gate
    for _ in range(9):
        bus.publish("lib")
    gate.set()
    await bus.drain()

    assert calls == ["lib", "lib"], "one delivery, plus one for the changes during it"


@pytest.mark.asyncio
async def test_a_failing_listener_does_not_stop_the_others():
    seen = []

    async def bad(_lid):
        raise RuntimeError("boom")

    async def good(lid):
        seen.append(lid)

    bus.subscribe("bad", bad, "lib")
    bus.subscribe("good", good, "lib")
    bus.publish("lib")
    await bus.drain()

    assert seen == ["lib"]


def test_publish_without_a_loop_or_listeners_returns_quietly():
    bus.publish("nobody-listens")          # no listeners: a no-op, no loop needed
    bus.subscribe("k", lambda _l: asyncio.sleep(0), "lib")
    bus.publish("lib")                     # no running loop: logged, never raised


@pytest.mark.asyncio
async def test_subscribing_twice_under_one_key_replaces():
    calls = []

    async def first(_lid):
        calls.append("first")

    async def second(_lid):
        calls.append("second")

    bus.subscribe("b1", first, "lib")
    bus.subscribe("b1", second, "lib")
    bus.publish("lib")
    await bus.drain()

    assert calls == ["second"]


@pytest.mark.asyncio
async def test_only_that_librarys_listeners_hear_it():
    heard = []

    async def a(lid):
        heard.append(("a", lid))

    async def b(lid):
        heard.append(("b", lid))

    bus.subscribe("a", a, "lib-a")
    bus.subscribe("b", b, "lib-b")
    bus.publish("lib-a")
    await bus.drain()

    assert heard == [("a", "lib-a")]


@pytest.mark.asyncio
async def test_one_change_during_a_delivery_gets_exactly_one_more():
    calls = []
    gate = asyncio.Event()

    async def listener(lid):
        calls.append(lid)
        if len(calls) == 1:
            await gate.wait()

    bus.subscribe("b1", listener, "lib")
    bus.publish("lib")
    await asyncio.sleep(0)      # first delivery starts and blocks on the gate
    bus.publish("lib")          # one change during the first delivery
    gate.set()
    await bus.drain()

    assert calls == ["lib", "lib"], "one delivery, plus one for the change during it"


@pytest.mark.asyncio
async def test_publish_for_guild_resolves_the_library(monkeypatch):
    heard = []

    async def listener(lid):
        heard.append(lid)

    async def library_id_for(guild_id):
        return "lib" if guild_id == "g1" else None

    monkeypatch.setattr("services.library_service.library_id_for", library_id_for)
    bus.subscribe("k", listener, "lib")
    bus.publish_for_guild("g1")
    bus.publish_for_guild("g-without-library")
    await bus.drain()

    assert heard == ["lib"]
