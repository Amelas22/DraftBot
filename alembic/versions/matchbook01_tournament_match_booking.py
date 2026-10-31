"""tournament match booking: a booked draft time on each match

Additive: four columns on tournament_matches, each added only if absent.

Revision ID: matchbook01
Revises: teamdraft01
Create Date: 2026-10-29
"""
from alembic import op
import sqlalchemy as sa

revision = 'matchbook01'
down_revision = 'teamdraft01'
branch_labels = None
depends_on = None

TABLE = 'tournament_matches'


def _columns():
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        return None
    return {c['name'] for c in inspector.get_columns(TABLE)}


def upgrade():
    cols = _columns()
    if cols is None:
        return
    with op.batch_alter_table(TABLE) as batch:
        if 'booked_start_utc' not in cols:
            batch.add_column(sa.Column('booked_start_utc', sa.DateTime(), nullable=True))
        if 'booked_by' not in cols:
            batch.add_column(sa.Column('booked_by', sa.String(64), nullable=True))
        if 'booked_by_name' not in cols:
            batch.add_column(sa.Column('booked_by_name', sa.String(100), nullable=True))
        if 'booking_version' not in cols:
            batch.add_column(sa.Column('booking_version', sa.Integer(), nullable=False, server_default='0'))


def downgrade():
    cols = _columns()
    if cols is None:
        return
    with op.batch_alter_table(TABLE) as batch:
        for name in ('booking_version', 'booked_by_name', 'booked_by', 'booked_start_utc'):
            if name in cols:
                batch.drop_column(name)
