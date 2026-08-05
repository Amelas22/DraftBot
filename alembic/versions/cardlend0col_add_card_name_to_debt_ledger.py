"""add card_name to debt_ledger (multi-entity: NULL = tix, set = that card)

Card loans live in the same double-entry ledger as tix debts: `amount` is a
signed quantity of the entry's entity, and `card_name` names the entity
(NULL = tix, which every pre-existing row is). Schema-only; no backfill.

Revision ID: cardlend0col
Revises: legacyimport0
Create Date: 2026-08-05
"""
import sqlalchemy as sa
from alembic import op

revision = "cardlend0col"
down_revision = "legacyimport0"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("debt_ledger", sa.Column("card_name", sa.String(128), nullable=True))


def downgrade():
    op.drop_column("debt_ledger", "card_name")
