# tests/test_cube_cache.py
"""A cube changes about once a fortnight: read it once, refresh it in the background."""
import asyncio

import pytest

import helpers.cube_list as cl


class _Fake:
    """CubeCobra's cubelist endpoint, scripted per call."""

    def __init__(self, *answers):
        self.answers = list(answers)     # each: (status, body, etag)
        self.requests = []               # the If-None-Match sent with each

    async def __call__(self, cube, etag):
        self.requests.append(etag)
        status, body, new_etag = self.answers.pop(0)
        if status == 304:
            return ("same", None, etag)
        if status != 200:
            return None
        return ("new", cl.parse_cube_list(body), new_etag)


@pytest.fixture
def cobra(monkeypatch):
    def install(*answers):
        fake = _Fake(*answers)
        monkeypatch.setattr(cl, "_get", fake)
        return fake
    return install


@pytest.mark.asyncio
async def test_a_cached_read_makes_no_request_at_any_age(cobra, monkeypatch):
    fake = cobra((200, "Bolt\nBolt\n", "e1"))
    assert await cl.fetch_cube("PowerLSV") == [{"name": "Bolt", "qty": 2}]
    monkeypatch.setattr(cl, "REFRESH_AFTER_S", 10**9)
    assert await cl.fetch_cube("PowerLSV ") == [{"name": "Bolt", "qty": 2}]
    assert fake.requests == [None]


@pytest.mark.asyncio
async def test_a_stale_entry_is_returned_at_once_and_refreshed_once(cobra, monkeypatch):
    fake = cobra((200, "Bolt\n", "e1"), (304, "", None))
    await cl.fetch_cube("c")
    monkeypatch.setattr(cl, "REFRESH_AFTER_S", -1)       # everything is stale now
    first = await cl.fetch_cube("c")
    second = await cl.fetch_cube("c")                    # refresh already in flight
    await cl.wait_for_refreshes()
    assert first == second == [{"name": "Bolt", "qty": 1}]
    assert fake.requests == [None, "e1"], "one conditional refresh, with the etag"


@pytest.mark.asyncio
async def test_a_changed_list_replaces_the_entry_and_publishes(cobra, monkeypatch):
    published = []
    monkeypatch.setattr(cl, "_libraries_offering", _offering({"c": ["lib"]}))
    monkeypatch.setattr("services.library_events.publish", published.append)
    cobra((200, "Bolt\n", "e1"), (200, "Bolt\nCounterspell\n", "e2"))
    await cl.fetch_cube("c")
    monkeypatch.setattr(cl, "REFRESH_AFTER_S", -1)
    await cl.fetch_cube("c")
    await cl.wait_for_refreshes()
    monkeypatch.setattr(cl, "REFRESH_AFTER_S", 10**9)
    assert {c["name"] for c in await cl.fetch_cube("c")} == {"Bolt", "Counterspell"}
    assert published == ["lib"]


@pytest.mark.asyncio
async def test_a_failed_refresh_keeps_the_entry(cobra, monkeypatch):
    cobra((200, "Bolt\n", "e1"), (503, "", None))
    await cl.fetch_cube("c")
    monkeypatch.setattr(cl, "REFRESH_AFTER_S", -1)
    await cl.fetch_cube("c")
    await cl.wait_for_refreshes()
    monkeypatch.setattr(cl, "REFRESH_AFTER_S", 10**9)
    assert await cl.fetch_cube("c") == [{"name": "Bolt", "qty": 1}]


@pytest.mark.asyncio
async def test_concurrent_first_reads_share_one_fetch(cobra):
    fake = cobra((200, "Bolt\n", "e1"))
    a, b = await asyncio.gather(cl.fetch_cube("c"), cl.fetch_cube("c"))
    assert a == b and fake.requests == [None]


@pytest.mark.asyncio
async def test_a_failed_first_read_caches_nothing(cobra):
    fake = cobra((503, "", None), (200, "Bolt\n", "e1"))
    assert await cl.fetch_cube("c") is None
    assert await cl.fetch_cube("c") == [{"name": "Bolt", "qty": 1}]
    assert fake.requests == [None, None]


@pytest.mark.asyncio
async def test_refresh_now_rereads_unconditionally_and_says_if_it_changed(cobra, monkeypatch):
    monkeypatch.setattr(cl, "_libraries_offering", _offering({}))
    fake = cobra((200, "Bolt\n", "e1"), (200, "Bolt\n", "e1"), (200, "Bolt\nIsland\n", "e2"))
    await cl.fetch_cube("c")
    assert await cl.refresh_now("c") == ([{"name": "Bolt", "qty": 1}], False)
    cards, changed = await cl.refresh_now("c")
    assert changed and {c["name"] for c in cards} == {"Bolt", "Island"}
    assert fake.requests == [None, None, None], "never conditional"


def _offering(table):
    async def offering(cube):
        return table.get(cube, [])
    return offering


@pytest.mark.asyncio
async def test_a_slow_background_refresh_does_not_undo_a_newer_refresh_now(monkeypatch):
    """The background read started first and answers last, with the list from
    before the admin's edit; the admin's re-read must stand."""
    monkeypatch.setattr(cl, "_libraries_offering", _offering({}))
    gate = asyncio.Event()
    calls = []

    async def get(cube, etag):
        calls.append(etag)
        if len(calls) == 1:
            return ("new", cl.parse_cube_list("Bolt\n"), "e1")
        if len(calls) == 2:                       # the background refresh
            await gate.wait()
            return ("new", cl.parse_cube_list("Bolt\nOld\n"), "e-old")
        return ("new", cl.parse_cube_list("Bolt\nIsland\n"), "e2")
    monkeypatch.setattr(cl, "_get", get)

    await cl.fetch_cube("c")
    monkeypatch.setattr(cl, "REFRESH_AFTER_S", -1)
    await cl.fetch_cube("c")                      # starts the background refresh
    await asyncio.sleep(0)
    cards, changed = await cl.refresh_now("c")
    assert changed
    gate.set()
    await cl.wait_for_refreshes()

    monkeypatch.setattr(cl, "REFRESH_AFTER_S", 10**9)
    assert {c["name"] for c in await cl.fetch_cube("c")} == {"Bolt", "Island"}


@pytest.mark.asyncio
async def test_refresh_now_reports_a_change_even_if_publishing_fails(cobra, monkeypatch):
    """The cache is already updated by then; raising would tell the admin the
    re-read failed when it did not."""
    async def broken(_cube):
        raise RuntimeError("database locked")
    monkeypatch.setattr(cl, "_libraries_offering", broken)
    cobra((200, "Bolt\n", "e1"), (200, "Bolt\nIsland\n", "e2"))
    await cl.fetch_cube("c")
    cards, changed = await cl.refresh_now("c")
    assert changed and {c["name"] for c in cards} == {"Bolt", "Island"}
