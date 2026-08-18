"""Add the two message-tracking columns for the sr_ladder leaderboard.

Every leaderboard category needs a pair of columns on leaderboard_messages:
the id of the posted board message and the timeframe it was rendered with.
Purely additive and nullable, so it carries no data-loss risk.

Revision ID: srladder0col1
Revises: dropteamreg01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'srladder0col1'
down_revision: Union[str, Sequence[str], None] = 'dropteamreg01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('leaderboard_messages',
                  sa.Column('sr_ladder_view_message_id', sa.String(length=64), nullable=True))
    op.add_column('leaderboard_messages',
                  sa.Column('sr_ladder_timeframe', sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column('leaderboard_messages', 'sr_ladder_timeframe')
    op.drop_column('leaderboard_messages', 'sr_ladder_view_message_id')
