"""add tournament entry_fee and participant escrow gate

Revision ID: tourneyescr01
Revises: mtgowallet01
Create Date: 2026-08-03

Adds escrow support to tournaments:
  * tournaments.entry_fee            — tix entry fee (0 = free; grandfathers existing rows).
  * tournament_participants.status   — 'paid' | 'pending' (server_default 'paid' so every
                                       existing participant and every free tournament is
                                       already complete; new paid registrations set 'pending').
  * tournament_participants.paid_at  — when escrow was secured.

Unlike the wallet tables, these are COLUMNS on existing tables, which the bot's init_db
create_all() will NOT add — so this migration is load-bearing, not just bookkeeping. Each
add is guarded by a column-existence check for idempotency.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'tourneyescr01'
down_revision: Union[str, Sequence[str], None] = 'mtgowallet01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table: str) -> set:
    return {c['name'] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    tournament_cols = _columns('tournaments')
    if 'entry_fee' not in tournament_cols:
        op.add_column('tournaments', sa.Column(
            'entry_fee', sa.Integer(), nullable=False, server_default=sa.text('0')))

    participant_cols = _columns('tournament_participants')
    if 'status' not in participant_cols:
        op.add_column('tournament_participants', sa.Column(
            'status', sa.String(length=16), nullable=False, server_default=sa.text("'paid'")))
    if 'paid_at' not in participant_cols:
        op.add_column('tournament_participants', sa.Column(
            'paid_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    participant_cols = _columns('tournament_participants')
    drop_participant = [c for c in ('paid_at', 'status') if c in participant_cols]
    if drop_participant:
        with op.batch_alter_table('tournament_participants', schema=None) as batch_op:
            for col in drop_participant:
                batch_op.drop_column(col)

    if 'entry_fee' in _columns('tournaments'):
        with op.batch_alter_table('tournaments', schema=None) as batch_op:
            batch_op.drop_column('entry_fee')
