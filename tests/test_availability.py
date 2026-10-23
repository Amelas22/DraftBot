import base64
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.availability import (
    InvalidWeek,
    CELLS,
    UsualWeek,
    decode_cells,
    cells_to_bytes,
    load_usual_week,
    save_usual_week,
    usual_week_doc,
    next_week_lines,
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
