from datetime import date, datetime, timezone

import pytest
from sqlalchemy import update

from conftest import seed_tournament_match, test_db  # noqa: F401
from database.db_session import db_session
from models.tournament import Tournament, TournamentRound
from services.booking import BookingSpan, rolling_span
from services.round_weeks import BadWeek
from services.scheduler_calendar import (
    guild_calendar,
    match_schedule,
    team_draft_schedule,
    this_week_horizon,
)

NOW = datetime(2026, 10, 26, 12, 10, tzinfo=timezone.utc)


def utc(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


async def _seed(deadline=None, status="active"):
    async with db_session() as s:
        match = await seed_tournament_match(s)
        mid = match.id
        await s.execute(update(Tournament).values(round1_deadline=deadline, status=status))
        await s.commit()
        return mid


@pytest.mark.asyncio
async def test_team_draft_without_a_calendar_opens_on_the_week_containing_now(test_db):
    async with db_session() as s:
        sch = await team_draft_schedule(s, "g1", {}, NOW)
    assert (sch.start_utc, sch.cells, sch.shape) == (utc("2026-10-25T07:00"), 386, "round")
    assert sch.page.week.number is None and sch.page.week.last_day == date(2026, 11, 1)
    assert sch.span == BookingSpan(utc("2026-10-26T12:00"), utc("2026-11-23T08:00"))


@pytest.mark.asyncio
async def test_team_draft_uses_the_running_calendar_tournament(test_db):
    await _seed(date(2026, 10, 18))
    async with db_session() as s:
        sch = await team_draft_schedule(s, "g1", {}, NOW)
    assert (sch.page.week.number, sch.page.next) == (3, None)
    assert sch.span.end_utc == utc("2026-11-02T08:00")


@pytest.mark.asyncio
async def test_newest_running_calendar_wins_and_registration_or_dateless_ones_are_ignored(test_db):
    await _seed(date(2026, 10, 18))
    async with db_session() as s:
        s.add_all([
            Tournament(guild_id="g1", name="New", total_rounds=5, status="active",
                       round1_deadline=date(2026, 10, 25), created_at=datetime(2030, 1, 1)),
            Tournament(guild_id="g1", name="Reg", total_rounds=9, status="registration",
                       round1_deadline=date(2026, 10, 1), created_at=datetime(2031, 1, 1)),
            Tournament(guild_id="g1", name="Dateless", total_rounds=9, status="active",
                       created_at=datetime(2032, 1, 1)),
            Tournament(guild_id="other", name="Else", total_rounds=9, status="active",
                       round1_deadline=date(2026, 10, 1), created_at=datetime(2033, 1, 1)),
        ])
        await s.commit()
        cal = await guild_calendar(s, "g1")
    assert (cal.round1_deadline, cal.total_rounds) == (date(2026, 10, 25), 5)


@pytest.mark.asyncio
async def test_team_draft_after_the_swiss_falls_back_to_unnumbered_weeks(test_db):
    await _seed(date(2026, 9, 6))
    async with db_session() as s:
        sch = await team_draft_schedule(s, "g1", {}, NOW)
    assert sch.page.week.number is None


@pytest.mark.asyncio
async def test_match_opens_on_its_own_round_and_books_only_inside_it(test_db):
    mid = await _seed(date(2026, 11, 1))
    async with db_session() as s:
        sch = await match_schedule(s, mid, {}, NOW)
        sch2 = await match_schedule(s, mid, {"round": "2"}, NOW)
    assert (sch.page.week.number, sch.start_utc, sch.cells) == (1, utc("2026-10-25T07:00"), 386)
    assert sch.span == BookingSpan(utc("2026-10-25T07:00"), utc("2026-11-02T08:00"))
    assert (sch2.page.week.number, sch2.span) == (2, sch.span)


@pytest.mark.asyncio
async def test_a_playoff_match_keeps_the_rolling_window(test_db):
    mid = await _seed(date(2026, 11, 1))
    async with db_session() as s:
        await s.execute(update(TournamentRound).values(stage="playoff"))
        await s.commit()
        sch = await match_schedule(s, mid, {"round": "2"}, NOW)
    assert (sch.shape, sch.cells, sch.page, sch.span) == ("rolling", 336, None, rolling_span(NOW))


@pytest.mark.asyncio
async def test_a_match_that_is_gone_keeps_the_rolling_window(test_db):
    async with db_session() as s:
        sch = await match_schedule(s, 999, {"round": "2"}, NOW)
    assert sch.shape == "rolling" and sch.page is None


@pytest.mark.asyncio
async def test_a_match_without_a_calendar_shows_weeks_and_books_seven_days(test_db):
    mid = await _seed(None)
    async with db_session() as s:
        sch = await match_schedule(s, mid, {}, NOW)
    assert sch.page.week.number is None
    assert sch.span == rolling_span(NOW)


@pytest.mark.asyncio
async def test_bad_round_raises_bad_week(test_db):
    mid = await _seed(date(2026, 11, 1))
    async with db_session() as s:
        with pytest.raises(BadWeek):
            await match_schedule(s, mid, {"round": "9"}, NOW)
        with pytest.raises(BadWeek):
            await team_draft_schedule(s, "g1", {"week": "2026-10-25"}, NOW)


@pytest.mark.asyncio
async def test_this_week_horizon_covers_both_the_calendar_and_the_unnumbered_weeks(test_db):
    async with db_session() as s:
        assert await this_week_horizon(s, "g1", NOW) == utc("2026-11-23T08:00")
    await _seed(date(2026, 11, 8))  # 3 rounds end Nov 22 -> shorter than the unnumbered horizon
    async with db_session() as s:
        assert await this_week_horizon(s, "g1", NOW) == utc("2026-11-23T08:00")
    async with db_session() as s:
        await s.execute(update(Tournament).values(total_rounds=9))
        await s.commit()
        assert await this_week_horizon(s, "g1", NOW) == utc("2027-01-04T08:00")


@pytest.mark.asyncio
async def test_the_calendar_carries_the_tournaments_break_sundays(test_db):
    await _seed(date(2026, 11, 8))
    async with db_session() as s:
        await s.execute(update(Tournament).values(round_breaks=["2026-11-29"], total_rounds=7))
        await s.commit()
        cal = await guild_calendar(s, "g1")
        sch = await team_draft_schedule(s, "g1", {}, utc("2026-11-27T15:00"))
    assert cal is not None and cal.breaks == frozenset({date(2026, 11, 29)})
    assert sch.page.week.number == 4
    assert (sch.page.week.first_day, sch.page.week.last_day) == (date(2026, 11, 22), date(2026, 12, 6))


@pytest.mark.asyncio
@pytest.mark.parametrize("stored, expected", [
    (["not a date", 5, None, "2026-11-29"], {date(2026, 11, 29)}),
    ("2026-11-29", set()),
    ({"2026-11-29": True}, set()),
])
async def test_unreadable_break_entries_are_ignored_rather_than_breaking_the_page(test_db, stored, expected):
    await _seed(date(2026, 11, 8))
    async with db_session() as s:
        await s.execute(update(Tournament).values(round_breaks=stored, total_rounds=7))
        await s.commit()
        cal = await guild_calendar(s, "g1")
    assert cal is not None and cal.breaks == frozenset(expected)
