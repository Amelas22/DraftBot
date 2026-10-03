"""add draw_number to tournament participants

The standings' last tiebreak (see TournamentParticipant.draw_number). Active
tournaments get a fresh random draw here; others stay NULL (a start draws its
own). Additive and nullable: nothing is dropped.

Revision ID: drawnum01
Revises: poolmatch01
"""
import random
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'drawnum01'
down_revision: Union[str, Sequence[str], None] = 'poolmatch01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('tournament_participants', schema=None) as batch_op:
        batch_op.add_column(sa.Column('draw_number', sa.Integer(), nullable=True))

    bind = op.get_bind()
    rng = random.SystemRandom()
    active = bind.execute(sa.text("SELECT id FROM tournaments WHERE status = 'active'")).scalars().all()
    for tournament_id in active:
        ids = bind.execute(sa.text(
            "SELECT id FROM tournament_participants WHERE tournament_id = :t ORDER BY id"
        ), {"t": tournament_id}).scalars().all()
        numbers = list(range(1, len(ids) + 1))
        rng.shuffle(numbers)
        for participant_id, number in zip(ids, numbers):
            bind.execute(sa.text(
                "UPDATE tournament_participants SET draw_number = :n WHERE id = :p"
            ), {"n": number, "p": participant_id})


def downgrade() -> None:
    with op.batch_alter_table('tournament_participants', schema=None) as batch_op:
        batch_op.drop_column('draw_number')
