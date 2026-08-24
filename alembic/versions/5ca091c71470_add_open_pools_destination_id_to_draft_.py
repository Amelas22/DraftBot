"""add open pools destination id to draft sessions

The open pools thread a tournament match gets in the shared draft chat needs its own
recorded destination, for the same reason the two per-team ones have theirs: the post
run is reconciler-driven, so a retry must resume into the thread it already opened
rather than open a second one beside it.

Hand-trimmed from the autogenerate output. --autogenerate additionally proposed
dropping match_results.guild_id, mtgo_accounts.verified, ix_mtgo_jobs_status and the
two partial unique indexes on wallet_tx (uq_wallet_tx_job_kind,
uq_wallet_tx_transfer_legs) — pre-existing model/schema drift, not part of this change.
Those two wallet indexes are what make a deposit and a transfer idempotent, so running
that as generated would have quietly removed the guarantee that stops a replayed poll
double-crediting. Only the column add is kept here.

Revision ID: 5ca091c71470
Revises: 34dfe25d7e3e
Create Date: 2026-08-24 06:29:11.822134

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5ca091c71470'
down_revision: Union[str, Sequence[str], None] = '34dfe25d7e3e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('open_pools_destination_id', sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.drop_column('open_pools_destination_id')
