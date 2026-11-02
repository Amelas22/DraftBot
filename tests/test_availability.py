import base64
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from services.availability import (
    DayOverride,
    InvalidWeek,
    CELLS,
    UsualWeek,
    clear_day_overrides,
    decode_cells,
    cells_to_bytes,
    day_cells_from_bytes,
    day_cells_to_bytes,
    effective_window_cells,
    iso_z,
    load_day_overrides,
    load_usual_week,
    replace_day_overrides,
    save_usual_week,
    usual_week_doc,
    next_week_lines,
    parse_this_week,
    this_week_doc,
    parse_usual_week,
    window_cells,
)

FIXTURES = Path("scheduler_page/fixtures")


def _b64(cells: set[int]) -> str:
    return base64.b64encode(cells_to_bytes(cells)).decode("ascii")


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def test_conversion_fixture_cases() -> None:
    data = json.loads((FIXTURES / "conversion.json").read_text())
    assert data["cases"]
    for case in data["cases"]:
        got = window_cells(
            case["zone"],
            decode_cells(case["usual_cells"]),
            _utc(case["window_start_utc"]),
        )
        assert got == decode_cells(case["expected_window_cells"]), case["name"]


def test_override_fixture_cases() -> None:
    data = json.loads((FIXTURES / "override.json").read_text())
    assert data["cases"]
    for case in data["cases"]:
        usual = case["usual"]
        week = UsualWeek(usual["zone"], decode_cells(usual["cells"])) if usual else None
        overrides = [
            DayOverride(
                o["zone"],
                date.fromisoformat(o["date"]),
                day_cells_from_bytes(base64.b64decode(o["cells"])),
            )
            for o in case["overrides"]
        ]
        got = effective_window_cells(week, overrides, _utc(case["window_start_utc"]))
        assert got == decode_cells(case["expected_window_cells"]), case["name"]


def test_day_cells_round_trip() -> None:
    assert day_cells_from_bytes(day_cells_to_bytes({0, 7, 47})) == {0, 7, 47}
    assert day_cells_to_bytes({0})[0] == 0x80
    assert len(day_cells_to_bytes(set())) == 6
    with pytest.raises(InvalidWeek):
        day_cells_from_bytes(b"\x00" * 5)


def test_round_trip_and_bit_order() -> None:
    assert decode_cells(_b64({0, 9, 335})) == {0, 9, 335}
    assert cells_to_bytes({0})[0] == 0x80


def test_parse_accepts_the_fixture() -> None:
    week = parse_usual_week((FIXTURES / "usual-week.json").read_bytes())
    assert week.zone == "America/New_York"
    assert len(week.cells) == 52


def _doc(**over: object) -> bytes:
    d: dict[str, object] = {
        "format": "draftbot.scheduler.usual_week",
        "version": 1,
        "zone": "UTC",
        "cells": _b64({1}),
    }
    d.update(over)
    return json.dumps(d).encode()


@pytest.mark.parametrize(
    "raw",
    [
        b" " * 4097,
        b"[]",
        b"[" * 4000,
        _doc(version=1.0),
        _doc(format="other"),
        _doc(version=2),
        _doc(zone="Mars/Olympus"),
        _doc(cells=base64.b64encode(bytes(41)).decode()),
        _doc(cells="!!!not base64!!!"),
    ],
)
def test_parse_refuses(raw: bytes) -> None:
    with pytest.raises(InvalidWeek):
        parse_usual_week(raw)


def test_empty_week_is_valid_and_has_no_lines() -> None:
    now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
    assert next_week_lines(UsualWeek("UTC", frozenset()), now) == []


def test_next_week_lines_merge_blocks() -> None:
    tue = 48
    cells = frozenset(
        list(range(tue + 38, tue + 42)) + [tue + 44]  # 19:00-21:00, 22:00-22:30
    )
    now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
    lines = next_week_lines(UsualWeek("UTC", cells), now)
    assert len(lines) == 1
    first = int(datetime(2026, 10, 20, 19, 0, tzinfo=timezone.utc).timestamp())
    start = int(datetime(2026, 10, 20, 21, 0, tzinfo=timezone.utc).timestamp())
    assert lines[0].startswith(f"<t:{first}:D>: ")
    assert lines[0] == (
        f"<t:{first}:D>: <t:{first}:t>–<t:{start}:t> · "
        f"<t:{first + 3 * 3600}:t>–<t:{first + 3 * 3600 + 1800}:t>"
    )


