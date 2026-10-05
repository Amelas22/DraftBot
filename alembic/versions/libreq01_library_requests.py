"""library requests on a draft session

Who asked for library cards for a draft, so the shelf can be held for it from
sign-up rather than from the moment teams form. A list of Discord ids; an
ACTIVE requester is one who is also still in sign_ups, which is what makes the
hold release itself when the last of them leaves.

Additive: nothing is dropped, and every existing row keeps its values. A row
with NULL here has nobody requesting, which is the same as the behaviour before
this column existed.

Revision ID: libreq01
Revises: bracketmsg01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'libreq01'
down_revision: Union[str, Sequence[str], None] = 'bracketmsg01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('library_requests', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.drop_column('library_requests')
