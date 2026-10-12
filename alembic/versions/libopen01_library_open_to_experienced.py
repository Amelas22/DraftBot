"""a draft-count gate on a library

`library.min_drafts_to_borrow`: NULL means the member list alone decides who
may borrow, which is every library until somebody sets it from the shell tool.

Additive: nothing is dropped and no existing row changes meaning.

Revision ID: libopen01
Revises: libtrust01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'libopen01'
down_revision: Union[str, Sequence[str], None] = 'libtrust01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('library', schema=None) as batch_op:
        batch_op.add_column(sa.Column('min_drafts_to_borrow', sa.Integer(),
                                      nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('library', schema=None) as batch_op:
        batch_op.drop_column('min_drafts_to_borrow')
