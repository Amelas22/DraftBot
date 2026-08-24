"""add participant role id

Revision ID: f31a48ad8c46
Revises: 34dfe25d7e3e
Create Date: 2026-08-24 06:18:01.618142

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f31a48ad8c46'
down_revision: Union[str, Sequence[str], None] = '34dfe25d7e3e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('tournament_participants') as batch_op:
        batch_op.add_column(sa.Column('role_id', sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('tournament_participants') as batch_op:
        batch_op.drop_column('role_id')
