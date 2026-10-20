from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from database.db_session import AsyncSessionLocal
from models.library_hold import LibraryHold
from services import library_ledger_store as store
from services.library_ledger import SERVER_WINDOW

NOW = datetime(2026, 10, 18, 12, 0)
LIB = "lib1"
CUBE = "cube1"


async def fake_fetch(cube_id):
    return [{"name": "Island", "qty": 1}]


async def unreadable(cube_id):
    return None


@pytest.fixture
def holdings(monkeypatch):
    async def fake(library_id):
        return {"Island": 2}

    monkeypatch.setattr(
        "services.card_library_inventory.library_holdings", fake)


async def _hold(draft):
    await store.book_server_hold(LIB, CUBE, draft, NOW)


@pytest.mark.asyncio
async def test_server_verdict_counts_existing_holds(test_db, holdings):
    await _hold("d1")
    v = await store.server_verdict(LIB, CUBE, NOW, fetch=fake_fetch)
    assert v is not None and v.ok
    await _hold("d2")
    v = await store.server_verdict(LIB, CUBE, NOW, fetch=fake_fetch)
    assert v is not None and not v.ok and v.peak == 2


@pytest.mark.asyncio
async def test_server_verdict_is_none_for_an_unreadable_cube(test_db, holdings):
    assert await store.server_verdict(LIB, CUBE, NOW, fetch=unreadable) is None


@pytest.mark.asyncio
async def test_booking_twice_keeps_one_live_hold(test_db):
    await _hold("d1")
    later = NOW + timedelta(minutes=5)
    await store.book_server_hold(LIB, CUBE, "d1", later)
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(LibraryHold).order_by(LibraryHold.id))
                ).scalars().all()
    assert len(rows) == 2
    assert rows[0].released_at == later
    assert rows[1].released_at is None and rows[1].starts_at == later


@pytest.mark.asyncio
async def test_release_ends_the_live_hold(test_db):
    await _hold("d1")
    after = NOW + SERVER_WINDOW + timedelta(hours=1)
    assert await store.release_server_hold("d1", NOW + timedelta(minutes=1))
    spans = await store.spans_overlapping(LIB, after, after + SERVER_WINDOW, after)
    assert spans == []
    assert not await store.release_server_hold("d1", after)


@pytest.mark.asyncio
async def test_spans_include_a_live_hold_past_its_window(test_db):
    await store.book_server_hold(LIB, CUBE, "d1", NOW - timedelta(hours=5))
    spans = await store.spans_overlapping(LIB, NOW, NOW + SERVER_WINDOW, NOW)
    assert len(spans) == 1
