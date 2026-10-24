"""scheduler: usual weeks and issued links

Additive: two new tables, each created only if absent.

Revision ID: usualweek01
Revises: tourneysite01
Create Date: 2026-10-22
"""
from alembic import op
import sqlalchemy as sa

revision = 'usualweek01'
down_revision = 'tourneysite01'
branch_labels = None
depends_on = None


def _tables():
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade():
    tables = _tables()
    if 'player_availability' not in tables:
        op.create_table(
            'player_availability',
            sa.Column('user_id', sa.String(64), primary_key=True),
            sa.Column('zone', sa.String(64), nullable=False),
            sa.Column('cells', sa.LargeBinary(42), nullable=False),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('stored_utc', sa.DateTime(), nullable=False),
        )
    if 'scheduler_link' not in tables:
        op.create_table(
            'scheduler_link',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('token', sa.String(32), nullable=False, unique=True),
            sa.Column('user_id', sa.String(64), nullable=False),
            sa.Column('guild_id', sa.String(64), nullable=False),
            sa.Column('view', sa.String(16), nullable=False),
            sa.Column('issued_utc', sa.DateTime(), nullable=False),
            sa.Column('expires_utc', sa.DateTime(), nullable=False),
            sa.Column('removed_utc', sa.DateTime(), nullable=True),
        )
        op.create_index('ix_scheduler_link_user_id', 'scheduler_link', ['user_id'])
        op.create_index('ix_scheduler_link_expires_utc', 'scheduler_link', ['expires_utc'])


def downgrade():
    tables = _tables()
    if 'scheduler_link' in tables:
        op.drop_table('scheduler_link')
    if 'player_availability' in tables:
        op.drop_table('player_availability')
