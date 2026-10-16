from datetime import datetime

import pytest

from cogs.donation_day_commands import new_event

NOW = datetime(2026, 10, 16, 12, 0)


def test_a_new_event_starts_now_unless_told_otherwise():
    event = new_event("Donation Day", 500, None, NOW)

    assert event == {"name": "Donation Day", "goal_usd": 500,
                     "starts_at": "2026-10-16T12:00", "page_key": "donation-day",
                     "active": True}


def test_an_explicit_start_is_parsed():
    assert new_event("DD", 100, "2026-10-16 09:30", NOW)["starts_at"] == "2026-10-16T09:30"


def test_a_bad_start_is_refused():
    with pytest.raises(ValueError):
        new_event("DD", 100, "friday", NOW)


def test_a_goal_must_be_positive():
    with pytest.raises(ValueError):
        new_event("DD", 0, None, NOW)
