"""dispnamefill0 migration: complete sign_up_history with provenance-marked
synthetic events, then backfill display names per (guild, player) from
deterministic, target-guild-first evidence.

The data logic is frozen inside the migration file (not shared with live
code), so these tests load that module directly.
"""
import importlib.util
from pathlib import Path

from sqlalchemy import create_engine, text

_spec = importlib.util.spec_from_file_location(
    "dispnamefill0",
    Path(__file__).parent.parent / "alembic" / "versions" /
    "dispnamefill0_backfill_display_names.py")
dispnamefill0 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dispnamefill0)

DDL = [
    """CREATE TABLE player_stats (
        player_id TEXT, guild_id TEXT, display_name TEXT,
        games_won INTEGER DEFAULT 0, games_lost INTEGER DEFAULT 0,
        PRIMARY KEY (player_id, guild_id))""",
    """CREATE TABLE draft_sessions (
        session_id TEXT, guild_id TEXT, session_type TEXT,
        draft_start_time TEXT, sign_ups TEXT)""",
    """CREATE TABLE sign_up_history (
        id TEXT PRIMARY KEY, session_id TEXT, user_id TEXT,
        user_display_name TEXT, action TEXT, timestamp TEXT, guild_id TEXT)""",
]


def _conn():
    engine = create_engine("sqlite://")
    conn = engine.connect()
    for stmt in DDL:
        conn.execute(text(stmt))
    return conn


def _stats_row(conn, pid, guild, name=None, games=0):
    conn.execute(text(
        "INSERT INTO player_stats (player_id, guild_id, display_name, games_won, games_lost) "
        "VALUES (:p, :g, :n, :w, 0)"), {"p": pid, "g": guild, "n": name, "w": games})


def test_signup_backfill_marks_synthetic_and_is_idempotent():
    conn = _conn()
    conn.execute(text(
        "INSERT INTO draft_sessions (session_id, guild_id, session_type, draft_start_time, sign_ups) "
        "VALUES ('s1', 'g', 'staked', '2025-01-01', '{\"1\": \"Alpha\"}')"))
    conn.execute(text(  # session with real roster events: untouched
        "INSERT INTO draft_sessions (session_id, guild_id, session_type, draft_start_time, sign_ups) "
        "VALUES ('s2', 'g', 'staked', '2026-01-01', '{\"3\": \"Gamma\"}')"))
    conn.execute(text(
        "INSERT INTO sign_up_history (id, session_id, user_id, user_display_name, action, timestamp, guild_id) "
        "VALUES ('e1', 's2', '3', 'RealGamma', 'join', '2026-01-01 10:00:00', 'g')"))
    conn.execute(text(  # session with ONLY ready-check events: still synthesized
        "INSERT INTO draft_sessions (session_id, guild_id, session_type, draft_start_time, sign_ups) "
        "VALUES ('s3', 'g', 'staked', '2025-06-01', '{\"5\": \"Echo\"}')"))
    conn.execute(text(
        "INSERT INTO sign_up_history (id, session_id, user_id, user_display_name, action, timestamp, guild_id) "
        "VALUES ('e2', 's3', '5', 'Echo', 'ready', '2025-06-01 10:00:00', 'g')"))

    assert dispnamefill0.backfill_sign_up_history(conn) == 2
    row = conn.execute(text(
        "SELECT user_display_name, action FROM sign_up_history WHERE session_id='s1'")).fetchone()
    # Provenance survives: reconstructed roster membership is not a real join.
    assert row == ("Alpha", "synthetic_join")
    assert conn.execute(text(
        "SELECT COUNT(*) FROM sign_up_history WHERE session_id='s2'")).scalar() == 1
    # Ready-check events are not roster events -- s3 got its synthetic_join.
    assert conn.execute(text(
        "SELECT COUNT(*) FROM sign_up_history WHERE session_id='s3' "
        "AND action='synthetic_join'")).scalar() == 1
    assert dispnamefill0.backfill_sign_up_history(conn) == 0


def test_name_backfill_is_guild_keyed_and_deterministic():
    conn = _conn()
    # p1 nameless in guild A; same-guild signup event AND cross-guild name:
    # same-guild event evidence must win.
    _stats_row(conn, "1", "A")
    _stats_row(conn, "1", "B", name="CrossB", games=50)
    conn.execute(text(
        "INSERT INTO sign_up_history (id, session_id, user_id, user_display_name, action, timestamp, guild_id) "
        "VALUES ('e1', 'x', '1', 'LocalNick', 'join', '2026-01-01 10:00:00', 'A')"))

    # p2 nameless in guild A; names in guilds B (10 games) and C (99 games):
    # deterministic cross-guild pick = most games.
    _stats_row(conn, "2", "A")
    _stats_row(conn, "2", "B", name="SmallGuild", games=10)
    _stats_row(conn, "2", "C", name="BigGuild", games=99)

    # p3 nameless in guild A; only evidence is an event in ANOTHER guild.
    _stats_row(conn, "3", "A")
    conn.execute(text(
        "INSERT INTO sign_up_history (id, session_id, user_id, user_display_name, action, timestamp, guild_id) "
        "VALUES ('e2', 'y', '3', 'ElsewhereNick', 'synthetic_join', '2025-06-01 10:00:00', 'B')"))

    # p4 named in guild A: untouched even though nameless in guild B.
    _stats_row(conn, "4", "A", name="KeepMe", games=5)
    _stats_row(conn, "4", "B")

    filled = dispnamefill0.backfill_missing_display_names(conn)

    names = {(g, p): n for p, g, n in conn.execute(text(
        "SELECT player_id, guild_id, display_name FROM player_stats")).fetchall()}
    assert names[("A", "1")] == "LocalNick"       # same-guild event beats cross-guild
    assert names[("A", "2")] == "BigGuild"        # most games wins, deterministically
    assert names[("A", "3")] == "ElsewhereNick"   # any-guild event as last resort
    assert names[("A", "4")] == "KeepMe"
    assert names[("B", "4")] == "KeepMe"          # p4's guild-B hole fills from guild A
    assert filled == 4
