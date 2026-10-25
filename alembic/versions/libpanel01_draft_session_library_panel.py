"""draft_sessions: library panel message and mode

Additive: two nullable columns, each added only if absent.

Revision ID: libpanel01
Revises: lfgteams01
Create Date: 2026-10-25
"""
from alembic import op
import sqlalchemy as sa

revision = 'libpanel01'
down_revision = 'lfgteams01'
branch_labels = None
depends_on = None


def _columns():
    return {c['name'] for c in sa.inspect(op.get_bind()).get_columns('draft_sessions')}


def upgrade():
    cols = _columns()
    with op.batch_alter_table('draft_sessions') as batch:
        if 'library_panel_message_id' not in cols:
            batch.add_column(sa.Column('library_panel_message_id', sa.String(64), nullable=True))
        if 'library_panel_mode' not in cols:
            batch.add_column(sa.Column('library_panel_mode', sa.String(16), nullable=True))


def downgrade():
    cols = _columns()
    with op.batch_alter_table('draft_sessions') as batch:
        if 'library_panel_mode' in cols:
            batch.drop_column('library_panel_mode')
        if 'library_panel_message_id' in cols:
            batch.drop_column('library_panel_message_id')
