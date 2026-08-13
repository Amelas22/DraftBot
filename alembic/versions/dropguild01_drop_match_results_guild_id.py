"""drop match_results.guild_id -- the draft session is the source of truth

The column was a denormalized copy with no writer: none of the three
MatchResult creation sites in utils.py ever set it, so it read NULL on 31,403
of 41,920 prod rows (every match created since mid-2025). The populated rows
predate the model even knowing the column existed -- it was added to the live
DB out of band and backfilled once, then models/match.py was retrofitted in
"update model to reflect db" (0a4b5b6, July 2025).

Nothing read it: every guild-scoped query already joins DraftSession
(services/ledger_stats.py, scripts/backfill_streak_enders.py), which is why a
75%-empty column went unnoticed for a year. Verified on a prod copy before
dropping -- the column carries no information the join can't produce:

  * 10,517 non-null rows: all resolve to a session, all agree with it, 0 disagree
  * 31,391 null rows: all resolve to a session (derivable)
  * 12 null rows: no session_id at all, so no guild was ever recorded on
    either side; they are one unplayed draft's pairings (0-0, no winner, never
    submitted) and are invisible to every stats path regardless

Backfilling instead would have copied 31,391 values a join already yields and
started drifting again from the next draft on, since the writers stayed
unfixed. Deriving leaves one source of truth and turns a future
MatchResult.guild_id into an AttributeError rather than a silently short
result.

Revision ID: dropguild01
Revises: tourneyboard01
"""
import sqlalchemy as sa
from alembic import op

revision = "dropguild01"
down_revision = "tourneyboard01"
branch_labels = None
depends_on = None


def upgrade():
    # batch mode for SQLite portability: native DROP COLUMN needs 3.35+, and
    # the deployment's sqlite version isn't ours to assume.
    with op.batch_alter_table("match_results") as batch:
        batch.drop_column("guild_id")


def downgrade():
    with op.batch_alter_table("match_results") as batch:
        batch.add_column(sa.Column("guild_id", sa.String(64), nullable=True))
    # Re-derive from the owning session rather than restoring the old mix of
    # populated and NULL: the join is what the column always should have been.
    op.execute(
        "UPDATE match_results SET guild_id = ("
        "  SELECT ds.guild_id FROM draft_sessions ds"
        "  WHERE ds.session_id = match_results.session_id)"
    )
