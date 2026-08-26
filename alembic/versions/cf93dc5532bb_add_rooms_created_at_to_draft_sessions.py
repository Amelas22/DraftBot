"""add rooms_created_at to draft sessions

The marker that room creation actually FINISHED. The old completeness check read
draft_chat_channel, which create_team_channel commits in its own session while
creating the first of three channels -- so any failure after that left a flag
saying "done" over a half-created draft, and every retry believed it.

Backfilled for every session that already has a draft chat. Without that, every
historical draft would read as incomplete the moment this deploys, and anything
that re-entered room creation for one would start making channels for a draft
that finished weeks ago. draft_start_time is used as the stamp because it is the
only time we know was before the rooms existed; the exact value never matters,
only that it is set.

Hand-trimmed from the autogenerate output. --autogenerate additionally proposed
dropping match_results.guild_id, mtgo_accounts.verified, ix_mtgo_jobs_status and
the two partial unique indexes on wallet_tx (uq_wallet_tx_job_kind,
uq_wallet_tx_transfer_legs) -- pre-existing model/schema drift, not part of this
change. Those two wallet indexes are what make a deposit and a transfer
idempotent, so running that as generated would have quietly removed the
guarantee that stops a replayed poll double-crediting. Only the column add and
its backfill are kept here.

Revision ID: cf93dc5532bb
Revises: f31a48ad8c46
Create Date: 2026-08-24 20:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cf93dc5532bb'
down_revision: Union[str, Sequence[str], None] = 'f31a48ad8c46'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('rooms_created_at', sa.DateTime(), nullable=True))

    op.execute(
        "UPDATE draft_sessions "
        "SET rooms_created_at = COALESCE(teams_start_time, draft_start_time) "
        "WHERE draft_chat_channel IS NOT NULL AND rooms_created_at IS NULL"
    )


def downgrade() -> None:
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.drop_column('rooms_created_at')
