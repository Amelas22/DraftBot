"""mtgo_jobs vault baseline

Every tix trade records the vault's tix count just before it goes out
(vault_before, read at vault_before_at), so settling can compare the vault
afterwards and book what actually moved -- not only what the serve reported.
Additive and nullable: a job written before this has no baseline and settles
as it always did. `assumed` / `alerted_at` mark a trade settled on an assumption
the vault couldn't prove, and when an admin was told about it.

Revision ID: vaultcheck01
Revises: libresv01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'vaultcheck01'
down_revision: Union[str, Sequence[str], None] = 'libresv01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('mtgo_jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('vault_before', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('vault_before_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('assumed', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('alerted_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('mtgo_jobs', schema=None) as batch_op:
        batch_op.drop_column('alerted_at')
        batch_op.drop_column('assumed')
        batch_op.drop_column('vault_before_at')
        batch_op.drop_column('vault_before')
