from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from database.db_session import AsyncSessionLocal
from models.draft_session import DraftSession
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


async def _reserved(draft, at=NOW):
    """A draft holding a reservation: what a live server hold stands for."""
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(session_id=draft, guild_id="g1", cube=CUBE,
                           library_reserved_at=at))
        await s.commit()


async def _hold(draft):
    await _reserved(draft)
    await store.book_server_hold(LIB, CUBE, draft, NOW)


async def _live_holds():
    async with AsyncSessionLocal() as s:
        return list((await s.scalars(select(LibraryHold).where(
            LibraryHold.released_at.is_(None)))).all())


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
    await _reserved("d1", NOW - timedelta(hours=5))
    await store.book_server_hold(LIB, CUBE, "d1", NOW - timedelta(hours=5))
    spans = await store.spans_overlapping(LIB, NOW, NOW + SERVER_WINDOW, NOW)
    assert len(spans) == 1


@pytest.mark.asyncio
async def test_a_hold_whose_draft_is_gone_is_released_and_not_counted(test_db):
    await store.book_server_hold(LIB, CUBE, "gone", NOW)

    assert await store.spans_overlapping(LIB, NOW, NOW + SERVER_WINDOW, NOW) == []

    async with AsyncSessionLocal() as s:
        hold = (await s.scalars(select(LibraryHold))).one()
    assert hold.released_at == NOW and hold.released_by == "ledger:stale"


@pytest.mark.asyncio
async def test_a_hold_whose_reservation_ended_is_released_at_that_moment(test_db):
    await _hold("d1")
    await _hold("d2")
    ended = NOW + timedelta(minutes=10)
    async with AsyncSessionLocal() as s:
        row = (await s.scalars(select(DraftSession).where(
            DraftSession.session_id == "d1"))).one()
        row.library_released_at = ended
        await s.commit()

    later = NOW + timedelta(minutes=20)
    spans = await store.spans_overlapping(LIB, later, later + SERVER_WINDOW, later)

    assert len(spans) == 1, "d2 still counts; d1 does not"
    async with AsyncSessionLocal() as s:
        d1 = (await s.scalars(select(LibraryHold).where(
            LibraryHold.draft_session_id == "d1"))).one()
    assert d1.released_at == ended and d1.released_by == "ledger:stale"
    assert [h.draft_session_id for h in await _live_holds()] == ["d2"]


@pytest.mark.asyncio
async def test_a_hold_whose_draft_never_reserved_is_released(test_db):
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(session_id="d1", guild_id="g1", cube=CUBE))
        await s.commit()
    await store.book_server_hold(LIB, CUBE, "d1", NOW)

    assert await store.spans_overlapping(LIB, NOW, NOW + SERVER_WINDOW, NOW) == []
    assert await _live_holds() == []


@pytest.mark.asyncio
async def test_a_stale_release_never_ends_a_newer_hold(test_db):
    """A release decided before a re-reserve must not end the hold the
    re-reserve booked: it can only end a hold that began by then."""
    await _hold("d1")
    rebooked = NOW + timedelta(seconds=1)
    await store.book_server_hold(LIB, CUBE, "d1", rebooked)

    assert not await store.release_server_hold(
        "d1", NOW + timedelta(milliseconds=500))

    live = await _live_holds()
    assert len(live) == 1 and live[0].starts_at == rebooked
