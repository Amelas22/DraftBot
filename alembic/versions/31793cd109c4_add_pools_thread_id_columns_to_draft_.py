"""add pools thread id columns to draft sessions

Task: aggregate each team's drafted pools into a thread. team_a/b_pools_thread_id
are set when the thread is created; the thread itself is the record of which
players have posted, so a retry resumes into it instead of re-posting or
starting a second one.

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
        batch_op.add_column(sa.Column('team_a_pools_thread_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('team_b_pools_thread_id', sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.drop_column('team_b_pools_thread_id')
        batch_op.drop_column('team_a_pools_thread_id')
