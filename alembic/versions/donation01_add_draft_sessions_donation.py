"""Donation drafts

Adds draft_sessions.donation. A donation draft plays like a staked draft but
escrows nothing: its losers owe their matched share to DraftBot.

Additive only: every existing row reads false.

Revision ID: donation01
Revises: dmpref01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'donation01'
down_revision: Union[str, Sequence[str], None] = 'dmpref01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('donation', sa.Boolean(), nullable=False,
                                      server_default=sa.text('0')))


def downgrade() -> None:
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.drop_column('donation')
