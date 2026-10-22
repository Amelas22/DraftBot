import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from database.db_session import db_session
from models.scheduler import PlayerAvailability, SchedulerLink
from scheduler_fakes import FakeSchedulerSpaces
from services.availability_sync import AvailabilitySync, get_usual_week
from services.scheduler_links import issue_usual_link

NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)
UID = "900000000000000002"
KEY = f"scheduler/availability/{UID}.json"
GOOD = (Path(__file__).parent.parent / "scheduler_page/fixtures/usual-week.json").read_bytes()
T1 = NOW - timedelta(hours=3)
T2 = NOW - timedelta(hours=2)
T3 = NOW - timedelta(hours=1)


async def _setup(issued: datetime = NOW) -> tuple[FakeSchedulerSpaces, AvailabilitySync]:
    spaces = FakeSchedulerSpaces()
    await issue_usual_link(spaces, UID, issued)
    return spaces, AvailabilitySync(spaces)


async def _rows() -> list[PlayerAvailability]:
    async with db_session() as session:
        return list((await session.execute(select(PlayerAvailability))).scalars())


@pytest.mark.asyncio
async def test_stores_a_saved_week(test_db):
    spaces, sync = await _setup()
    spaces.put(KEY, GOOD, T1)
    result = await sync.poll_once(NOW)
    assert result.stored == [UID]
    assert len(await _rows()) == 1
    week = await get_usual_week(UID)
    assert week is not None
    assert week.zone == "America/New_York"
    assert len(week.cells) == 52
    assert await get_usual_week("1") is None


@pytest.mark.asyncio
async def test_unchanged_object_is_not_refetched(test_db):
    spaces, sync = await _setup()
    spaces.put(KEY, GOOD, T1)
    await sync.poll_once(NOW)
    await sync.poll_once(NOW)
    assert spaces.fetch_calls == [KEY]


@pytest.mark.asyncio
async def test_invalid_file_is_skipped_and_not_refetched(test_db):
    spaces, sync = await _setup()
    spaces.put(KEY, GOOD, T1)
    await sync.poll_once(NOW)
    before = (await _rows())[0].cells

    big = b"x" * 5000
    other = json.dumps({"format": "wrong", "version": 1}).encode()
    key2 = "scheduler/availability/900000000000000003.json"
    spaces.put(KEY, big, T2)
    spaces.put(key2, other, T2)
    result = await sync.poll_once(NOW)
    assert sorted(result.rejected) == sorted([KEY, key2])
    rows = await _rows()
    assert len(rows) == 1 and rows[0].cells == before

    spaces.fetch_calls.clear()
    result = await sync.poll_once(NOW)
    assert spaces.fetch_calls == []
    assert result.rejected == []

    spaces.put(KEY, GOOD, T3)
    result = await sync.poll_once(NOW)
    assert result.stored == [UID]
    assert (await _rows())[0].object_modified_utc == T3.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_older_object_never_overwrites(test_db):
    spaces, sync = await _setup()
    spaces.put(KEY, GOOD, T2)
    await sync.poll_once(NOW)
    before = (await _rows())[0].cells
    other = json.loads(GOOD)
    other["cells"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"[:56]
    spaces.put(KEY, json.dumps(other).encode(), T1)
    spaces.fetch_calls.clear()
    result = await sync.poll_once(NOW)
    assert result.stored == [] and spaces.fetch_calls == []
    rows = await _rows()
    assert rows[0].cells == before
    assert rows[0].object_modified_utc == T2.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_catches_up_after_restart(test_db):
    spaces, sync = await _setup()
    spaces.put(KEY, GOOD, T1)
    await sync.poll_once(NOW)
    other = json.loads(GOOD)
    other["zone"] = "Europe/London"
    spaces.put(KEY, json.dumps(other).encode(), T2)
    result = await AvailabilitySync(spaces).poll_once(NOW)
    assert result.stored == [UID]
    week = await get_usual_week(UID)
    assert week is not None and week.zone == "Europe/London"


@pytest.mark.asyncio
async def test_skips_spaces_when_no_recent_link(test_db):
    spaces, sync = await _setup(issued=NOW - timedelta(days=9))
    spaces.put(KEY, GOOD, T1)
    result = await sync.poll_once(NOW)
    assert spaces.list_calls == [] and spaces.fetch_calls == []
    assert result.stored == []


@pytest.mark.asyncio
async def test_lists_for_a_link_just_past_its_expiry(test_db):
    spaces, sync = await _setup(issued=NOW - timedelta(days=7, hours=12))
    spaces.put(KEY, GOOD, T1)
    await sync.poll_once(NOW)
    assert spaces.list_calls != []


@pytest.mark.asyncio
async def test_removes_expired_link_files(test_db):
    spaces, sync = await _setup(issued=NOW - timedelta(days=8))
    async with db_session() as session:
        link = (await session.execute(select(SchedulerLink))).scalars().one()
        key = f"scheduler/link/{link.token}.json"
    assert key in spaces.objects
    first = await sync.poll_once(NOW)
    second = await sync.poll_once(NOW)
    assert key not in spaces.objects
    assert (first.links_removed, second.links_removed) == (1, 0)
    async with db_session() as session:
        link = (await session.execute(select(SchedulerLink))).scalars().one()
        assert link.removed_utc == NOW.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_ignores_non_numeric_keys(test_db):
    spaces, sync = await _setup()
    spaces.put("scheduler/availability/../x.json", GOOD, T1)
    spaces.put("scheduler/availability/abc.json", GOOD, T1)
    result = await sync.poll_once(NOW)
    assert spaces.fetch_calls == []
    assert result.stored == [] and result.rejected == []
