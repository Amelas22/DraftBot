"""add the escrow_holds table

Additive: a new table, nothing existing is touched. Guarded so a database that
already has it (created from the models) is left alone.

Revision ID: escrowhold01
Revises: loanoverdue01
Create Date: 2026-11-13
"""
from alembic import op
import sqlalchemy as sa

revision = 'escrowhold01'
down_revision = 'loanoverdue01'
branch_labels = None
depends_on = None


def _has_table():
    return 'escrow_holds' in sa.inspect(op.get_bind()).get_table_names()


def upgrade():
    if _has_table():
        return
    op.create_table(
        'escrow_holds',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('guild_id', sa.String(64), nullable=False),
        sa.Column('funder_id', sa.String(64), nullable=False),
        sa.Column('counterparty_id', sa.String(64), nullable=False),
        sa.Column('amount', sa.Integer(), nullable=False),
        sa.Column('note', sa.String(200), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('decided_by', sa.String(64), nullable=True),
        sa.Column('decided_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_escrow_holds_guild_status', 'escrow_holds',
                    ['guild_id', 'status'])


def downgrade():
    # Dropping the table strands the ledger rows of every ``escrow:hold:<id>`` holder:
    # their tix stay in wallet_tx with no record saying whose they are. Re-upgrading
    # would also restart the ids at 1 and collide with the existing ``escrow:<id>:fund``
    # sources. Do not run this on a database that has ever held an escrow.
    if _has_table():
        op.drop_table('escrow_holds')
