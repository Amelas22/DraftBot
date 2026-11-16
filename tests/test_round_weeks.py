import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from services.round_weeks import (
    BadWeek,
    Calendar,
    Week,
    current_round,
    deadline_on_or_after,
    horizon,
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
