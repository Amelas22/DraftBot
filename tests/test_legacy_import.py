"""Legacy CSV import + old-server guild migration (helpers/legacy_import.py).

The old bot's matchResults.csv/draftResults.csv become 'staked' sessions and
match rows directly under the CURRENT guild id ('legacy-' session prefix keeps
provenance), and migrate_guild_history re-guilds the old server's native
DraftBot rows the same way — after which the ordinary backfill sees one
continuous ledger with no aliasing anywhere.
"""
from sqlalchemy import create_engine, text

from helpers.legacy_import import (
    CURRENT_GUILD_ID as NEW,
    OLD_GUILD_ID as OLD,
    community_data_present,
    import_legacy_history,
    migrate_guild_history,
    parse_legacy_csvs,
)
from helpers.skill import PRIOR_MU, backfill_skill_ratings

MATCH_CSV = """id,bluePlayer,redPlayer,result,createdAt,updatedAt,draftResultId
1,111,222,blue,2024-03-16 19:21:06.150 +00:00,2024-03-16 20:00:00.000 +00:00,1
2,111,222,red,2024-03-16 19:21:06.150 +00:00,2024-03-16 21:00:00.000 +00:00,1
3,111,333,unplayed,2024-03-17 10:00:00.000 +00:00,2024-03-17 10:00:00.000 +00:00,2
"""

DRAFT_CSV = """id,guild_id,result,team_formation,red_captain,blue_captain,createdAt,updatedAt
1,715228693529886760,draw,random,,,2024-03-16 19:21:06.138 +00:00,2024-03-16 19:21:06.138 +00:00
2,715228693529886760,blue,random,,,2024-03-17 10:00:00.000 +00:00,2024-03-17 10:00:00.000 +00:00
"""

DDL = [
    """CREATE TABLE player_stats (
        player_id TEXT, guild_id TEXT, display_name TEXT,
        true_skill_mu REAL, true_skill_sigma REAL,
        games_won INTEGER, games_lost INTEGER,
        PRIMARY KEY (player_id, guild_id))""",
    """CREATE TABLE draft_sessions (
        session_id TEXT, guild_id TEXT, session_type TEXT, draft_start_time TEXT)""",
    """CREATE TABLE match_results (
        id INTEGER PRIMARY KEY, session_id TEXT, match_number INTEGER,
        player1_id TEXT, player2_id TEXT, winner_id TEXT, guild_id TEXT,
        result_submitted_at TEXT)""",
]


def _conn():
    engine = create_engine("sqlite://")
    conn = engine.connect()
    for stmt in DDL:
        conn.execute(text(stmt))
    return conn


def _write_csvs(tmp_path):
    (tmp_path / "matchResults.csv").write_text(MATCH_CSV)
    (tmp_path / "draftResults.csv").write_text(DRAFT_CSV)
    return tmp_path


def test_community_data_present_gates_foreign_deployments():
    # The legacyimport0 migration ships in a public codebase: another
    # community's deployment (or a fresh DB) has no rows under either of this
    # community's guild ids and must skip the import entirely.
    conn = _conn()
    assert community_data_present(conn) is False
    conn.execute(text(
        "INSERT INTO draft_sessions VALUES ('s1', :g, 'staked', '2026-01-01')"),
        {"g": "999888777666555444"})
    assert community_data_present(conn) is False   # some OTHER community's data
    conn.execute(text(
        "INSERT INTO draft_sessions VALUES ('s2', :g, 'staked', '2026-01-01')"),
        {"g": NEW})
    assert community_data_present(conn) is True


def test_parse_imports_under_target_guild_and_skips_unplayed(tmp_path):
    sessions, matches = parse_legacy_csvs(_write_csvs(tmp_path), target_guild=NEW)
    assert [s["session_id"] for s in sessions] == ["legacy-1", "legacy-2"]
    # The CSV says guild 715...; the rows import as CURRENT-guild history.
    assert all(s["guild_id"] == NEW and s["session_type"] == "staked" for s in sessions)
    # Match 3 is unplayed -> excluded entirely; blue win maps to bluePlayer.
    assert len(matches) == 2
    m1, m2 = matches
    assert (m1["player1_id"], m1["player2_id"], m1["winner_id"]) == ("111", "222", "111")
    assert m2["winner_id"] == "222"
    assert m1["guild_id"] == NEW
    # Chronology: submitted_at comes from updatedAt (result recording time).
    assert m1["result_submitted_at"] == "2024-03-16 20:00:00"


