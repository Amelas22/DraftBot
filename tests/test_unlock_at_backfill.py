"""
Unit test for the unlock_at backfill SQL in migration logcapr0recon.

Context: migration logcapr0recon_add_unlock_team_logs.py adds a nullable
`unlock_at` column to draft_sessions that the new reconciler uses to decide
when to publish the public draft-log embed. The old in-memory
`schedule_publish` timer that used to drive publication was deleted, so any
draft that was captured-but-not-yet-published at deploy time (has
`logs_captured_at`, `data_received = 0`, `unlock_at IS NULL`) would never get
published without a backfill.

This test exercises the exact backfill UPDATE statement (copy/pasted from the
migration's upgrade()) against a scratch sqlite3 database with a minimal
draft_sessions-like table, and asserts the row-selection semantics:
  (a) a captured-but-unpublished row captured 1 hour ago -> unlock_at gets set
  (b) a captured-but-unpublished row captured 5 days ago -> untouched (outside
      the 1-day bound)
  (c) an already-published row (data_received=1) -> untouched
"""
import sqlite3
from datetime import datetime, timedelta


BACKFILL_SQL = """
    UPDATE draft_sessions
    SET unlock_at = datetime(logs_captured_at, '+180 minutes')
    WHERE logs_captured_at IS NOT NULL
      AND unlock_at IS NULL
      AND (data_received = 0 OR data_received IS NULL)
      AND logs_captured_at >= datetime('now', '-1 day')
"""


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE draft_sessions (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            logs_captured_at TEXT,
            data_received INTEGER,
            unlock_at TEXT
        )
        """
    )
    return conn


def _iso(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def test_backfill_sets_recent_captured_unpublished_row_only():
    conn = _make_db()
    now = datetime.utcnow()

    recent_captured_unpublished = _iso(now - timedelta(hours=1))
    stale_captured_unpublished = _iso(now - timedelta(days=5))
    recent_published = _iso(now - timedelta(hours=2))

    conn.execute(
        "INSERT INTO draft_sessions (session_id, logs_captured_at, data_received, unlock_at) VALUES (?, ?, ?, ?)",
        ("recent-unpublished", recent_captured_unpublished, 0, None),
    )
    conn.execute(
        "INSERT INTO draft_sessions (session_id, logs_captured_at, data_received, unlock_at) VALUES (?, ?, ?, ?)",
        ("stale-unpublished", stale_captured_unpublished, 0, None),
    )
    conn.execute(
        "INSERT INTO draft_sessions (session_id, logs_captured_at, data_received, unlock_at) VALUES (?, ?, ?, ?)",
        ("recent-published", recent_published, 1, None),
    )
    conn.commit()

    conn.execute(BACKFILL_SQL)
    conn.commit()

    rows = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT session_id, unlock_at FROM draft_sessions"
        ).fetchall()
    }

    # (a) recent captured-but-unpublished row gets unlock_at = logs_captured_at + 180 min
    expected_unlock_at = conn.execute(
        "SELECT datetime(?, '+180 minutes')", (recent_captured_unpublished,)
    ).fetchone()[0]
    assert rows["recent-unpublished"] == expected_unlock_at

    # (b) stale captured-but-unpublished row (>1 day old) is untouched
    assert rows["stale-unpublished"] is None

    # (c) already-published row is untouched
    assert rows["recent-published"] is None

    conn.close()


def test_backfill_would_wrongly_resurrect_stale_row_without_the_day_bound():
    """Guards the '-1 day' bound: removing it would also backfill ancient rows."""
    conn = _make_db()
    now = datetime.utcnow()
    stale_captured_unpublished = _iso(now - timedelta(days=5))

    conn.execute(
        "INSERT INTO draft_sessions (session_id, logs_captured_at, data_received, unlock_at) VALUES (?, ?, ?, ?)",
        ("stale-unpublished", stale_captured_unpublished, 0, None),
    )
    conn.commit()

    sql_without_day_bound = BACKFILL_SQL.replace(
        "AND logs_captured_at >= datetime('now', '-1 day')", ""
    )
    conn.execute(sql_without_day_bound)
    conn.commit()

    unlock_at = conn.execute(
        "SELECT unlock_at FROM draft_sessions WHERE session_id = 'stale-unpublished'"
    ).fetchone()[0]
    # Without the bound, the stale row would incorrectly get an unlock_at set.
    assert unlock_at is not None

    conn.close()


def test_backfill_would_wrongly_republish_already_published_row_without_guard():
    """Guards the data_received guard: removing it would re-touch published rows."""
    conn = _make_db()
    now = datetime.utcnow()
    recent_published = _iso(now - timedelta(hours=2))

    conn.execute(
        "INSERT INTO draft_sessions (session_id, logs_captured_at, data_received, unlock_at) VALUES (?, ?, ?, ?)",
        ("recent-published", recent_published, 1, None),
    )
    conn.commit()

    sql_without_data_received_guard = BACKFILL_SQL.replace(
        "AND (data_received = 0 OR data_received IS NULL)", ""
    )
    conn.execute(sql_without_data_received_guard)
    conn.commit()

    unlock_at = conn.execute(
        "SELECT unlock_at FROM draft_sessions WHERE session_id = 'recent-published'"
    ).fetchone()[0]
    # Without the guard, the already-published row would incorrectly get unlock_at set.
    assert unlock_at is not None

    conn.close()
