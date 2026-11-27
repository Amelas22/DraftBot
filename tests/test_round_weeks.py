import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.round_weeks import (
    BadWeek,
    Calendar,
    Week,
    confirmable_weeks,
    confirmed_window_cells,
    current_round,
    deadline_on_or_after,
    horizon,
    league_monday,
    league_weeks_between,
    league_weeks_of_cells,
    parse_monday,
    pick_page,
    round_week,
    week_doc,
    week_ending,
)

FIXTURE = json.loads(
    (Path(__file__).parent.parent / "scheduler_page" / "fixtures" / "rounds.json").read_text()
)
LOTUS = Calendar(date(2026, 11, 8), 7)


def utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "")).replace(tzinfo=timezone.utc)


NOW = utc("2026-11-16T12:00")


def test_weeks_fixture_cases():
    for case in FIXTURE["weeks"]:
        w = week_ending(date.fromisoformat(case["last_day"]))
        assert w.start_utc == utc(case["start_utc"]), case["name"]
        assert w.cells == case["cells"], case["name"]


def test_deadlines_fixture_cases():
    for case in FIXTURE["deadlines"]:
        assert deadline_on_or_after(utc(case["now_utc"])) == date.fromisoformat(case["deadline"]), case["name"]


def test_round_week_counts_from_round1():
    assert round_week(LOTUS, 3) == Week(
        3, date(2026, 11, 15), date(2026, 11, 22), datetime(2026, 11, 15, 8, tzinfo=timezone.utc), 384
    )
    assert round_week(LOTUS, 3).end_utc == datetime(2026, 11, 23, 8, tzinfo=timezone.utc)


def test_current_round_turns_over_at_midnight_pacific():
    assert current_round(LOTUS, utc("2026-11-16T12:00")) == 3
    assert current_round(LOTUS, utc("2026-11-23T07:59")) == 3
    assert current_round(LOTUS, utc("2026-11-23T08:00")) == 4
    assert current_round(LOTUS, utc("2026-11-01T07:00")) == 1
    assert current_round(LOTUS, utc("2026-11-01T06:59")) == 1  # before Round 1's view starts
    assert current_round(LOTUS, utc("2026-12-21T08:00")) is None  # Round 7 (Dec 20) is over


def test_pick_page_with_a_calendar():
    p = pick_page(LOTUS, {}, NOW, default_round=3)
    assert (p.week.number, p.prev, p.next) == (3, {"round": 2}, {"round": 4})
    assert pick_page(LOTUS, {"round": "1"}, NOW, 3).prev is None
    assert pick_page(LOTUS, {"round": "7"}, NOW, 3).next is None
    for bad in ({"round": "0"}, {"round": "8"}, {"round": "x"}, {"week": "2026-11-15"}):
        with pytest.raises(BadWeek):
            pick_page(LOTUS, bad, NOW, 3)


def test_pick_page_without_a_calendar():
    p = pick_page(None, {}, NOW)
    assert (p.week.number, p.week.first_day, p.prev, p.next) == (
        None, date(2026, 11, 15), {"week": "2026-11-08"}, {"week": "2026-11-22"})
    assert pick_page(None, {"week": "2026-10-18"}, NOW).prev is None  # 4 weeks back
    assert pick_page(None, {"week": "2026-12-06"}, NOW).next is None  # 3 weeks ahead
    for bad in ({"week": "2026-10-11"}, {"week": "2026-12-13"}, {"week": "2026-11-16"},
                {"week": "nope"}, {"round": "1"}):
        with pytest.raises(BadWeek):
            pick_page(None, bad, NOW)


def test_horizon():
    assert horizon(LOTUS, NOW) == datetime(2026, 12, 21, 8, tzinfo=timezone.utc)
    assert horizon(None, NOW) == datetime(2026, 12, 14, 8, tzinfo=timezone.utc)
    assert horizon(LOTUS, utc("2026-12-21T08:00")) == horizon(None, utc("2026-12-21T08:00"))  # swiss over


def test_week_doc():
    assert week_doc(pick_page(LOTUS, {}, NOW, 3)) == {
        "number": 3, "first_day": "2026-11-15", "last_day": "2026-11-22",
        "prev": {"round": 2}, "next": {"round": 4}}


