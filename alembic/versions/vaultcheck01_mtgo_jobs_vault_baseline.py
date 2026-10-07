"""mtgo_jobs vault baseline

Every tix trade records the vault's tix count just before it goes out
(vault_before, read at vault_before_at), so settling can compare the vault
afterwards and book what actually moved -- not only what the serve reported.
Additive and nullable: a job written before this has no baseline and settles
as it always did.

Revision ID: vaultcheck01
Revises: bracketmsg01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'vaultcheck01'
down_revision: Union[str, Sequence[str], None] = 'bracketmsg01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('mtgo_jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('vault_before', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('vault_before_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('mtgo_jobs', schema=None) as batch_op:
        batch_op.drop_column('vault_before_at')
        batch_op.drop_column('vault_before')
