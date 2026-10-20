"""add the tournament's league-site link

Additive: one nullable column, so existing tournaments simply have no link.

Revision ID: tourneysite01
Revises: walletasset01
Create Date: 2026-10-20
"""
from alembic import op
import sqlalchemy as sa

revision = 'tourneysite01'
down_revision = 'walletasset01'
branch_labels = None
depends_on = None


def _tournament_columns():
    bind = op.get_bind()
    return {c['name'] for c in sa.inspect(bind).get_columns('tournaments')}


def upgrade():
    if 'website_url' not in _tournament_columns():
        op.add_column('tournaments', sa.Column('website_url', sa.String(512), nullable=True))


def downgrade():
    if 'website_url' in _tournament_columns():
        with op.batch_alter_table('tournaments', schema=None) as batch_op:
            batch_op.drop_column('website_url')
