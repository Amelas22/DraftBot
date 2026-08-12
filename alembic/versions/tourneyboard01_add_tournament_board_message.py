"""add tournament registration-board message columns

Revision ID: tourneyboard01
Revises: walletsafety01
Create Date: 2026-08-12
"""
from alembic import op
import sqlalchemy as sa

revision = 'tourneyboard01'
down_revision = 'walletsafety01'
branch_labels = None
depends_on = None


def _tournament_columns():
    bind = op.get_bind()
    return {c['name'] for c in sa.inspect(bind).get_columns('tournaments')}


def upgrade():
    cols = _tournament_columns()
    if 'board_channel_id' not in cols:
        op.add_column('tournaments', sa.Column('board_channel_id', sa.String(64), nullable=True))
    if 'board_message_id' not in cols:
        op.add_column('tournaments', sa.Column('board_message_id', sa.String(64), nullable=True))


def downgrade():
    cols = _tournament_columns()
    with op.batch_alter_table('tournaments', schema=None) as batch_op:
        for name in ('board_message_id', 'board_channel_id'):
            if name in cols:
                batch_op.drop_column(name)
