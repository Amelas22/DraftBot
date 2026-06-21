"""add lobby_ready_check_message_id to draft_sessions

Revision ID: l0bby1rc2msg3
Revises: a1c2t3i4on32
Create Date: 2026-06-20 12:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'l0bby1rc2msg3'
down_revision: Union[str, Sequence[str], None] = 'a1c2t3i4on32'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('lobby_ready_check_message_id', sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.drop_column('lobby_ready_check_message_id')
