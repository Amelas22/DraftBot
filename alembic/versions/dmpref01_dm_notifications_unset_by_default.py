"""DM notifications are unset until the player chooses

`player_preferences.dm_notifications` loses its server default of 0. A row
created for another answer (the entry cap, the library question) used to get
DMs off from that default, overriding the guild default of on. A new row now
leaves it NULL, which reads as the guild default.

Schema only: no existing row changes value.

Revision ID: dmpref01
Revises: vaultcheck01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'dmpref01'
down_revision: Union[str, Sequence[str], None] = 'vaultcheck01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('player_preferences', schema=None) as batch_op:
        batch_op.alter_column('dm_notifications', existing_type=sa.Boolean(),
                              existing_nullable=True, server_default=None)


def downgrade() -> None:
    with op.batch_alter_table('player_preferences', schema=None) as batch_op:
        batch_op.alter_column('dm_notifications', existing_type=sa.Boolean(),
                              existing_nullable=True, server_default=sa.text('0'))