def test_worst_case_line_fits_a_discord_field() -> None:
    # Clocks go back in New York on 2026-11-01 (25 local hours); every other
    # cell painted so each cell is its own block.
    cells = frozenset(range(0, CELLS, 2))
    now = datetime(2026, 10, 31, 4, 0, tzinfo=timezone.utc)
    lines = next_week_lines(UsualWeek("America/New_York", cells), now)
    assert max(len(line) for line in lines) <= 1024


NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_save_then_load_round_trips(test_db) -> None:
    assert await load_usual_week("1") is None
    week = UsualWeek("America/New_York", frozenset({0, 5, 47, 335}))
    await save_usual_week("1", week, NOW)
    assert await load_usual_week("1") == week
    newer = UsualWeek("UTC", frozenset({7}))
    await save_usual_week("1", newer, NOW)
    assert await load_usual_week("1") == newer
    assert await load_usual_week("2") is None


def test_usual_week_doc_matches_parse() -> None:
    w = UsualWeek("Europe/Paris", frozenset({1, 2, 3, 300}))
    doc = usual_week_doc(w)
    assert (doc["format"], doc["version"], doc["zone"]) == (
        "draftbot.scheduler.usual_week", 1, "Europe/Paris")
    assert parse_usual_week(json.dumps(usual_week_doc(w)).encode()) == w


def test_iso_z_reads_naive_as_utc_and_converts_aware():
    from zoneinfo import ZoneInfo

    assert iso_z(datetime(2026, 10, 22, 12, 0, 5, 123456)) == "2026-10-22T12:00:05Z"
    assert iso_z(datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)) == "2026-10-22T12:00:00Z"
    assert iso_z(datetime(2026, 10, 22, 8, 0, tzinfo=ZoneInfo("America/New_York"))) == "2026-10-22T12:00:00Z"


# --- dated overrides ---------------------------------------------------------

NY = "America/New_York"
TODAY = date(2026, 10, 22)  # NOW is Oct 22 08:00 in New York


def _day(d: date, cells: set[int], zone: str = NY) -> DayOverride:
    return DayOverride(zone, d, frozenset(cells))


def _usual(*, weekday: int, cells: set[int]) -> UsualWeek:
    return UsualWeek(NY, frozenset(weekday * 48 + c for c in cells))


@pytest.mark.asyncio
async def test_override_equal_to_usual_stores_nothing(test_db) -> None:
    await save_usual_week("1", _usual(weekday=TODAY.weekday(), cells={20, 21}), NOW)
    stored = await replace_day_overrides("1", [_day(TODAY, {20, 21})], False, NOW)
    assert stored == []
    assert await load_day_overrides("1", NOW) == []


@pytest.mark.asyncio
async def test_changed_day_is_stored_and_others_are_not(test_db) -> None:
    usual = _usual(weekday=TODAY.weekday(), cells={20, 21})
    await save_usual_week("1", usual, NOW)
    other = date(2026, 10, 23)
    changed = _day(TODAY, {30})
    same = _day(other, set())
    stored = await replace_day_overrides("1", [changed, same], False, NOW)
    assert stored == [changed]
    assert await load_day_overrides("1", NOW) == [changed]


@pytest.mark.asyncio
async def test_usual_edit_shows_through_untouched_days(test_db) -> None:
    thu, fri = TODAY, date(2026, 10, 23)
    both = {thu.weekday() * 48 + 20, fri.weekday() * 48 + 20}
    await save_usual_week("1", UsualWeek(NY, frozenset(both)), NOW)
    await replace_day_overrides("1", [_day(thu, {30})], False, NOW)
    new_usual = UsualWeek(NY, frozenset({thu.weekday() * 48 + 22, fri.weekday() * 48 + 22}))
    await save_usual_week("1", new_usual, NOW)
    start = datetime(2026, 10, 22, 4, 0, tzinfo=timezone.utc)  # local midnight Oct 22 (EDT)
    got = effective_window_cells(
        await load_usual_week("1"), await load_day_overrides("1", NOW), start)
    # Thursday follows its override (cell 30); Friday follows the new usual (cell 22).
    assert got == frozenset({30, 48 + 22})


