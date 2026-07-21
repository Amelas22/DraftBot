"""reveal names on guess, pay to re-guess: add finalized/changed_answer to
trophy_quiz_submissions and drop the now-unused trophy_quiz_reveals table

Revision ID: trophreguess0
Revises: quizschedtype0
"""
from alembic import op
import sqlalchemy as sa

revision = "trophreguess0"
down_revision = "quizschedtype0"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "trophy_quiz_submissions",
        sa.Column("finalized", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "trophy_quiz_submissions",
        sa.Column("changed_answer", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )
    op.drop_table("trophy_quiz_reveals")


def downgrade():
    op.create_table(
        "trophy_quiz_reveals",
        sa.Column("quiz_id", sa.String(length=64), nullable=False),
        sa.Column("player_id", sa.String(length=64), nullable=False),
        sa.Column("revealed_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["quiz_id"], ["trophy_quiz_sessions.quiz_id"]),
        sa.PrimaryKeyConstraint("quiz_id", "player_id"),
    )
    op.drop_column("trophy_quiz_submissions", "changed_answer")
    op.drop_column("trophy_quiz_submissions", "finalized")
