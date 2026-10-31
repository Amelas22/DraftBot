"""let a tournament lend its players the library for free

Additive: one boolean column that defaults to off, so no existing tournament
changes until somebody turns it on with scripts/library_tool.py.

Revision ID: libexempt01
Revises: matchbook01
Create Date: 2026-10-31
"""
from alembic import op
import sqlalchemy as sa

revision = 'libexempt01'
down_revision = 'matchbook01'
branch_labels = None
depends_on = None


def _tournament_columns():
    bind = op.get_bind()
    return {c['name'] for c in sa.inspect(bind).get_columns('tournaments')}


def upgrade():
    if 'library_exempt' not in _tournament_columns():
        op.add_column('tournaments', sa.Column('library_exempt', sa.Boolean(), nullable=False,
                                               server_default=sa.text('0')))


def downgrade():
    if 'library_exempt' in _tournament_columns():
        with op.batch_alter_table('tournaments', schema=None) as batch_op:
            batch_op.drop_column('library_exempt')
