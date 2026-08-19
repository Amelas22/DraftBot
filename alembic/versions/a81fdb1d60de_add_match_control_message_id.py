"""add match control message id

Revision ID: a81fdb1d60de
Revises: srladder0col1
Create Date: 2026-08-19 14:55:58.892524

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a81fdb1d60de'
down_revision: Union[str, Sequence[str], None] = 'srladder0col1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('tournament_matches', schema=None) as batch_op:
        batch_op.add_column(sa.Column('control_message_id', sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tournament_matches', schema=None) as batch_op:
        batch_op.drop_column('control_message_id')
