"""add logs_captured_at to draft_sessions

Revision ID: c4pture10g5at
Revises: l0bby1rc2msg3
Create Date: 2026-06-22 02:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4pture10g5at'
down_revision: Union[str, Sequence[str], None] = 'l0bby1rc2msg3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('logs_captured_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.drop_column('logs_captured_at')
