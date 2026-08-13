"""track the leaderboard group header messages

Leaderboards now post in clusters (performance / streaks / quizzes) with a
header message opening each one. This column remembers those header messages so
they are edited in place like the boards, rather than duplicated on every run.

A JSON map keyed by group rather than a column per group: this table already
carries two columns per category, and adding a fourth cluster shouldn't need a
migration.

Revision ID: lbgroups01
Revises: orphanmatch01
"""
import sqlalchemy as sa
from alembic import op

revision = "lbgroups01"
down_revision = "orphanmatch01"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("leaderboard_messages") as batch:
        batch.add_column(sa.Column("group_header_message_ids", sa.JSON(), nullable=True))
    # Existing rows: no headers posted yet, so an empty map (not NULL) means
    # "nothing tracked" without every read needing a None guard.
    op.execute("UPDATE leaderboard_messages SET group_header_message_ids = '{}'")


def downgrade():
    with op.batch_alter_table("leaderboard_messages") as batch:
        batch.drop_column("group_header_message_ids")
