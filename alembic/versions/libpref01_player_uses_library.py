"""whether a player borrows from the library

`player_preferences.uses_library`: NULL means the player has not been asked,
which is every player until the sign-up flow asks them.

Additive: nothing is dropped and no existing row changes meaning.

Revision ID: libpref01
Revises: libopen01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'libpref01'
down_revision: Union[str, Sequence[str], None] = 'libopen01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('player_preferences', schema=None) as batch_op:
        batch_op.add_column(sa.Column('uses_library', sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('player_preferences', schema=None) as batch_op:
        batch_op.drop_column('uses_library')
