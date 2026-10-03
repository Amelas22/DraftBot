"""organizer and bracket message

The tournament records who runs it (organizer_user_id) and the pinned bracket
message it keeps current (bracket_message_id). Additive: nothing is dropped,
and every existing row keeps its values.

Revision ID: bracketmsg01
Revises: brackettree01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'bracketmsg01'
down_revision: Union[str, Sequence[str], None] = 'brackettree01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('tournaments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('organizer_user_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('bracket_message_id', sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tournaments', schema=None) as batch_op:
        batch_op.drop_column('bracket_message_id')
        batch_op.drop_column('organizer_user_id')
