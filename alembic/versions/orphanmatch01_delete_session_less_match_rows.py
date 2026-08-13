"""delete match rows that belong to no draft session

Follow-up to dropguild01, which found them: 12 prod rows carry no session_id
at all -- not a dangling reference, no link ever recorded. They are one team
draft's complete scaffolding (8 players, 3 rounds x 4 matches, match_number
1-12, contiguous ids, three pairing messages timestamped 2026-04-28
15:43:49Z) that was paired and then never played: every row 0-0, no winner,
result_submitted_at NULL.

Nothing can attribute them. No draft_sessions row holds those players, so
there is no session to point them at, and with dropguild01 there is no
guild_id on the row either. They are invisible to every stats path (all of
which join DraftSession), so this removes clutter rather than changing any
number -- the value is that the next person profiling this table doesn't
rediscover them the hard way.

The predicate is guarded to result-free rows: a session-less row that somehow
carries a result is left in place and stays visible, rather than being swept
up by a broad "session_id IS NULL" delete. Verified on a prod copy that all
12 qualify and that zero rows have a dangling (non-null, unresolvable)
session_id.

One-way: deleted rows cannot be restored, so downgrade is a no-op rather than
a lie.

Revision ID: orphanmatch01
Revises: dropguild01
"""
from alembic import op

revision = "orphanmatch01"
down_revision = "dropguild01"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "DELETE FROM match_results"
        " WHERE session_id IS NULL"
        "   AND winner_id IS NULL"
        "   AND result_submitted_at IS NULL"
        "   AND COALESCE(player1_wins, 0) = 0"
        "   AND COALESCE(player2_wins, 0) = 0"
    )


def downgrade():
    # Deleted scaffolding rows carried no information to restore.
    pass