def test_import_inserts_and_is_idempotent(tmp_path):
    conn = _conn()
    csv_dir = _write_csvs(tmp_path)
    assert import_legacy_history(conn, csv_dir, target_guild=NEW) == 2
    assert import_legacy_history(conn, csv_dir, target_guild=NEW) == 0
    n_sessions = conn.execute(text(
        "SELECT COUNT(*) FROM draft_sessions WHERE guild_id=:g"), {"g": NEW}).scalar()
    n_matches = conn.execute(text("SELECT COUNT(*) FROM match_results")).scalar()
    assert (n_sessions, n_matches) == (2, 2)


def test_migrate_guild_history_reguilds_sessions_and_matches():
    conn = _conn()
    conn.execute(text(
        "INSERT INTO draft_sessions VALUES ('native-old', :old, 'staked', '2025-03-15')"),
        {"old": OLD})
    conn.execute(text(
        "INSERT INTO match_results (session_id, player1_id, player2_id, winner_id, guild_id, result_submitted_at) "
        "VALUES ('native-old', '111', '222', '111', :old, '2025-03-15 10:00:00')"), {"old": OLD})
    conn.execute(text(
        "INSERT INTO player_stats (player_id, guild_id, true_skill_mu, true_skill_sigma, games_won, games_lost) "
        "VALUES ('111', :old, 26.0, 1.0, 1, 0)"), {"old": OLD})

    moved = migrate_guild_history(conn, OLD, NEW)

    assert moved == 1  # sessions moved
    assert conn.execute(text(
        "SELECT guild_id FROM draft_sessions WHERE session_id='native-old'")).scalar() == NEW
    assert conn.execute(text(
        "SELECT guild_id FROM match_results WHERE session_id='native-old'")).scalar() == NEW
    # The old guild's stats rows are gone: their history now lives (and is
    # recomputed) under the new guild.
    assert conn.execute(text(
        "SELECT COUNT(*) FROM player_stats WHERE guild_id=:g"), {"g": OLD}).scalar() == 0


def test_full_history_replays_as_one_stream(tmp_path):
    """Imported 2024 matches + a re-guilded native 2025 match + a native 2026
    match fold into one chronological rating stream under the current guild."""
    conn = _conn()
    import_legacy_history(conn, _write_csvs(tmp_path), target_guild=NEW)  # 111 1-1 222
    conn.execute(text(
        "INSERT INTO draft_sessions VALUES ('native-old', :old, 'staked', '2025-03-15')"),
        {"old": OLD})
    conn.execute(text(
        "INSERT INTO match_results (session_id, player1_id, player2_id, winner_id, guild_id, result_submitted_at) "
        "VALUES ('native-old', '111', '222', '111', :old, '2025-03-15 10:00:00')"), {"old": OLD})
    migrate_guild_history(conn, OLD, NEW)
    conn.execute(text(
        "INSERT INTO draft_sessions VALUES ('native-new', :new, 'staked', '2026-01-01')"),
        {"new": NEW})
    conn.execute(text(
        "INSERT INTO match_results (session_id, player1_id, player2_id, winner_id, guild_id, result_submitted_at) "
        "VALUES ('native-new', '111', '222', '111', :new, '2026-01-01 10:00:00')"), {"new": NEW})

    backfill_skill_ratings(conn)

    row = conn.execute(text(
        "SELECT games_won, games_lost, true_skill_mu FROM player_stats "
        "WHERE player_id='111' AND guild_id=:g"), {"g": NEW}).fetchone()
    assert (row[0], row[1]) == (3, 1)
    assert row[2] > PRIOR_MU
    assert conn.execute(text(
        "SELECT COUNT(*) FROM player_stats WHERE guild_id=:g"), {"g": OLD}).scalar() == 0
