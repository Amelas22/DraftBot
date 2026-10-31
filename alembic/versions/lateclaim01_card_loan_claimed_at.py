"""date a deck asked for after its draft from the asking

Additive: one nullable column. Every existing loan is NULL, which is what a deck
handed out with its draft is, so nothing about any of them changes.

Revision ID: lateclaim01
Revises: libexempt01
Create Date: 2026-10-31
"""
from alembic import op
import sqlalchemy as sa

revision = 'lateclaim01'
down_revision = 'libexempt01'
branch_labels = None
depends_on = None


def _loan_columns():
    bind = op.get_bind()
    return {c['name'] for c in sa.inspect(bind).get_columns('card_loans')}


def upgrade():
    if 'claimed_at' not in _loan_columns():
        op.add_column('card_loans', sa.Column('claimed_at', sa.DateTime(), nullable=True))


def downgrade():
    if 'claimed_at' in _loan_columns():
        with op.batch_alter_table('card_loans', schema=None) as batch_op:
            batch_op.drop_column('claimed_at')