CONFIRMED = json.loads(
    (Path(__file__).parent.parent / "scheduler_page" / "fixtures" / "confirmed.json").read_text()
)
HALF_HOUR = timedelta(minutes=30)


def test_confirmed_fixture_mondays() -> None:
    for c in CONFIRMED["mondays"]:
        assert league_monday(utc(c["at_utc"])).isoformat() == c["monday"], c["name"]


def test_confirmed_fixture_between() -> None:
    for c in CONFIRMED["between"]:
        start = utc(c["start_utc"])
        got = league_weeks_between(start, start + c["cells"] * HALF_HOUR)
        assert [d.isoformat() for d in got] == c["weeks"], c["name"]


def test_confirmed_fixture_painted() -> None:
    for c in CONFIRMED["painted"]:
        got = league_weeks_of_cells(c["cells"], utc(c["start_utc"]))
        assert [d.isoformat() for d in got] == c["weeks"], c["name"]


def test_confirmable_weeks() -> None:
    now = utc("2026-10-22T12:00")
    ny = "America/New_York"
    assert confirmable_weeks(
        [date(2026, 10, 19), date(2026, 10, 26)], date(2026, 10, 22), date(2026, 10, 26), ny, now
    ) == {date(2026, 10, 19), date(2026, 10, 26)}
    # ended week: dropped
    assert confirmable_weeks([date(2026, 10, 12)], date(2026, 10, 22), date(2026, 10, 26), ny, now) == frozenset()
    # NY Oct 26 ends 21:00 PDT, so the week of Nov 2 does not overlap
    with pytest.raises(ValueError):
        confirmable_weeks([date(2026, 11, 2)], date(2026, 10, 22), date(2026, 10, 26), ny, now)


def test_confirmed_fixture_cells() -> None:
    for c in CONFIRMED["cells"]:
        got = confirmed_window_cells(
            frozenset(c["free"]),
            c["trusts_usual"],
            frozenset(date.fromisoformat(d) for d in c["confirmed_rounds"]),
            utc(c["start_utc"]),
        )
        assert sorted(got) == c["expected"], c["name"]


def test_parse_monday() -> None:
    assert parse_monday("2026-11-16") == date(2026, 11, 16)
    for bad in ("2026-11-17", "16/11/2026", "2026-02-30", ""):
        with pytest.raises(ValueError):
            parse_monday(bad)


THANKSGIVING = Calendar(date(2026, 11, 8), 7, frozenset({date(2026, 11, 29)}))


def test_a_break_sunday_stretches_its_round_to_the_next_sunday():
    r4 = round_week(THANKSGIVING, 4)
    assert (r4.first_day, r4.last_day) == (date(2026, 11, 22), date(2026, 12, 6))
    assert r4.start_utc == datetime(2026, 11, 22, 8, tzinfo=timezone.utc)
    assert r4.end_utc == datetime(2026, 12, 7, 8, tzinfo=timezone.utc)
    assert r4.cells == 15 * 48


def test_rounds_after_a_break_shift_back_a_week_and_rounds_before_it_stay():
    assert [round_week(THANKSGIVING, n).last_day for n in range(1, 8)] == [
        date(2026, 11, 8), date(2026, 11, 15), date(2026, 11, 22), date(2026, 12, 6),
        date(2026, 12, 13), date(2026, 12, 20), date(2026, 12, 27),
    ]
    assert round_week(THANKSGIVING, 5).first_day == date(2026, 12, 6)
    assert round_week(THANKSGIVING, 3) == round_week(LOTUS, 3)


def test_the_round_spanning_a_break_is_current_through_the_break_week():
    assert current_round(THANKSGIVING, utc("2026-11-27T15:00")) == 4
    assert current_round(THANKSGIVING, utc("2026-11-30T12:00")) == 4
    assert current_round(THANKSGIVING, utc("2026-12-07T07:59")) == 4
    assert current_round(THANKSGIVING, utc("2026-12-07T08:00")) == 5


def test_the_horizon_follows_the_shifted_last_round():
    assert horizon(THANKSGIVING, utc("2026-11-27T15:00")) == datetime(2026, 12, 28, 8, tzinfo=timezone.utc)
