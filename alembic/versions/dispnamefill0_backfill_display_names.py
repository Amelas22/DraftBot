"""complete sign_up_history, then backfill missing display names

Two-step data fill, with the logic FROZEN in this file (migrations must not
depend on the future behavior of live helpers):

1. sign_up_history (live event recording began 2025-08) is completed
   backward by synthesizing events from the final-roster sign_ups JSON of
   older sessions. Synthetic rows carry action='synthetic_join' so
   reconstructed roster membership is forever distinguishable from an
   observed queue join; only sessions with ZERO real events are touched,
   making the run idempotent.
2. player_stats display names are filled per (guild_id, player_id) —
   names are per-guild nicknames — from deterministic evidence, in order:
   the player's latest sign_up_history event IN THAT GUILD; their
   display_name in another guild's stats row (most rated games wins,
   guild_id as the final tiebreak); their latest event in any guild.
   Players with no evidence anywhere resolve at display time via live
   Discord member lookup.

Data-only; idempotent; safe on any deployment (purely self-relative).
Downgrade is a no-op.

Revision ID: dispnamefill0
Revises: cardlend0col
Create Date: 2026-08-08
"""
import json
import uuid

from alembic import op
from sqlalchemy import text

revision = "dispnamefill0"
down_revision = "cardlend0col"
branch_labels = None
depends_on = None


def backfill_sign_up_history(connection) -> int:
    """Synthesize provenance-marked events from final-roster sign_ups JSON
    for sessions recorded before live event tracking existed. Returns
    events created."""
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
            events.append({
                "id": str(uuid.uuid4()), "sid": session_id, "uid": pid,
                "n": name if isinstance(name, str) else None,
                "ts": started_at, "g": guild_id})
    if events:
        connection.execute(text(
            "INSERT INTO sign_up_history (id, session_id, user_id, "
            "user_display_name, action, timestamp, guild_id) "
            "VALUES (:id, :sid, :uid, :n, 'synthetic_join', :ts, :g)"), events)
    return len(events)


def backfill_missing_display_names(connection) -> int:
    """Fill empty player_stats display names per (guild, player) from the
    best deterministic evidence. Returns rows updated."""
    targets: set = set()          # (guild_id, player_id)
    stats_names: dict = {}        # player_id -> [(games, guild_id, name)]
    for pid, guild, name, gw, gl in connection.execute(text(
        "SELECT player_id, guild_id, display_name, "
        "COALESCE(games_won, 0), COALESCE(games_lost, 0) FROM player_stats"
    )).fetchall():
        if name:
            stats_names.setdefault(pid, []).append((gw + gl, guild, name))
        else:
            targets.add((guild, pid))
    if not targets:
        return 0

    target_pids = {pid for _, pid in targets}
    # Latest event per (guild, player) and per player overall; ascending
    # timestamp order means later events overwrite earlier ones.
    guild_events: dict = {}       # (guild_id, player_id) -> name
    any_events: dict = {}         # player_id -> name
    for pid, guild, name in connection.execute(text(
        "SELECT user_id, guild_id, user_display_name FROM sign_up_history "
        "WHERE user_display_name IS NOT NULL AND user_display_name != '' "
        "ORDER BY timestamp")).fetchall():
        if pid in target_pids:
            guild_events[(guild, pid)] = name
            any_events[pid] = name

    updates = []
    for guild, pid in targets:
        name = guild_events.get((guild, pid))
        if not name and pid in stats_names:
            # Deterministic cross-guild pick: most rated games, then the
            # lowest guild_id as a stable final tiebreak.
            name = sorted(stats_names[pid], key=lambda t: (-t[0], t[1]))[0][2]
        if not name:
            name = any_events.get(pid)
        if name:
            updates.append({"n": name, "p": pid, "g": guild})
    if updates:
        connection.execute(text(
            "UPDATE player_stats SET display_name = :n "
            "WHERE player_id = :p AND guild_id = :g "
            "AND (display_name IS NULL OR display_name = '')"), updates)
    return len(updates)


def upgrade():
    conn = op.get_bind()
    backfill_sign_up_history(conn)
    backfill_missing_display_names(conn)


def downgrade():
    # One-way data fill; nothing to reverse.
    pass
