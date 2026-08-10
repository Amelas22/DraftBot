"""add friendly_id to draft_session

friendly_id is a human-readable id like "lightning-bolt-7", picked from the
Magic card name pool in helpers/friendly_id.py. It replaces the Draftmancer
draft_id in the draft footer/embeds -- draft_id can be regenerated mid-draft
on reconnect, so it isn't a stable identifier for a whole draft's lifetime.
It's just a display label, not looked up or relied on for uniqueness, so
occasional duplicates are fine and not worth checking for.

Revision ID: cc4fcb545e83
Revises: trophreguess0
Create Date: 2026-07-29 15:54:07.961196
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from helpers.friendly_id import get_friendly_id

# revision identifiers, used by Alembic.
revision: str = 'cc4fcb545e83'
down_revision: Union[str, Sequence[str], None] = 'trophreguess0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _backfill_friendly_ids(connection) -> None:
    """Assign a friendly_id to every draft_sessions row missing one."""
    rows = connection.execute(
        sa.text("SELECT id FROM draft_sessions WHERE friendly_id IS NULL")
    ).fetchall()
    for (row_id,) in rows:
        connection.execute(
            sa.text("UPDATE draft_sessions SET friendly_id = :friendly_id WHERE id = :id"),
            {"friendly_id": get_friendly_id(), "id": row_id},
        )


def upgrade() -> None:
    """Add friendly_id and backfill every existing session with one."""
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('friendly_id', sa.String(length=32), nullable=True))

    _backfill_friendly_ids(op.get_bind())


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.drop_column('friendly_id')
