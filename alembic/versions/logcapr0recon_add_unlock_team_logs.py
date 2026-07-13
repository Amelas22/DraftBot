"""add unlock_at and team_logs_posted_at to draft_sessions

Revision ID: logcapr0recon
Revises: tskill0backfl
Create Date: 2026-07-13
"""
from alembic import op
import sqlalchemy as sa

revision = "logcapr0recon"
down_revision = "tskill0backfl"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("draft_sessions") as batch_op:
        batch_op.add_column(sa.Column("unlock_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("team_logs_posted_at", sa.DateTime(), nullable=True))

    # Backfill unlock_at for drafts captured but not yet published at deploy time,
    # so the new reconciler publishes them (the old in-memory publish timer is gone).
    # 180 minutes mirrors the production PUBLISH_DELAY; bounded to the last day so we
    # don't resurrect ancient unpublished drafts.
    op.execute(
        """
        UPDATE draft_sessions
        SET unlock_at = datetime(logs_captured_at, '+180 minutes')
        WHERE logs_captured_at IS NOT NULL
          AND unlock_at IS NULL
          AND (data_received = 0 OR data_received IS NULL)
          AND logs_captured_at >= datetime('now', '-1 day')
        """
    )


def downgrade():
    with op.batch_alter_table("draft_sessions") as batch_op:
        batch_op.drop_column("team_logs_posted_at")
        batch_op.drop_column("unlock_at")
