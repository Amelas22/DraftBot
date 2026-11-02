"""scheduler: dated availability overrides

Additive: one new table, created only if absent.

Revision ID: dayoverride01
Revises: lateclaim01
Create Date: 2026-11-02
"""
from alembic import op
import sqlalchemy as sa

revision = 'dayoverride01'
down_revision = 'lateclaim01'
branch_labels = None
depends_on = None


def upgrade():
    if 'player_day_override' not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            'player_day_override',
            sa.Column('user_id', sa.String(64), primary_key=True),
            sa.Column('local_date', sa.Date(), primary_key=True),
            sa.Column('zone', sa.String(64), nullable=False),
            sa.Column('cells', sa.LargeBinary(6), nullable=False),
            sa.Column('stored_utc', sa.DateTime(), nullable=False),
        )


def downgrade():
    if 'player_day_override' in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table('player_day_override')
