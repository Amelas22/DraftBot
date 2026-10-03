"""bracket feeder links

A bracket is built whole at the cut: each match names the match its winner
moves into (feeds_match_id, feeds_slot), and a match waiting on its feeders
has empty team slots -- so team_a_participant_id becomes nullable. The
tournament records the channel the bracket posts into. Additive: nothing is
dropped, and every existing row keeps its values.

Revision ID: brackettree01
Revises: drawnum01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'brackettree01'
down_revision: Union[str, Sequence[str], None] = 'drawnum01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('tournament_matches', schema=None) as batch_op:
        batch_op.alter_column('team_a_participant_id', existing_type=sa.Integer(), nullable=True)
        batch_op.add_column(sa.Column('feeds_match_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('feeds_slot', sa.String(length=1), nullable=True))
        batch_op.create_foreign_key('fk_match_feeds_match', 'tournament_matches',
                                    ['feeds_match_id'], ['id'])
    with op.batch_alter_table('tournaments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('bracket_channel_id', sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tournaments', schema=None) as batch_op:
        batch_op.drop_column('bracket_channel_id')
    with op.batch_alter_table('tournament_matches', schema=None) as batch_op:
        batch_op.drop_constraint('fk_match_feeds_match', type_='foreignkey')
        batch_op.drop_column('feeds_slot')
        batch_op.drop_column('feeds_match_id')
        batch_op.alter_column('team_a_participant_id', existing_type=sa.Integer(), nullable=False)
