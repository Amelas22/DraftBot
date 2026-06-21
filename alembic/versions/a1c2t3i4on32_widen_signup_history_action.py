"""widen sign_up_history.action to 32 chars

Revision ID: a1c2t3i4on32
Revises: 935bb3b19df1
Create Date: 2026-06-20 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c2t3i4on32'
down_revision: Union[str, Sequence[str], None] = '935bb3b19df1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('sign_up_history', schema=None) as batch_op:
        batch_op.alter_column(
            'action',
            existing_type=sa.String(length=16),
            type_=sa.String(length=32),
            existing_nullable=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('sign_up_history', schema=None) as batch_op:
        batch_op.alter_column(
            'action',
            existing_type=sa.String(length=32),
            type_=sa.String(length=16),
            existing_nullable=False,
        )
