from datetime import datetime, timedelta, timezone

import pytest

from conftest import test_db  # noqa: F401
from database.db_session import db_session
from models.team_draft import TeamDraft
import services.team_draft as svc
from services.team_draft import Picks, book, claim_draft, clear, create_team_draft

UTC = timezone.utc
NOW = datetime(2026, 10, 26, 12, 10, tzinfo=UTC)
START = datetime(2026, 10, 28, 19, 30, tzinfo=UTC)


async def _make():
    async with db_session() as s:
        td = await create_team_draft(s, "900", "c", "A vs B", Picks(("1",), ()), Picks(("2",), ()), "9", NOW)
        await s.commit()
        return td.id


async def _row(td_id):
    async with db_session() as s:
        td = await s.get(TeamDraft, td_id)
        return td.booked_start_utc, td.booked_by, td.booked_by_name, td.booking_version


async def _book(td_id, start=START, seen=0, user="u1", name="Ann", now=NOW):
    async with db_session() as s:
        change = await book(s, td_id, user, name, start, seen, now)
        await s.commit()
        return change


async def _clear(td_id, seen, user="u1", name="Ann", now=NOW):
    async with db_session() as s:
        change = await clear(s, td_id, user, name, seen, now)
        await s.commit()
        return change


@pytest.mark.asyncio
async def test_book_stores_the_time_and_the_booker(test_db):  # noqa: F811
    td_id = await _make()
    change = await _book(td_id)
    assert (change.ok, change.code, change.old, change.new) == (True, None, None, START)
    assert (change.by_name, change.version) == ("Ann", 1)
    assert await _row(td_id) == (datetime(2026, 10, 28, 19, 30), "u1", "Ann", 1)


@pytest.mark.asyncio
async def test_book_accepts_a_non_utc_aware_time(test_db):  # noqa: F811
    td_id = await _make()
    plus2 = timezone(timedelta(hours=2))
    change = await _book(td_id, start=datetime(2026, 10, 28, 21, 30, tzinfo=plus2))
    assert change.ok and change.new == START
    assert (await _row(td_id))[0] == datetime(2026, 10, 28, 19, 30)


@pytest.mark.asyncio
async def test_gone_when_there_is_no_matchup(test_db):  # noqa: F811
    assert (await _book(999)).code == "gone"
    assert (await _clear(999, 0)).code == "gone"


@pytest.mark.asyncio
async def test_started_when_a_draft_claimed_the_matchup(test_db):  # noqa: F811
    td_id = await _make()
    async with db_session() as s:
        assert await claim_draft(s, td_id, "d1")
        await s.commit()
    assert (await _book(td_id)).code == "started"
    assert (await _clear(td_id, 0)).code == "started"
    assert (await _row(td_id))[3] == 0


@pytest.mark.asyncio
async def test_changed_when_the_version_is_stale_and_carries_the_current_booking(test_db):  # noqa: F811
    td_id = await _make()
    await _book(td_id)
    stale = await _book(td_id, start=START + timedelta(hours=1), seen=0, user="u2", name="Bob")
    assert (stale.ok, stale.code, stale.old, stale.new, stale.version) == (False, "changed", None, START, 1)
    cleared = await _clear(td_id, 0)
    assert (cleared.code, cleared.new, cleared.version) == ("changed", START, 1)
    assert (await _row(td_id))[1:] == ("u1", "Ann", 1)


@pytest.mark.asyncio
async def test_bad_input_when_not_on_a_half_hour(test_db):  # noqa: F811
    td_id = await _make()
    for bad in (START + timedelta(minutes=10), START + timedelta(seconds=1), START + timedelta(microseconds=1)):
        assert (await _book(td_id, start=bad)).code == "bad_input"
    assert (await _row(td_id))[3] == 0


@pytest.mark.asyncio
async def test_past_when_before_now(test_db):  # noqa: F811
    td_id = await _make()
    assert (await _book(td_id, start=datetime(2026, 10, 26, 12, 0, tzinfo=UTC))).code == "past"
    assert (await _book(td_id, start=datetime(2026, 10, 26, 12, 30, tzinfo=UTC))).ok


