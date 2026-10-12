"""a second collateral figure, for a borrower the library does not know

One nullable column on `library`. NULL means the library is not tiered and
charges everyone its existing `collateral_tix`, so every library behaves exactly
as it did until somebody sets this from the shell tool.

Additive: nothing is dropped and no existing row changes meaning.

Revision ID: libtrust01
Revises: bracketmsg01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'libtrust01'
down_revision: Union[str, Sequence[str], None] = 'bracketmsg01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('library', schema=None) as batch_op:
        batch_op.add_column(sa.Column('collateral_tix_untrusted',
                                      sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('library', schema=None) as batch_op:
        batch_op.drop_column('collateral_tix_untrusted')
