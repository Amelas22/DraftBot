"""Round and week arithmetic on the league's Pacific clock.

Round N's deadline is the Nth Sunday counting weekly from ``round1_deadline``,
passing over the calendar's break Sundays, so a break stretches the round it
falls in by a week and moves every later round back by one. Round N's view runs
from 00:00 PT on Round N-1's deadline Sunday (Round 1: the Sunday a week before
its own) to 24:00 PT on Round N's deadline Sunday.
"""

from collections.abc import Iterable, Mapping
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
    breaks: frozenset[date] = frozenset()


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
    return _span(last_day - timedelta(days=7), last_day, number)


def _span(first_day: date, last_day: date, number: int | None) -> Week:
    start = _midnight_utc(first_day)
    end = _midnight_utc(last_day + timedelta(days=1))
    cells = int((end - start) / timedelta(minutes=CELL_MINUTES))
    return Week(number, first_day, last_day, start, cells)


def deadline_on_or_after(now: datetime) -> date:
    today = now.astimezone(_TZ).date()
    return today + timedelta(days=(_SUNDAY - today.weekday()) % 7)


def _deadline(cal: Calendar, n: int) -> date:
    """Round n's deadline Sunday; n = 0 is the Sunday Round 1's view starts on."""
    if n == 0:
        return cal.round1_deadline - timedelta(days=7)
    day = cal.round1_deadline
    for _ in range(n - 1):
        day += timedelta(days=7)
        while day in cal.breaks:
            day += timedelta(days=7)
    return day


def round_week(cal: Calendar, n: int) -> Week:
    return _span(_deadline(cal, n - 1), _deadline(cal, n), n)


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


def league_monday(moment: datetime) -> date:
    """The Monday of the Pacific Mon-Sun week containing the aware instant."""
    day = moment.astimezone(_TZ).date()
    return day - timedelta(days=day.weekday())


def league_weeks_between(start_utc: datetime, end_utc: datetime) -> tuple[date, ...]:
    """Ascending Mondays of every league week overlapping [start_utc, end_utc)."""
    first = league_monday(start_utc)
    last = league_monday(end_utc - timedelta(microseconds=1))
    count = (last - first).days // 7 + 1
    return tuple(first + timedelta(days=7 * i) for i in range(max(count, 0)))


def league_weeks_of_cells(cells: Iterable[int], start_utc: datetime) -> tuple[date, ...]:
    """Ascending, unique league Mondays of the half-hours ``start_utc + 30 min * j``."""
    step = timedelta(minutes=CELL_MINUTES)
    return tuple(sorted({league_monday(start_utc + step * j) for j in cells}))


def league_week_bounds(monday: date) -> tuple[datetime, datetime]:
    """A league week's span: Monday 00:00 PT to the next Monday 00:00 PT, in UTC."""
    return _midnight_utc(monday), _midnight_utc(monday + timedelta(days=7))


def confirmable_weeks(
    painted: Iterable[date],
    first_day: date,
    last_day: date,
    zone: str,
    now_utc: datetime,
) -> frozenset[date]:
    """The painted league weeks a save may confirm.

    Weeks that have already ended (next Monday 00:00 PT <= now) are dropped
    first, before the overlap check, so a stale week is ignored rather than
    rejected. Any remaining week that does not overlap the days
    [00:00 first_day, 24:00 last_day) in ``zone`` raises ``ValueError``.
    """
    live = {w for w in painted if league_week_bounds(w)[1] > now_utc}
    zone_info = ZoneInfo(zone)
    span_start = datetime(first_day.year, first_day.month, first_day.day, tzinfo=zone_info)
    after = last_day + timedelta(days=1)
    span_end = datetime(after.year, after.month, after.day, tzinfo=zone_info)
    overlapping = set(league_weeks_between(span_start.astimezone(timezone.utc), span_end.astimezone(timezone.utc)))
    stray = live - overlapping
    if stray:
        raise ValueError(f"week {min(stray).isoformat()} is outside the saved days")
    return frozenset(live)


def parse_monday(text: str) -> date:
    """An ISO date that is a Monday, else ``ValueError``."""
    day = date.fromisoformat(text)
    if day.weekday() != 0:
        raise ValueError(f"{text} is not a Monday")
    return day


def confirmed_window_cells(
    free: frozenset[int],
    trusts_usual: bool,
    confirmed: frozenset[date],
    start_utc: datetime,
) -> frozenset[int]:
    """The free cells that count as confirmed: all when the person trusts their usual week, else those in a confirmed league week."""
    if trusts_usual:
        return free
    step = timedelta(minutes=CELL_MINUTES)
    return frozenset(j for j in free if league_monday(start_utc + step * j) in confirmed)
