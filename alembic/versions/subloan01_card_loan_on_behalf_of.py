"""record whose pool a library loan carries when a sub borrows it

Additive: one nullable column. Every existing loan is NULL, which is what a loan
of the borrower's own pool is, so nothing about any of them changes.

Revision ID: subloan01
Revises: smallhold01
Create Date: 2026-11-05
"""
from alembic import op
import sqlalchemy as sa

revision = 'subloan01'
down_revision = 'smallhold01'
branch_labels = None
depends_on = None


def _loan_columns():
    bind = op.get_bind()
    return {c['name'] for c in sa.inspect(bind).get_columns('card_loans')}


def upgrade():
    if 'on_behalf_of' not in _loan_columns():
        op.add_column('card_loans', sa.Column('on_behalf_of', sa.String(64), nullable=True))


def downgrade():
    if 'on_behalf_of' in _loan_columns():
        with op.batch_alter_table('card_loans', schema=None) as batch_op:
            batch_op.drop_column('on_behalf_of')
