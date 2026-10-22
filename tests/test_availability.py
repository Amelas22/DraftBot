import base64
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.availability import (
    InvalidWeek,
    UsualWeek,
    decode_cells,
    encode_cells,
    next_week_lines,
    parse_usual_week,
    window_cells,
)

FIXTURES = Path("scheduler_page/fixtures")


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
    assert decode_cells(encode_cells({0, 9, 335})) == {0, 9, 335}
    assert base64.b64decode(encode_cells({0}))[0] == 0x80


def test_parse_accepts_the_fixture() -> None:
    week = parse_usual_week((FIXTURES / "usual-week.json").read_bytes())
    assert week.zone == "America/New_York"
    assert len(week.cells) == 52


def _doc(**over: object) -> bytes:
    d: dict[str, object] = {
        "format": "draftbot.scheduler.usual_week",
        "version": 1,
        "zone": "UTC",
        "cells": encode_cells({1}),
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
    assert lines[0] == (
        f"<t:{first}:t>–<t:{start}:t> · "
        f"<t:{first + 3 * 3600}:t>–<t:{first + 3 * 3600 + 1800}:t>"
    )
