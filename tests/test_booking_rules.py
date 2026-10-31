"""The booking rules, exercised against an in-memory store (no database)."""

from datetime import datetime, timedelta, timezone

import pytest

from services.availability import floor_half_hour, naive_utc
from services.booking import WINDOW_CELLS, change

NOW = datetime(2026, 11, 2, 12, 10, tzinfo=timezone.utc)
GOOD = datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc)


class FakeStore:
    """Dict-backed BookingStore; ``write`` can be told to lose the race."""

    def __init__(self, booked=None, version=0, started=False, gone=False):
        self.row = None if gone else {"booked": booked, "version": version, "started": started}
        self.lose_race_to = None  # row state to adopt when write is called
        self.writes = []

    async def current(self, session):
        if self.row is None:
            return None
        return self.row["booked"], self.row["version"], self.row["started"]

    async def write(self, session, seen_version, values):
        self.writes.append((seen_version, values))
        if self.lose_race_to is not None:
            self.row = self.lose_race_to
            return False
        self.row["booked"] = values["booked_start_utc"]
        self.row["version"] = seen_version + 1
        return True


async def _book(store, start=GOOD, seen=0, now=NOW):
    return await change(None, store, "u1", "Ann", start, seen, now)


@pytest.mark.asyncio
async def test_gone():
    result = await _book(FakeStore(gone=True))
    assert (result.ok, result.code) == (False, "gone")


@pytest.mark.asyncio
async def test_started():
    result = await _book(FakeStore(started=True, booked=naive_utc(GOOD)))
    assert (result.ok, result.code) == (False, "started")
    assert result.old == GOOD


@pytest.mark.asyncio
async def test_changed_on_stale_version():
    store = FakeStore(version=3, booked=naive_utc(GOOD))
    result = await _book(store, seen=2)
    assert (result.ok, result.code, result.version) == (False, "changed", 3)
    assert result.old is None and result.new == GOOD
    assert store.writes == []


@pytest.mark.asyncio
async def test_bad_input_off_the_half_hour():
    result = await _book(FakeStore(), start=GOOD.replace(minute=15))
    assert (result.ok, result.code) == (False, "bad_input")


@pytest.mark.asyncio
async def test_past():
    result = await _book(FakeStore(), start=GOOD.replace(day=1))
    assert (result.ok, result.code) == (False, "past")


@pytest.mark.asyncio
async def test_out_of_window_boundary():
    edge = floor_half_hour(NOW) + WINDOW_CELLS * timedelta(minutes=30)
    refused = await _book(FakeStore(), start=edge)
    assert (refused.ok, refused.code) == (False, "out_of_window")
    allowed = await _book(FakeStore(), start=edge - timedelta(minutes=30))
    assert allowed.ok


@pytest.mark.asyncio
async def test_clear_when_not_booked():
    result = await _book(FakeStore(), start=None)
    assert (result.ok, result.code) == (False, "not_booked")


@pytest.mark.asyncio
async def test_clear_writes_nulls():
    store = FakeStore(booked=naive_utc(GOOD))
    result = await _book(store, start=None)
    assert result.ok and result.new is None and result.old == GOOD
    assert store.writes[0][1] == dict(booked_start_utc=None, booked_by=None, booked_by_name=None)


@pytest.mark.asyncio
async def test_lost_race_to_start():
    store = FakeStore()
    store.lose_race_to = {"booked": naive_utc(GOOD), "version": 1, "started": True}
    result = await _book(store)
    assert (result.ok, result.code, result.version) == (False, "started", 1)


@pytest.mark.asyncio
async def test_lost_race_to_another_booking():
    store = FakeStore()
    store.lose_race_to = {"booked": naive_utc(GOOD), "version": 1, "started": False}
    result = await _book(store)
    assert (result.ok, result.code, result.version) == (False, "changed", 1)
    assert result.new == GOOD


@pytest.mark.asyncio
async def test_lost_race_to_deletion():
    store = FakeStore()
    store.lose_race_to = None
    original_write = store.write

    async def write(session, seen, values):
        await original_write(session, seen, values)
        store.row = None
        return False

    store.write = write
    result = await _book(store)
    assert (result.ok, result.code) == (False, "gone")


@pytest.mark.asyncio
async def test_success_bumps_version_and_is_aware_utc():
    store = FakeStore(version=4)
    result = await _book(store, seen=4)
    assert result.ok and result.code is None and result.version == 5
    assert result.new == GOOD and result.new.tzinfo is not None
    assert result.new.utcoffset() == timedelta(0)
    assert store.writes[0] == (
        4,
        dict(booked_start_utc=naive_utc(GOOD), booked_by="u1", booked_by_name="Ann"),
    )
