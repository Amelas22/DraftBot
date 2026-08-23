"""add tournament cut columns

Revision ID: 34dfe25d7e3e
Revises: 748aae6ae438
Create Date: 2026-08-23 15:57:07.539833

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '34dfe25d7e3e'
down_revision: Union[str, Sequence[str], None] = '748aae6ae438'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('tournaments') as batch_op:
        batch_op.add_column(sa.Column('cut_to', sa.Integer(), nullable=True))
    with op.batch_alter_table('tournament_rounds') as batch_op:
        batch_op.add_column(sa.Column('stage', sa.String(length=16), nullable=False,
                                      server_default='swiss'))
    with op.batch_alter_table('tournament_participants') as batch_op:
        batch_op.add_column(sa.Column('seed', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('tournament_participants') as batch_op:
        batch_op.drop_column('seed')
    with op.batch_alter_table('tournament_rounds') as batch_op:
        batch_op.drop_column('stage')
    with op.batch_alter_table('tournaments') as batch_op:
        batch_op.drop_column('cut_to')