@pytest.mark.asyncio
async def test_replace_drops_rows_in_another_zone(test_db) -> None:
    await replace_day_overrides("1", [_day(TODAY, {1}, "Europe/London")], False, NOW)
    assert [o.zone for o in await load_day_overrides("1", NOW)] == ["Europe/London"]
    await replace_day_overrides("1", [_day(TODAY, {2})], False, NOW)
    assert [(o.zone, o.cells) for o in await load_day_overrides("1", NOW)] == [
        (NY, frozenset({2}))]


@pytest.mark.asyncio
async def test_dead_rows_are_not_loaded(test_db) -> None:
    old = _day(date(2026, 10, 20), {4})
    live = _day(date(2026, 10, 21), {5})  # yesterday is still live
    await replace_day_overrides("1", [old, live], False, NOW)
    assert await load_day_overrides("1", NOW) == [live]


@pytest.mark.asyncio
async def test_also_usual_writes_usual_week_when_none(test_db) -> None:
    stored = await replace_day_overrides("1", [_day(TODAY, {8, 9})], True, NOW)
    assert stored == []
    assert await load_usual_week("1") == _usual(weekday=TODAY.weekday(), cells={8, 9})
    assert await load_day_overrides("1", NOW) == []


@pytest.mark.asyncio
async def test_also_usual_ignored_when_usual_exists(test_db) -> None:
    usual = _usual(weekday=0, cells={3})
    await save_usual_week("1", usual, NOW)
    stored = await replace_day_overrides("1", [_day(TODAY, {8, 9})], True, NOW)
    assert len(stored) == 1
    assert await load_usual_week("1") == usual


@pytest.mark.asyncio
async def test_clear_day_overrides(test_db) -> None:
    await replace_day_overrides("1", [_day(TODAY, {8})], False, NOW)
    await clear_day_overrides("1")
    assert await load_day_overrides("1", NOW) == []


TW_NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def _day_b64(cells: set[int]) -> str:
    return base64.b64encode(day_cells_to_bytes(cells)).decode("ascii")


def _tw(**over: object) -> bytes:
    d: dict[str, object] = {
        "format": "draftbot.scheduler.this_week",
        "version": 1,
        "zone": "UTC",
        "also_usual": False,
        "days": [{"date": "2026-10-22", "cells": _day_b64({1})}],
    }
    d.update(over)
    return json.dumps(d).encode()


def _days(*dates: str) -> list[dict[str, str]]:
    return [{"date": d, "cells": _day_b64({1})} for d in dates]


@pytest.mark.parametrize(
    "raw",
    [
        b" " * 4097,
        b"[]",
        b"[" * 4000,
        _tw(version=1.0),
        _tw(version=2),
        _tw(format="other"),
        _tw(zone="Mars/Olympus"),
        _tw(zone=None),
        _tw(also_usual="yes"),
        _tw(days="nope"),
        _tw(days=["2026-10-22"]),
        _tw(days=_days("2026-10-21", "2026-10-21")),
        _tw(days=_days("2026-10-20")),
        _tw(days=_days("2026-10-30")),
        _tw(days=_days("not-a-date")),
        _tw(days=_days("20261022")),
        _tw(days=[{"date": "2026-10-22", "cells": base64.b64encode(bytes(7)).decode()}]),
        _tw(days=[{"date": "2026-10-22", "cells": "!!!not base64!!!"}]),
        _tw(days=_days(*[f"2026-10-{d}" for d in range(21, 31)])),
    ],
)
def test_parse_this_week_refuses(raw: bytes) -> None:
    with pytest.raises(InvalidWeek):
        parse_this_week(raw, TW_NOW)


def test_parse_this_week_accepts_window_edges_in_the_zone() -> None:
    week = parse_this_week(_tw(zone="Asia/Tokyo", also_usual=True, days=_days("2026-10-21", "2026-10-29")), TW_NOW)
    assert week.zone == "Asia/Tokyo" and week.also_usual
    assert [d.local_date for d in week.days] == [date(2026, 10, 21), date(2026, 10, 29)]
    assert week.days[0].cells == frozenset({1})
    assert len(parse_this_week(_tw(days=_days(*[f"2026-10-{d}" for d in range(21, 30)])), TW_NOW).days) == 9


def test_this_week_doc_shape() -> None:
    assert this_week_doc([]) == {
        "format": "draftbot.scheduler.this_week", "version": 1, "zone": None, "days": [],
    }
    doc = this_week_doc([DayOverride("UTC", date(2026, 10, 22), frozenset({1}))])
    assert doc["zone"] == "UTC"
    assert doc["days"] == [{"date": "2026-10-22", "cells": _day_b64({1})}]
