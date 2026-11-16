"""confirmround01 migration: the backfill that keeps existing this-week edits
counting as confirmed. Its logic is frozen in the migration file, so this
loads that module directly."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, text

_spec = importlib.util.spec_from_file_location(
    "confirmround01",
    Path(__file__).parent.parent / "alembic" / "versions" /
    "confirmround01_scheduler_round_confirmation.py")
confirmround01 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(confirmround01)
backfill_confirmations = confirmround01.backfill_confirmations

NOW = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)   # current week Nov 16, next Nov 23

DDL = [
    """CREATE TABLE player_day_override (
        user_id VARCHAR(64), local_date DATE, zone VARCHAR(64),
        cells BLOB, stored_utc DATETIME, PRIMARY KEY (user_id, local_date))""",
    """CREATE TABLE player_round_confirmation (
        user_id VARCHAR(64), round_monday DATE, source VARCHAR(16) NOT NULL,
        stored_utc DATETIME NOT NULL, PRIMARY KEY (user_id, round_monday))""",
    """CREATE TABLE player_scheduler_setting (
        user_id VARCHAR(64) PRIMARY KEY, trusts_usual BOOLEAN)""",
]


def _conn():
    conn = create_engine("sqlite://").connect()
    for stmt in DDL:
        conn.execute(text(stmt))
    return conn


def _override(conn, user, day, zone):
    conn.execute(text(
        "INSERT INTO player_day_override (user_id, local_date, zone, cells, stored_utc) "
        "VALUES (:u, :d, :z, :c, '2026-11-01 00:00:00')"),
        {"u": user, "d": day, "z": zone, "c": b"\x00" * 6})


def _rows(conn):
    return {(u, str(m), s) for u, m, s in conn.execute(text(
        "SELECT user_id, round_monday, source FROM player_round_confirmation"))}


def test_backfill_confirms_the_current_and_next_week_of_existing_overrides():
    conn = _conn()
    _override(conn, "u1", "2026-11-17", "America/Los_Angeles")
    _override(conn, "u2", "2026-11-24", "America/Los_Angeles")
    _override(conn, "u3", "2026-11-10", "America/Los_Angeles")
    _override(conn, "u4", "2026-11-30", "America/Los_Angeles")
    _override(conn, "u5", "2026-11-23", "Asia/Tokyo")
    _override(conn, "u6", "2026-11-16", "Asia/Tokyo")
    assert backfill_confirmations(conn, NOW) == 5
    assert _rows(conn) == {("u1", "2026-11-16", "backfill"), ("u2", "2026-11-23", "backfill"),
                           ("u5", "2026-11-16", "backfill"), ("u5", "2026-11-23", "backfill"),
                           ("u6", "2026-11-16", "backfill")}


def test_backfill_is_idempotent_and_keeps_existing_rows():
    conn = _conn()
    _override(conn, "u1", "2026-11-17", "America/Los_Angeles")
    conn.execute(text(
        "INSERT INTO player_round_confirmation VALUES ('u1', '2026-11-16', 'button', '2026-11-17 00:00:00')"))
    assert backfill_confirmations(conn, NOW) == 0
    assert _rows(conn) == {("u1", "2026-11-16", "button")}
    _override(conn, "u2", "2026-11-17", "America/Los_Angeles")
    assert backfill_confirmations(conn, NOW) == 1
    assert backfill_confirmations(conn, NOW) == 0


def test_backfill_with_no_overrides_inserts_nothing():
    conn = _conn()
    assert backfill_confirmations(conn, NOW) == 0
    assert _rows(conn) == set()


def test_backfill_skips_malformed_override_rows_and_keeps_the_rest():
    conn = _conn()
    _override(conn, "bad_zone", "2026-11-17", "Not/AZone")
    _override(conn, "null_zone", "2026-11-17", None)
    _override(conn, "bad_date", "not-a-date", "America/Los_Angeles")
    _override(conn, "u1", "2026-11-17", "America/Los_Angeles")
    assert backfill_confirmations(conn, NOW) == 1
    assert _rows(conn) == {("u1", "2026-11-16", "backfill")}