@pytest.mark.asyncio
async def test_out_of_window_from_seven_days_after_the_floored_half_hour(test_db):  # noqa: F811
    td_id = await _make()
    edge = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    assert (await _book(td_id, start=edge)).code == "out_of_window"
    assert (await _book(td_id, start=edge - timedelta(minutes=30))).ok


@pytest.mark.asyncio
async def test_not_booked_when_clearing_nothing(test_db):  # noqa: F811
    td_id = await _make()
    change = await _clear(td_id, 0)
    assert (change.ok, change.code) == (False, "not_booked")
    assert (await _row(td_id))[3] == 0


@pytest.mark.asyncio
async def test_book_then_change_then_clear_bumps_the_version_each_time(test_db):  # noqa: F811
    td_id = await _make()
    first = await _book(td_id)
    later = START + timedelta(hours=2)
    moved = await _book(td_id, start=later, seen=first.version, user="u2", name="Bob")
    assert (moved.ok, moved.old, moved.new, moved.version, moved.by_name) == (True, START, later, 2, "Bob")
    assert await _row(td_id) == (later.replace(tzinfo=None), "u2", "Bob", 2)
    gone = await _clear(td_id, moved.version, user="u3", name="Cy")
    assert (gone.ok, gone.old, gone.new, gone.version, gone.by_name) == (True, later, None, 3, "Cy")
    assert await _row(td_id) == (None, None, None, 3)


@pytest.mark.asyncio
async def test_two_bookings_on_one_version_one_wins(test_db):  # noqa: F811
    td_id = await _make()
    async with db_session() as s1, db_session() as s2:
        first = await book(s1, td_id, "u1", "Ann", START, 0, NOW)
        await s1.commit()
        second_start = START + timedelta(hours=1)
        second = await book(s2, td_id, "u2", "Bob", second_start, 0, NOW)
        assert first.ok
        assert (second.ok, second.code) == (False, "changed")
        assert second.new == START and second.version == 1
        await s2.commit()
    assert await _row(td_id) == (datetime(2026, 10, 28, 19, 30), "u1", "Ann", 1)


@pytest.mark.asyncio
async def test_the_conditional_update_decides_when_both_read_the_same_version(test_db):  # noqa: F811
    td_id = await _make()
    async with db_session() as s1, db_session() as s2:
        # s2 reads version 0 first, then s1 commits a booking before s2 writes.
        await s2.get(TeamDraft, td_id)
        await book(s1, td_id, "u1", "Ann", START, 0, NOW)
        await s1.commit()
        second = await book(s2, td_id, "u2", "Bob", START + timedelta(hours=1), 0, NOW)
        assert (second.code, second.new, second.version) == ("changed", START, 1)


@pytest.mark.asyncio
async def test_booking_the_same_time_again_is_ok_and_changes_nothing_but_the_booker(test_db):  # noqa: F811
    td_id = await _make()
    first = await _book(td_id)
    again = await _book(td_id, seen=first.version, user="u2", name="Bob")
    assert (again.ok, again.old, again.new) == (True, START, START)
    assert await _row(td_id) == (datetime(2026, 10, 28, 19, 30), "u2", "Bob", 2)


@pytest.mark.asyncio
async def test_losing_the_conditional_update_reports_changed_with_the_winner(test_db, monkeypatch):  # noqa: F811
    td_id = await _make()
    real = svc._current
    calls = []

    async def stale_first(session, team_draft_id):
        calls.append(1)
        if len(calls) == 1:
            return (None, 0, False)  # the read that preceded the other session's commit
        return await real(session, team_draft_id)

    await _book(td_id)  # the winner: version is now 1
    monkeypatch.setattr(svc, "_current", stale_first)
    loser = await _book(td_id, start=START + timedelta(hours=1), seen=0, user="u2", name="Bob")
    assert (loser.ok, loser.code, loser.new, loser.version) == (False, "changed", START, 1)
    assert (await _row(td_id))[1:] == ("u1", "Ann", 1)
