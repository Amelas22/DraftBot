from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, update

from conftest import seed_session, seed_tournament_match, test_db  # noqa: F401
from database.db_session import db_session
from models.draft_session import DraftSession
from models.tournament import TournamentMatch
from services.match_booking import MatchStore, book_match, clear_match

UTC = timezone.utc
NOW = datetime(2026, 10, 26, 12, 10, tzinfo=UTC)
START = datetime(2026, 10, 28, 19, 30, tzinfo=UTC)


async def _make():
    async with db_session() as s:
        match = await seed_tournament_match(s)
        return match.id


async def _row(match_id):
    async with db_session() as s:
        m = await s.get(TournamentMatch, match_id)
        return m.booked_start_utc, m.booked_by, m.booked_by_name, m.booking_version


async def _book(match_id, start=START, seen=0, user="u1", name="Ann"):
    async with db_session() as s:
        change = await book_match(s, match_id, user, name, start, seen, NOW)
        await s.commit()
        return change


async def _clear(match_id, seen, user="u1", name="Ann"):
    async with db_session() as s:
        change = await clear_match(s, match_id, user, name, seen, NOW)
        await s.commit()
        return change


@pytest.mark.asyncio
async def test_book_stores_naive_utc_and_bumps_the_version(test_db):  # noqa: F811
    match_id = await _make()
    change = await _book(match_id)
    assert (change.ok, change.code, change.new, change.version) == (True, None, START, 1)
    assert await _row(match_id) == (datetime(2026, 10, 28, 19, 30), "u1", "Ann", 1)


@pytest.mark.asyncio
async def test_another_user_moves_the_booking(test_db):  # noqa: F811
    match_id = await _make()
    first = await _book(match_id)
    later = START + timedelta(hours=2)
    moved = await _book(match_id, start=later, seen=first.version, user="u2", name="Bob")
    assert (moved.ok, moved.old, moved.new, moved.version) == (True, START, later, 2)
    assert await _row(match_id) == (later.replace(tzinfo=None), "u2", "Bob", 2)


@pytest.mark.asyncio
async def test_clear_removes_the_booking(test_db):  # noqa: F811
    match_id = await _make()
    first = await _book(match_id)
    cleared = await _clear(match_id, first.version, user="u3", name="Cy")
    assert (cleared.ok, cleared.old, cleared.new, cleared.version) == (True, START, None, 2)
    assert await _row(match_id) == (None, None, None, 2)


@pytest.mark.asyncio
async def test_gone_when_there_is_no_such_match(test_db):  # noqa: F811
    assert (await _book(999)).code == "gone"
    assert (await _clear(999, 0)).code == "gone"


@pytest.mark.asyncio
async def test_started_once_a_result_is_recorded(test_db):  # noqa: F811
    match_id = await _make()
    async with db_session() as s:
        m = await s.get(TournamentMatch, match_id)
        m.team_a_wins = 2
        m.team_b_wins = 0
        await s.commit()
    assert (await _book(match_id)).code == "started"
    assert (await _clear(match_id, 0)).code == "started"
    assert (await _row(match_id))[3] == 0


async def _link_draft(match_id):
    await seed_session("d1")
    async with db_session() as s:
        await s.execute(
            update(DraftSession).where(DraftSession.session_id == "d1").values(tournament_match_id=match_id)
        )
        await s.commit()


@pytest.mark.asyncio
async def test_started_once_a_draft_is_linked_and_open_again_when_it_is_deleted(test_db):  # noqa: F811
    match_id = await _make()
    await _link_draft(match_id)
    assert (await _book(match_id)).code == "started"
    assert (await _row(match_id))[3] == 0
    async with db_session() as s:
        await s.execute(delete(DraftSession).where(DraftSession.session_id == "d1"))
        await s.commit()
    assert (await _book(match_id)).ok


@pytest.mark.asyncio
async def test_write_refuses_when_a_draft_appears_after_the_read(test_db):  # noqa: F811
    # The guards live in the UPDATE, not only in current().
    match_id = await _make()
    await _link_draft(match_id)
    async with db_session() as s:
        assert not await MatchStore(match_id).write(s, 0, {"booked_by": "u1"})


@pytest.mark.asyncio
async def test_two_sessions_on_one_version_exactly_one_wins(test_db):  # noqa: F811
    match_id = await _make()
    async with db_session() as s1, db_session() as s2:
        store = MatchStore(match_id)
        assert (await store.current(s1))[1] == 0
        assert (await store.current(s2))[1] == 0
        first = await book_match(s1, match_id, "u1", "Ann", START, 0, NOW)
        await s1.commit()
        second = await book_match(s2, match_id, "u2", "Bob", START + timedelta(hours=1), 0, NOW)
    assert first.ok
    assert (second.ok, second.code) == (False, "changed")
    assert (await _row(match_id))[1:] == ("u1", "Ann", 1)


@pytest.mark.asyncio
async def test_schedulable_match_names_its_guild_and_checks_it_only_when_asked(test_db):  # noqa: F811
    from services.match_booking import schedulable_match

    match_id = await _make()
    async with db_session() as s:
        trusted = await schedulable_match(s, None, match_id)
        assert trusted is not None
        assert (trusted.match.id, trusted.a.id) == (match_id, trusted.match.team_a_participant_id)
        assert await schedulable_match(s, trusted.guild_id, match_id) == trusted
        assert await schedulable_match(s, trusted.guild_id + "0", match_id) is None
