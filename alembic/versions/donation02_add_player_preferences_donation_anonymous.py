"""Donation page anonymity

Adds player_preferences.donation_anonymous. A donor who sets it shows as
"Anonymous" on the public donation page.

Additive only: nullable with no default, because the row is shared with
unrelated answers and creating it for one must not decide this one. NULL reads
as named.

Revision ID: donation02
Revises: donation01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'donation02'
down_revision: Union[str, Sequence[str], None] = 'donation01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('player_preferences', schema=None) as batch_op:
        batch_op.add_column(sa.Column('donation_anonymous', sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('player_preferences', schema=None) as batch_op:
        batch_op.drop_column('donation_anonymous')
