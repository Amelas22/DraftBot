"""Give every wallet ledger row an asset

Adds wallet_tx.asset and widens uq_wallet_tx_transfer_legs from
(source, kind) to (source, kind, asset), so one transfer source can carry a
pay/receive pair per asset.

Additive: every existing row is backfilled as 'tix' by the column's server
default. Batch mode rebuilds the table on SQLite, so back up the production
database before deploying this.

Revision ID: walletasset01
Revises: srhide01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'walletasset01'
down_revision: Union[str, Sequence[str], None] = 'srhide01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LEGS_WHERE = "kind IN ('pay', 'receive') AND source IS NOT NULL"


def upgrade() -> None:
    with op.batch_alter_table('wallet_tx', schema=None) as batch_op:
        batch_op.add_column(sa.Column('asset', sa.String(length=16), nullable=False,
                                      server_default=sa.text("'tix'")))
        batch_op.drop_index('uq_wallet_tx_transfer_legs')
        batch_op.create_index('uq_wallet_tx_transfer_legs', ['source', 'kind', 'asset'],
                              unique=True, sqlite_where=sa.text(_LEGS_WHERE))


def downgrade() -> None:
    with op.batch_alter_table('wallet_tx', schema=None) as batch_op:
        batch_op.drop_index('uq_wallet_tx_transfer_legs')
        batch_op.create_index('uq_wallet_tx_transfer_legs', ['source', 'kind'],
                              unique=True, sqlite_where=sa.text(_LEGS_WHERE))
        batch_op.drop_column('asset')
