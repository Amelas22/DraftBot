"""add mtgo_accounts and wallet_tx tables

Revision ID: mtgowallet01
Revises: trophreguess0
Create Date: 2026-08-03

Adds the two tables backing the MTGO escrow/wallet feature:
  * mtgo_accounts — Discord id <-> MTGO username identity link.
  * wallet_tx     — append-only tix claim ledger (balance = SUM of 'done' rows).

Both are also in Base.metadata, so the bot's init_db() create_all() would create them
anyway; this migration keeps the alembic graph authoritative for prod (systemd runs
`alembic upgrade head` before start). Each create is guarded by an existence check so the
migration is a no-op if create_all() happened to run first on a given database — it can
never fail prod's ExecStartPre with a "table already exists" error.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'mtgowallet01'
down_revision: Union[str, Sequence[str], None] = 'trophreguess0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table('mtgo_accounts'):
        op.create_table(
            'mtgo_accounts',
            sa.Column('discord_user_id', sa.String(length=64), nullable=False),
            sa.Column('mtgo_username', sa.String(length=128), nullable=False),
            sa.Column('mtgo_username_lower', sa.String(length=128), nullable=False),
            sa.Column('verified', sa.Boolean(), nullable=True),
            sa.Column('linked_at', sa.DateTime(), nullable=True),
            sa.Column('guild_id', sa.String(length=64), nullable=True),
            sa.PrimaryKeyConstraint('discord_user_id'),
        )
        with op.batch_alter_table('mtgo_accounts', schema=None) as batch_op:
            batch_op.create_index(
                batch_op.f('ix_mtgo_accounts_mtgo_username_lower'),
                ['mtgo_username_lower'], unique=True)

    if not _has_table('wallet_tx'):
        op.create_table(
            'wallet_tx',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('guild_id', sa.String(length=64), nullable=False),
            sa.Column('player_id', sa.String(length=64), nullable=False),
            sa.Column('kind', sa.String(length=32), nullable=False),
            sa.Column('amount', sa.Integer(), nullable=False),
            sa.Column('status', sa.String(length=16), nullable=False),
            sa.Column('counterparty_id', sa.String(length=64), nullable=True),
            sa.Column('job_id', sa.String(length=64), nullable=True),
            sa.Column('source', sa.String(length=64), nullable=True),
            sa.Column('notes', sa.String(length=256), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
        )
        with op.batch_alter_table('wallet_tx', schema=None) as batch_op:
            batch_op.create_index(
                'ix_wallet_tx_balance_lookup', ['guild_id', 'player_id', 'status'], unique=False)
            batch_op.create_index(batch_op.f('ix_wallet_tx_guild_id'), ['guild_id'], unique=False)
            batch_op.create_index(batch_op.f('ix_wallet_tx_job_id'), ['job_id'], unique=False)
            batch_op.create_index(batch_op.f('ix_wallet_tx_player_id'), ['player_id'], unique=False)


def downgrade() -> None:
    if _has_table('wallet_tx'):
        with op.batch_alter_table('wallet_tx', schema=None) as batch_op:
            batch_op.drop_index(batch_op.f('ix_wallet_tx_player_id'))
            batch_op.drop_index(batch_op.f('ix_wallet_tx_job_id'))
            batch_op.drop_index(batch_op.f('ix_wallet_tx_guild_id'))
            batch_op.drop_index('ix_wallet_tx_balance_lookup')
        op.drop_table('wallet_tx')

    if _has_table('mtgo_accounts'):
        with op.batch_alter_table('mtgo_accounts', schema=None) as batch_op:
            batch_op.drop_index(batch_op.f('ix_mtgo_accounts_mtgo_username_lower'))
        op.drop_table('mtgo_accounts')
