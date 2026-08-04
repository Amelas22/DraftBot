"""Import the old bot's exported history into the match_results ledger.

legacy_data/matchResults.csv + draftResults.csv hold the previous server's
10.5k played matches (2024-03-16 -> 2025-03-08), exported when the community
moved. Each old draft becomes a 'staked' draft_session (their real historical
format) directly under the CURRENT guild id — this is the same community, and
importing under one guild keeps ratings, /stats, and head-to-head queries
consistent with no aliasing anywhere. Provenance stays readable in the
'legacy-' session id prefix. Unplayed rows are skipped; no stake_info rows are
created (fabricated stakes would leak into the live debt system). Import is
idempotent via the session id prefix.

migrate_guild_history re-guilds the old server's native DraftBot rows (the
three weeks between the old bot's retirement and the server move) the same
way, so the full history replays as one stream.

Only stdlib + sqlalchemy; every function takes a raw SQLAlchemy Connection,
so the legacyimport0 migration and tests drive it against any engine.
"""
import csv
from pathlib import Path

from sqlalchemy import text

LEGACY_SESSION_PREFIX = "legacy-"

# The previous server, and the server the community moved to on 2025-03-31.
OLD_GUILD_ID = "715228693529886760"
CURRENT_GUILD_ID = "1355718878298116096"


def _clean_ts(raw):
    """'2024-03-16 19:21:06.150 +00:00' -> '2024-03-16 19:21:06' (UTC, matching
    the naive-UTC timestamps the rest of the ledger uses)."""
    return raw.split(".")[0].split("+")[0].strip()


def community_data_present(connection):
    """True iff this database belongs to the community the import is for.

    The legacyimport0 migration ships in a public codebase; other deployments
    (and fresh databases) have no sessions under either of this community's
    guild ids and must not receive its history.
    """
    return connection.execute(text(
        "SELECT 1 FROM draft_sessions WHERE guild_id IN (:old, :new) LIMIT 1"),
        {"old": OLD_GUILD_ID, "new": CURRENT_GUILD_ID}).scalar() is not None


def parse_legacy_csvs(csv_dir, target_guild=CURRENT_GUILD_ID):
    """(sessions, matches) dicts from the legacy CSV pair.

    Sessions cover every legacy draft (so imported matches always join);
    matches cover played rows only — 'unplayed' rows carry no result and can
    never be rated. Winner 'blue' is bluePlayer (player1), 'red' is redPlayer.
    All rows import under target_guild regardless of the guild in the CSV.
    """
    csv_dir = Path(csv_dir)
    with open(csv_dir / "draftResults.csv", newline="") as f:
        sessions = [
            {
                "session_id": f"{LEGACY_SESSION_PREFIX}{row['id']}",
                "guild_id": target_guild,
                "session_type": "staked",
                "draft_start_time": _clean_ts(row["createdAt"]),
            }
            for row in csv.DictReader(f)
        ]

    matches = []
    with open(csv_dir / "matchResults.csv", newline="") as f:
        for row in csv.DictReader(f):
            if row["result"] not in ("blue", "red"):
                continue
            matches.append({
                "session_id": f"{LEGACY_SESSION_PREFIX}{row['draftResultId']}",
                "match_number": int(row["id"]),
                "player1_id": row["bluePlayer"],
                "player2_id": row["redPlayer"],
                "winner_id": row["bluePlayer"] if row["result"] == "blue" else row["redPlayer"],
                "guild_id": target_guild,
                "result_submitted_at": _clean_ts(row["updatedAt"]),
            })
    return sessions, matches


def import_legacy_history(connection, csv_dir, target_guild=CURRENT_GUILD_ID):
    """Insert legacy sessions + played matches; returns matches inserted.

    Idempotent: if any legacy session already exists the import is a no-op
    (the CSVs are a frozen historical export — partial state never occurs
    outside a failed transaction, which rolls back wholesale).
    """
    existing = connection.execute(text(
        "SELECT 1 FROM draft_sessions WHERE session_id LIKE :pfx LIMIT 1"),
        {"pfx": f"{LEGACY_SESSION_PREFIX}%"}).scalar()
    if existing:
        return 0

    sessions, matches = parse_legacy_csvs(csv_dir, target_guild)
    # Lists of dicts trigger executemany — two statements instead of ~11.7k.
    connection.execute(text(
        "INSERT INTO draft_sessions (session_id, guild_id, session_type, draft_start_time) "
        "VALUES (:session_id, :guild_id, :session_type, :draft_start_time)"), sessions)
    connection.execute(text(
        "INSERT INTO match_results (session_id, match_number, player1_id, player2_id, "
        "winner_id, guild_id, result_submitted_at) "
        "VALUES (:session_id, :match_number, :player1_id, :player2_id, "
        ":winner_id, :guild_id, :result_submitted_at)"), matches)
    return len(matches)


def migrate_guild_history(connection, old_guild=OLD_GUILD_ID, new_guild=CURRENT_GUILD_ID):
    """Move the old server's sessions and match rows to the current guild.

    Also drops the old guild's player_stats rows: their history is recomputed
    under the new guild by the backfill that follows this in the
    legacyimport0 migration. Deliberately narrower than the 9-table repoint in
    scripts/update_guild_id.py: the other guild-keyed tables (log channels,
    leaderboard messages, sign-up history, ...) reference Discord entities of
    the dead server and must stay under its id.
    Returns the number of sessions moved (0 on re-run — idempotent).
    """
    moved = connection.execute(text(
        "UPDATE draft_sessions SET guild_id=:new WHERE guild_id=:old"),
        {"new": new_guild, "old": old_guild}).rowcount
    connection.execute(text(
        "UPDATE match_results SET guild_id=:new WHERE guild_id=:old"),
        {"new": new_guild, "old": old_guild})
    connection.execute(text(
        "DELETE FROM player_stats WHERE guild_id=:old"), {"old": old_guild})
    return moved
