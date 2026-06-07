"""add pack settings to draft_sessions

Revision ID: b7c4e1a9pk01
Revises: d14b002764f0
Create Date: 2026-06-07 05:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c4e1a9pk01'
down_revision: Union[str, Sequence[str], None] = 'd14b002764f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('packs_per_player', sa.Integer(), nullable=True, server_default='3'))
        batch_op.add_column(sa.Column('cards_per_pack', sa.Integer(), nullable=True, server_default='15'))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.drop_column('cards_per_pack')
        batch_op.drop_column('packs_per_player')
