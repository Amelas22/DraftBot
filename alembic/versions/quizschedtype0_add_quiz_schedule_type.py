"""add quiz_type to quiz_schedules

Revision ID: quizschedtype0
Revises: trophreveal01
"""
from alembic import op
import sqlalchemy as sa

revision = "quizschedtype0"
down_revision = "trophreveal01"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "quiz_schedules",
        sa.Column("quiz_type", sa.Text(), nullable=False, server_default=sa.text("'pick'")),
    )


def downgrade():
    op.drop_column("quiz_schedules", "quiz_type")
