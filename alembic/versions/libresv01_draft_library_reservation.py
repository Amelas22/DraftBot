"""a draft's reservation of its rental library's cube

`draft_sessions.library_reserved_at` / `library_released_at`: when the draft
took the library's copy of its cube and when it gave it back. Both NULL means
it never reserved, which is every draft before this column.

`draft_sessions.library_released`: players who said they don't need the
library for that draft. `draft_sessions.library_agreed`: players who agreed to
borrow in it, without whom a reservation does not outlast teams forming.

Additive: nothing is dropped and no existing row changes meaning.

Revision ID: libresv01
Revises: libpref01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'libresv01'
down_revision: Union[str, Sequence[str], None] = 'libpref01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('library_reserved_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('library_released_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('library_released', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('library_agreed', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.drop_column('library_agreed')
        batch_op.drop_column('library_released')
        batch_op.drop_column('library_released_at')
        batch_op.drop_column('library_reserved_at')
