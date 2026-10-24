"""Week bitmaps, the usual-week -> window conversion, and usual-week storage.

Python twin of scheduler_page/js/week.js; both reproduce
scheduler_page/fixtures/conversion.json exactly.
"""

import base64
import binascii
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import cache
from zoneinfo import ZoneInfo, available_timezones

from database.db_session import db_session
from models.scheduler import PlayerAvailability

CELLS: int = 336
CELL_MINUTES: int = 30
MAX_FILE_BYTES: int = 4096
_BYTES = CELLS // 8
_FORMAT = "draftbot.scheduler.usual_week"
_VERSION = 1


class InvalidWeek(ValueError):
    """A week bitmap or usual-week file failed validation."""


@dataclass(frozen=True)
class UsualWeek:
    zone: str
    cells: frozenset[int]


def naive_utc(moment: datetime) -> datetime:
    """The naive-UTC form the scheduler's *_utc columns store."""
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def iso_z(moment: datetime) -> str:
    """UTC ISO-8601 to the second with a ``Z``; a naive moment is taken as UTC (the *_utc columns)."""
    if moment.tzinfo is not None:
        moment = naive_utc(moment)
    return moment.isoformat(timespec="seconds") + "Z"


@cache
def _known_zones() -> frozenset[str]:
    # available_timezones() walks the tzdata tree; once per process is enough.
    return frozenset(available_timezones())


def cells_to_bytes(cells: Iterable[int]) -> bytes:
    data = bytearray(_BYTES)
    for i in cells:
        data[i // 8] |= 0x80 >> (i % 8)
    return bytes(data)


def cells_from_bytes(data: bytes) -> frozenset[int]:
    if len(data) != _BYTES:
        raise InvalidWeek(f"expected {_BYTES} bytes, got {len(data)}")
    return frozenset(i for i in range(CELLS) if data[i // 8] & (0x80 >> (i % 8)))


def decode_cells(b64: str) -> frozenset[int]:
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidWeek("cells is not valid base64") from exc
    return cells_from_bytes(data)


def window_cells(
    zone: str, usual: frozenset[int], start_utc: datetime
) -> frozenset[int]:
    """Window cell j is free iff the local time of start + 30 min * j is painted."""
    tz = ZoneInfo(zone)
    out: set[int] = set()
    for j in range(CELLS):
        local = (start_utc + timedelta(minutes=CELL_MINUTES * j)).astimezone(tz)
        cell = local.weekday() * 48 + (local.hour * 60 + local.minute) // CELL_MINUTES
        if cell in usual:
            out.add(j)
    return frozenset(out)


def merge_blocks(cells: Iterable[int]) -> list[tuple[int, int]]:
    """Half-open [start, end) runs of consecutive cells, sorted."""
    blocks: list[tuple[int, int]] = []
    for i in sorted(cells):
        if blocks and blocks[-1][1] == i:
            blocks[-1] = (blocks[-1][0], i + 1)
        else:
            blocks.append((i, i + 1))
    return blocks


def parse_usual_week(raw: bytes) -> UsualWeek:
    if len(raw) > MAX_FILE_BYTES:
        raise InvalidWeek("file too large")
    try:
        doc = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise InvalidWeek("not JSON") from exc
    if not isinstance(doc, dict):
        raise InvalidWeek("not a JSON object")
    if doc.get("format") != _FORMAT:
        raise InvalidWeek("unknown format")
    version = doc.get("version")
    if type(version) is not int or version != _VERSION:
        raise InvalidWeek("unsupported version")
    zone = doc.get("zone")
    if not isinstance(zone, str) or zone not in _known_zones():
        raise InvalidWeek("unknown zone")
    cells = doc.get("cells")
    if not isinstance(cells, str):
        raise InvalidWeek("cells missing")
    return UsualWeek(zone=zone, cells=decode_cells(cells))


def next_week_lines(week: UsualWeek, now_utc: datetime) -> list[str]:
    """One line per local day with free time over the next 7 days."""
    now = now_utc.astimezone(timezone.utc)
    start = now.replace(
        minute=now.minute - now.minute % CELL_MINUTES, second=0, microsecond=0
    )
    tz = ZoneInfo(week.zone)
    by_day: dict[date, list[str]] = {}
    day_start: dict[date, int] = {}
    for first, end in merge_blocks(window_cells(week.zone, week.cells, start)):
        begin = start + timedelta(minutes=CELL_MINUTES * first)
        finish = start + timedelta(minutes=CELL_MINUTES * end)
        day = begin.astimezone(tz).date()
        day_start.setdefault(day, int(begin.timestamp()))
        by_day.setdefault(day, []).append(
            f"<t:{int(begin.timestamp())}:t>–<t:{int(finish.timestamp())}:t>"
        )
    # The leading date timestamp is the day's first block, so Discord renders
    # the day in the viewer's zone, consistent with the times after it.
    return [f"<t:{day_start[day]}:D>: " + " · ".join(by_day[day]) for day in sorted(by_day)]


def usual_week_doc(week: UsualWeek) -> dict[str, object]:
    """The ``draftbot.scheduler.usual_week`` v1 document for a week."""
    return {
        "format": _FORMAT,
        "version": _VERSION,
        "zone": week.zone,
        "cells": base64.b64encode(cells_to_bytes(week.cells)).decode("ascii"),
    }


async def load_usual_week(user_id: str) -> UsualWeek | None:
    """The stored usual week for a player, or None if none was saved."""
    async with db_session() as session:
        row = await session.get(PlayerAvailability, user_id)
        if row is None:
            return None
        return UsualWeek(zone=str(row.zone), cells=cells_from_bytes(bytes(row.cells)))


async def save_usual_week(user_id: str, week: UsualWeek, now_utc: datetime) -> None:
    """Store a player's usual week, replacing any earlier one."""
    async with db_session() as session:
        row = await session.get(PlayerAvailability, user_id)
        if row is None:
            row = PlayerAvailability(user_id=user_id)
            session.add(row)
        row.zone = week.zone
        row.cells = cells_to_bytes(week.cells)
        row.version = _VERSION
        row.stored_utc = naive_utc(now_utc)
