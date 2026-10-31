"""a separate hold for a loan of a few named cards

Two nullable columns on `library`. NULL means no separate small-loan figure, and
each then falls back on its deck counterpart, so every library charges exactly
what it did until somebody sets them from the shell tool.

Additive: nothing is dropped and no existing row changes meaning.

Revision ID: smallhold01
Revises: lateclaim01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'smallhold01'
down_revision: Union[str, Sequence[str], None] = 'lateclaim01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('library', schema=None) as batch_op:
        batch_op.add_column(sa.Column('small_loan_tix', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('small_loan_tix_untrusted',
                                      sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('library', schema=None) as batch_op:
        batch_op.drop_column('small_loan_tix_untrusted')
        batch_op.drop_column('small_loan_tix')
