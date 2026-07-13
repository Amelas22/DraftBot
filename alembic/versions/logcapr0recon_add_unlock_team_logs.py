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


def downgrade():
    with op.batch_alter_table("draft_sessions") as batch_op:
        batch_op.drop_column("team_logs_posted_at")
        batch_op.drop_column("unlock_at")
