"""add mtgo_jobs.card_name for house card lending

A job that moves CARDS instead of tix. NULL means the job is tix, which is every row
written before house lending existed, so nothing needs backfilling and every existing
query keeps its meaning. When set, ``amount`` is the number of copies.

The same nullable-discriminator shape ``debt_ledger.card_name`` already uses, and for the
same reason: one table, two entity kinds, distinguished by whether the name is present.

Deliberately NOT a printing column. The MTGO serve records which printing actually crossed
and pins it itself when the cards come back, so DraftBot never needs to know one — that is
what makes lending by name safe to recall exactly.

Revision ID: cardloan01
Revises: tourneydrop01
Create Date: 2026-09-12
"""
from alembic import op
import sqlalchemy as sa

revision = 'cardloan01'
down_revision = 'tourneydrop01'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('mtgo_jobs', sa.Column('card_name', sa.String(length=128), nullable=True))


def downgrade():
    op.drop_column('mtgo_jobs', 'card_name')
