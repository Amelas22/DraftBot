"""Add the library_hold table for time-boxed library holds

Additive: a new table, no existing rows touched. Downgrade drops it and the
holds it records.

Revision ID: libhold01
Revises: subloan01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'libhold01'
down_revision: Union[str, Sequence[str], None] = 'subloan01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'library_hold',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('library_id', sa.String(length=64), nullable=False),
        sa.Column('cube_id', sa.String(length=128), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('unit', sa.String(length=8), nullable=False),
        sa.Column('cards', sa.JSON(), nullable=True),
        sa.Column('starts_at', sa.DateTime(), nullable=False),
        sa.Column('ends_at', sa.DateTime(), nullable=False),
        sa.Column('draft_session_id', sa.String(), nullable=True),
        sa.Column('tournament_match_id', sa.Integer(), nullable=True),
        sa.Column('player_id', sa.String(length=64), nullable=True),
        sa.Column('released_at', sa.DateTime(), nullable=True),
        sa.Column('released_by', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_library_hold_library_id', 'library_hold', ['library_id'])
    op.create_index('ix_library_hold_starts_at', 'library_hold', ['starts_at'])
    op.create_index('ix_library_hold_draft_session_id', 'library_hold',
                    ['draft_session_id'])
    op.create_index('uq_library_hold_one_live_server_per_draft', 'library_hold',
                    ['draft_session_id'], unique=True,
                    sqlite_where=sa.text("released_at IS NULL AND kind = 'server'"))


def downgrade() -> None:
    op.drop_index('uq_library_hold_one_live_server_per_draft',
                  table_name='library_hold')
    op.drop_index('ix_library_hold_draft_session_id', table_name='library_hold')
    op.drop_index('ix_library_hold_starts_at', table_name='library_hold')
    op.drop_index('ix_library_hold_library_id', table_name='library_hold')
    op.drop_table('library_hold')
