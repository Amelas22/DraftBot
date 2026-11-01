"""Remember a hosted draft's pause and its bot-played seats

Adds three nullable draft_sessions columns for drafts on the self-hosted
Draftmancer: hosted_paused_by ('bot' or 'human'), hosted_pause_notice_id, and
hosted_bot_seats (JSON list of seat names given to a bot). NULL on every
existing row means not paused and no bot seats.

Revision ID: hosted02
Revises: hosted01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'hosted02'
down_revision: Union[str, Sequence[str], None] = 'hosted01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('hosted_paused_by', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('hosted_pause_notice_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('hosted_bot_seats', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.drop_column('hosted_bot_seats')
        batch_op.drop_column('hosted_pause_notice_id')
        batch_op.drop_column('hosted_paused_by')
