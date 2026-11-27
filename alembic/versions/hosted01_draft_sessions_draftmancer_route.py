"""Record which Draftmancer path each draft runs on

Adds draft_sessions.draftmancer_route: 'hosted' for the self-hosted control
API, 'fallback' for a draft created on the websocket manager because the API
was down. NULL (every existing row) is the legacy manager.

Revision ID: hosted01
Revises: confirmround01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'hosted01'
down_revision: Union[str, Sequence[str], None] = 'confirmround01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('draftmancer_route', sa.String(length=16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.drop_column('draftmancer_route')
