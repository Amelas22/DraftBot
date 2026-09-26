"""card loan notification timestamps

Whether a borrower has been told their deck is ready, and when we last asked
for it back. Both additive and nullable: NULL means "not yet", which is the
correct reading for every loan that predates this.

Revision ID: libremind01
Revises: cardlib03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'libremind01'
down_revision: Union[str, Sequence[str], None] = 'cardlib03'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('card_loans', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ready_dm_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('last_reminded_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('card_loans', schema=None) as batch_op:
        batch_op.drop_column('last_reminded_at')
        batch_op.drop_column('ready_dm_at')
