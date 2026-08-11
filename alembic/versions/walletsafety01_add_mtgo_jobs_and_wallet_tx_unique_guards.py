"""add mtgo_jobs table and wallet_tx idempotency unique indexes

Durability + concurrency hardening for the money paths (from the escrow review):

* ``mtgo_jobs`` — durable record of every started serve job, so a startup
  resumer can finish booking trades that completed after a poll timeout or
  across a bot restart.

* Partial UNIQUE indexes on ``wallet_tx`` turn the app-level SELECT-then-insert
  idempotency checks into database guarantees (concurrent duplicate handlers
  now conflict instead of double-booking):
    - one settled booking per (job_id, kind)         — deposit/withdraw credits
    - one leg per (source, kind) for pay/receive     — internal pays, payouts,
      prize reallocations (also kills duplicate prize credits from a racing
      double /tournament start)
    - one live escrow reserve per source             — concurrent registers

Revision ID: walletsafety01
Revises: tourneypay01
Create Date: 2026-08-11
"""
from alembic import op
import sqlalchemy as sa

revision = 'walletsafety01'
down_revision = 'tourneypay01'
branch_labels = None
depends_on = None


def _has_table(name):
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def upgrade():
    if not _has_table('mtgo_jobs'):
        op.create_table(
            'mtgo_jobs',
            sa.Column('job_id', sa.String(64), primary_key=True, nullable=False),
            sa.Column('kind', sa.String(16), nullable=False),
            sa.Column('guild_id', sa.String(64), nullable=False),
            sa.Column('player_id', sa.String(64), nullable=False),
            sa.Column('mtgo_user', sa.String(128), nullable=False),
            sa.Column('amount', sa.Integer(), nullable=False),
            sa.Column('reserve_tx_id', sa.Integer(), nullable=True),
            sa.Column('context', sa.String(64), nullable=True),
            sa.Column('status', sa.String(16), nullable=False, server_default='pending'),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('resolved_at', sa.DateTime(), nullable=True),
        )
        op.create_index('ix_mtgo_jobs_status', 'mtgo_jobs', ['status'])

    # Partial unique indexes (SQLite supports WHERE on CREATE INDEX).
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_tx_job_kind "
        "ON wallet_tx (job_id, kind) WHERE job_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_tx_transfer_legs "
        "ON wallet_tx (source, kind) "
        "WHERE kind IN ('pay', 'receive') AND source IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_tx_live_escrow "
        "ON wallet_tx (source) WHERE kind = 'escrow' AND status = 'pending'"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS uq_wallet_tx_live_escrow")
    op.execute("DROP INDEX IF EXISTS uq_wallet_tx_transfer_legs")
    op.execute("DROP INDEX IF EXISTS uq_wallet_tx_job_kind")
    if _has_table('mtgo_jobs'):
        op.drop_table('mtgo_jobs')
