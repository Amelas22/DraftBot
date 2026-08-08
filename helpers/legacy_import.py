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
backfill_sign_up_history completes the sign_up_history event table from the
final-roster sign_ups JSON of sessions that predate live event recording
(started 2025-08), so the event table is THE one historical signup record.
backfill_missing_display_names then names legacy-only players from the best
sources: another guild's player_stats, else their latest sign_up_history
event.

Only stdlib + sqlalchemy; every function takes a raw SQLAlchemy Connection,
so the legacyimport0 migration and tests drive it against any engine.
"""
import csv
import json
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


def backfill_sign_up_history(connection) -> int:
    """Synthesize 'join' events from final-roster sign_ups JSON for sessions
    recorded before live event tracking existed (2025-08).

    Only sessions with a sign_ups roster and ZERO existing history rows are
    touched, so real event streams are never mixed with synthetic ones and
    re-runs are no-ops. Synthetic events carry the session's start time --
    the roster is a snapshot, so per-user join times are unknowable.
    Returns events created.
    """
    import uuid as _uuid

    rows = connection.execute(text(
        "SELECT d.session_id, d.guild_id, d.draft_start_time, d.sign_ups "
        "FROM draft_sessions d WHERE d.sign_ups IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM sign_up_history h WHERE h.session_id = d.session_id)"
    )).fetchall()

    events = []
    for session_id, guild_id, started_at, su in rows:
        try:
            roster = json.loads(su) if isinstance(su, str) else su
        except (ValueError, TypeError):
            continue
        if not isinstance(roster, dict):
            continue
        for pid, name in roster.items():
            if not isinstance(name, str):
                name = None
            events.append({
                "id": str(_uuid.uuid4()), "sid": session_id, "uid": pid,
                "n": name, "ts": started_at, "g": guild_id})
    if events:
        connection.execute(text(
            "INSERT INTO sign_up_history (id, session_id, user_id, "
            "user_display_name, action, timestamp, guild_id) "
            "VALUES (:id, :sid, :uid, :n, 'join', :ts, :g)"), events)
    return len(events)


def backfill_missing_display_names(connection) -> int:
    """Fill missing player_stats.display_name from the best available source.

    Legacy-only players (imported history, never drafted live) have stats
    rows with no display name, so leaderboards fall back to "User <id>".
    Two data sources can name many of them, in priority order:
    1. their display_name in ANY other guild's player_stats row (maintained
       by the live path, freshest), then
    2. their most recent appearance in a draft session's sign_ups JSON
       (the name they signed up under at the time).
    Players present in neither stay unnamed and resolve at display time via
    the live Discord member lookup (if still in the server). Idempotent:
    only rows with NULL/'' names are touched. Returns rows updated.
    """
    # One scan buckets every stats row: rows with empty names are targets,
    # rows with names feed the cross-guild source for those same targets.
    targets: set = set()
    named_rows: list = []
    for pid, name in connection.execute(text(
        "SELECT player_id, display_name FROM player_stats")).fetchall():
        if name:
            named_rows.append((pid, name))
        else:
            targets.add(pid)
    if not targets:
        return 0

    names: dict[str, str] = {}
    for pid, name in named_rows:
        if pid in targets and pid not in names:
            names[pid] = name

    remaining = targets - set(names)
    if remaining:
        # sign_up_history is complete back to the earliest rosters once
        # backfill_sign_up_history has run (the migration runs it first);
        # ascending timestamp means the latest event's name wins.
        signup_names: dict[str, str] = {}
        for pid, name in connection.execute(text(
            "SELECT user_id, user_display_name FROM sign_up_history "
            "WHERE user_display_name IS NOT NULL AND user_display_name != '' "
            "ORDER BY timestamp")).fetchall():
            if pid in remaining:
                signup_names[pid] = name
        names.update(signup_names)

    if names:
        # List of dicts triggers executemany -- same idiom as the import above.
        connection.execute(text(
            "UPDATE player_stats SET display_name = :n "
            "WHERE player_id = :p AND (display_name IS NULL OR display_name = '')"),
            [{"n": name, "p": pid} for pid, name in names.items()])
    return len(names)
