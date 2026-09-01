"""premade draft entry fee

Revision ID: 5d58521b7531
Revises: cf93dc5532bb
Create Date: 2026-09-01 11:53:20.919934

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5d58521b7531'
down_revision: Union[str, Sequence[str], None] = 'schematruth01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the premade entry fee column.

    Additive and nullable: NULL is a free draft, which is every premade that
    exists today, so nothing already in flight changes behaviour.

    Autogenerate also proposed dropping match_results.guild_id,
    mtgo_accounts.verified, ix_mtgo_jobs_status, uq_wallet_tx_job_kind and
    uq_wallet_tx_transfer_legs. Those are pre-existing model/schema drift, not
    part of this change, and two of them are the unique indexes that make
    wallet transfers idempotent -- dropping them would let a replayed transfer
    book twice. They are deliberately not here.
    """
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('entry_fee', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Drop the column. The fee is recorded per draft, so a draft settled while
    it existed keeps its money in the ledger regardless."""
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.drop_column('entry_fee')
