"""add trophy_quiz_reveals table

Revision ID: trophreveal01
Revises: troph1quiz00
"""
from alembic import op
import sqlalchemy as sa

revision = "trophreveal01"
down_revision = "troph1quiz00"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "trophy_quiz_reveals",
        sa.Column("quiz_id", sa.String(length=64), nullable=False),
        sa.Column("player_id", sa.String(length=64), nullable=False),
        sa.Column("revealed_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["quiz_id"], ["trophy_quiz_sessions.quiz_id"]),
        sa.PrimaryKeyConstraint("quiz_id", "player_id"),
    )


def downgrade():
    op.drop_table("trophy_quiz_reveals")
