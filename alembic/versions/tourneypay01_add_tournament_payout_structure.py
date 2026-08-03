"""add tournament payout_structure

Revision ID: tourneypay01
Revises: tourneyescr01
Create Date: 2026-08-03

Adds tournaments.payout_structure — how the prize pool is split at payout
('winner_take_all' | 'top2' | 'top3' | 'top4'), declared at creation. server_default
'winner_take_all' grandfathers existing rows. A column on an existing table, so create_all
won't add it — this migration is load-bearing. Column-existence guarded for idempotency.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'tourneypay01'
down_revision: Union[str, Sequence[str], None] = 'tourneyescr01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table: str) -> set:
    return {c['name'] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if 'payout_structure' not in _columns('tournaments'):
        op.add_column('tournaments', sa.Column(
            'payout_structure', sa.String(length=32), nullable=False,
            server_default=sa.text("'winner_take_all'")))


def downgrade() -> None:
    if 'payout_structure' in _columns('tournaments'):
        with op.batch_alter_table('tournaments', schema=None) as batch_op:
            batch_op.drop_column('payout_structure')
