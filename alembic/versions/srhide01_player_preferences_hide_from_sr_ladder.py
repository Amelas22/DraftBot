"""Hide from the Skill Rating Ladder

Adds player_preferences.hide_from_sr_ladder. A player who sets it is left off
the Skill Rating Ladder board.

Additive only: nullable with no default, because the row is shared with
unrelated answers and creating it for one must not decide this one. NULL reads
as listed.

Revision ID: srhide01
Revises: donation02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'srhide01'
down_revision: Union[str, Sequence[str], None] = 'donation02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('player_preferences', schema=None) as batch_op:
        batch_op.add_column(sa.Column('hide_from_sr_ladder', sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('player_preferences', schema=None) as batch_op:
        batch_op.drop_column('hide_from_sr_ladder')
