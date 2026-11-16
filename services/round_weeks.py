"""Round and week arithmetic on the league's Pacific clock.

Round N's deadline is ``round1_deadline + 7*(N-1)`` days (a Sunday). Round N's
view runs from 00:00 PT on Round N-1's deadline Sunday to 24:00 PT on Round
N's deadline Sunday.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from services.availability import CELL_MINUTES

LEAGUE_ZONE: str = "America/Los_Angeles"
WEEKS_BACK: int = 4
WEEKS_AHEAD: int = 3

_TZ = ZoneInfo(LEAGUE_ZONE)
_SUNDAY = 6


class BadWeek(ValueError):
    """The requested round or week is not one the page can show."""


@dataclass(frozen=True)
class Calendar:
    round1_deadline: date
    total_rounds: int


@dataclass(frozen=True)
class Week:
    number: int | None
    first_day: date
    last_day: date
    start_utc: datetime
    cells: int

    @property
    def end_utc(self) -> datetime:
        return self.start_utc + timedelta(minutes=CELL_MINUTES * self.cells)


@dataclass(frozen=True)
class WeekPage:
    week: Week
    prev: dict[str, int | str] | None
    next: dict[str, int | str] | None


def _midnight_utc(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=_TZ).astimezone(timezone.utc)


def week_ending(last_day: date, number: int | None = None) -> Week:
    first_day = last_day - timedelta(days=7)
    start = _midnight_utc(first_day)
    end = _midnight_utc(last_day + timedelta(days=1))
    cells = int((end - start) / timedelta(minutes=CELL_MINUTES))
    return Week(number, first_day, last_day, start, cells)


def deadline_on_or_after(now: datetime) -> date:
    today = now.astimezone(_TZ).date()
    return today + timedelta(days=(_SUNDAY - today.weekday()) % 7)


def _deadline(cal: Calendar, n: int) -> date:
    return cal.round1_deadline + timedelta(days=7 * (n - 1))


def round_week(cal: Calendar, n: int) -> Week:
    return week_ending(_deadline(cal, n), n)


def current_round(cal: Calendar, now: datetime) -> int | None:
    for n in range(1, cal.total_rounds + 1):
        if now < _midnight_utc(_deadline(cal, n) + timedelta(days=1)):
            return n
    return None


def _round_page(cal: Calendar, n: int) -> WeekPage:
    return WeekPage(
        round_week(cal, n),
        {"round": n - 1} if n > 1 else None,
        {"round": n + 1} if n < cal.total_rounds else None,
    )


def _week_page(last_day: date, d0: date) -> WeekPage:
    lo = d0 - timedelta(days=7 * WEEKS_BACK)
    hi = d0 + timedelta(days=7 * WEEKS_AHEAD)
    return WeekPage(
        week_ending(last_day),
        {"week": (last_day - timedelta(days=14)).isoformat()} if last_day > lo else None,
        {"week": last_day.isoformat()} if last_day < hi else None,
    )


def pick_page(
    cal: Calendar | None,
    query: Mapping[str, str],
    now: datetime,
    default_round: int | None = None,
) -> WeekPage:
    if cal is not None:
        if "week" in query:
            raise BadWeek("week")
        raw = query.get("round")
        if raw is None:
            n = default_round if default_round is not None else (current_round(cal, now) or cal.total_rounds)
        elif raw.isascii() and raw.isdigit():
            n = int(raw)
        else:
            raise BadWeek(raw)
        if not 1 <= n <= cal.total_rounds:
            raise BadWeek(str(n))
        return _round_page(cal, n)

    if "round" in query:
        raise BadWeek("round")
    d0 = deadline_on_or_after(now)
    raw = query.get("week")
    if raw is None:
        return _week_page(d0, d0)
    try:
        first_day = date.fromisoformat(raw)
    except ValueError:
        raise BadWeek(raw) from None
    if first_day.weekday() != _SUNDAY:
        raise BadWeek(raw)
    last_day = first_day + timedelta(days=7)
    if not d0 - timedelta(days=7 * WEEKS_BACK) <= last_day <= d0 + timedelta(days=7 * WEEKS_AHEAD):
        raise BadWeek(raw)
    return _week_page(last_day, d0)


def horizon(cal: Calendar | None, now: datetime) -> datetime:
    if cal is not None and current_round(cal, now) is not None:
        return round_week(cal, cal.total_rounds).end_utc
    return week_ending(deadline_on_or_after(now) + timedelta(days=7 * WEEKS_AHEAD)).end_utc


def week_doc(page: WeekPage) -> dict[str, object]:
    w = page.week
    return {
        "number": w.number,
        "first_day": w.first_day.isoformat(),
        "last_day": w.last_day.isoformat(),
        "prev": page.prev,
        "next": page.next,
    }
