"""add draw_number to tournament participants

The standings' last tiebreak (see TournamentParticipant.draw_number). Active
tournaments get a fresh random draw here; others stay NULL (a start draws its
own). Additive and nullable: nothing is dropped.

Revision ID: drawnum01
Revises: poolmatch01
"""
import logging
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
    # Logged in full: this draw can decide a cut seat, and the journal is the
    # only record of what it was and when it was made.
    log = logging.getLogger("alembic.runtime.migration")
    active = bind.execute(sa.text(
        "SELECT id, name FROM tournaments WHERE status = 'active'")).all()
    for tournament_id, name in active:
        rows = bind.execute(sa.text(
            "SELECT id, team_name FROM tournament_participants "
            "WHERE tournament_id = :t ORDER BY id"
        ), {"t": tournament_id}).all()
        numbers = list(range(1, len(rows) + 1))
        rng.shuffle(numbers)
        for (participant_id, _), number in zip(rows, numbers):
            bind.execute(sa.text(
                "UPDATE tournament_participants SET draw_number = :n WHERE id = :p"
            ), {"n": number, "p": participant_id})
        drawn = sorted(zip(numbers, (team for _, team in rows)))
        log.info(f"draw_number for tournament {tournament_id} ({name}): "
                 + ", ".join(f"{n}={team}" for n, team in drawn))


def downgrade() -> None:
    with op.batch_alter_table('tournament_participants', schema=None) as batch_op:
        batch_op.drop_column('draw_number')
