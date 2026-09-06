"""add tournament_participants.dropped_at

Records that a team has left a running tournament, so the rounds still to be
paired leave it out while everything it already played stays where it is.

Deliberately its own column rather than a new ``status`` value: ``status``
answers whether the entry fee is held, and the escrow reads it. A team that
paid and then dropped is still 'paid', and folding the two together would make
it look unpaid to the money paths.

Additive and nullable, so every existing row reads as "still in" and nothing
needs backfilling.

Revision ID: tourneydrop01
Revises: drafttableurl
Create Date: 2026-09-06
"""
from alembic import op
import sqlalchemy as sa

revision = 'tourneydrop01'
down_revision = 'drafttableurl'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'tournament_participants',
        sa.Column('dropped_at', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('tournament_participants', 'dropped_at')
