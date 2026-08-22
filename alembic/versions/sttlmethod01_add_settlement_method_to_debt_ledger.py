"""Add settlement_method to debt_ledger and classify existing settlement rows.

Nothing in debt_ledger previously distinguished a settlement auto-drawn from the
payer's wallet (mtgo_resolution_service.settle_debt_from_wallet) from one recorded
after tix moved in MTGO (debt_service.create_settlement, the manual /settle path).
Both writers used source_type='settlement'; the only difference was a notes prefix,
and the post-draft stake summary rendered every settlement as a wallet payment as a
result — wrong for the common case (in production, 2,100 of 2,194 settlement rows
are manual, only 94 are wallet).

The backfill below is the LAST moment the notes prefix can be used to tell the two
apart: both note strings are bot-generated constants ("Wallet debt settlement:
{amount} tix" vs whatever /settle records), so they're a reliable classifier for
history. From this migration forward, both writers set settlement_method explicitly
at write time, so nothing after this depends on note text again.

Revision ID: sttlmethod01
Revises: srladder0col1
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'sttlmethod01'
down_revision: Union[str, Sequence[str], None] = 'srladder0col1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('debt_ledger', schema=None) as batch_op:
        batch_op.add_column(sa.Column('settlement_method', sa.String(length=16), nullable=True))

    # Classify existing settlement rows from their notes prefix (see module docstring).
    op.execute(
        "UPDATE debt_ledger SET settlement_method = 'wallet' "
        "WHERE source_type = 'settlement' AND notes LIKE 'Wallet debt settlement%'"
    )
    op.execute(
        "UPDATE debt_ledger SET settlement_method = 'external' "
        "WHERE source_type = 'settlement' AND settlement_method IS NULL"
    )


def downgrade() -> None:
    with op.batch_alter_table('debt_ledger', schema=None) as batch_op:
        batch_op.drop_column('settlement_method')
