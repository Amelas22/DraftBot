"""add pools destination id columns to draft sessions

Task: aggregate each team's drafted pools into a thread.
team_a/b_pools_destination_id record WHERE a team's pools are being delivered
-- the pools thread, or the team channel itself when Discord refused a thread.
Whichever one carried a pool is stored, and it is the record of who has
posted, so a retry resumes into the same place instead of re-posting or
opening a second destination.

Revision ID: 31793cd109c4
Revises: 748aae6ae438
Create Date: 2026-08-22 02:48:48.725434

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '31793cd109c4'
down_revision: Union[str, Sequence[str], None] = '748aae6ae438'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('team_a_pools_destination_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('team_b_pools_destination_id', sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.drop_column('team_b_pools_destination_id')
        batch_op.drop_column('team_a_pools_destination_id')
