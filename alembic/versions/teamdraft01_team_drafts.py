"""team drafts: matchups, applied actions, scheduler link subject

Additive: two new tables and one nullable column, each added only if absent.

Revision ID: teamdraft01
Revises: libpanel01
Create Date: 2026-10-26
"""
from alembic import op
import sqlalchemy as sa

revision = 'teamdraft01'
down_revision = 'libpanel01'
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def upgrade():
    tables = set(_inspector().get_table_names())
    if 'team_draft' not in tables:
        op.create_table(
            'team_draft',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('guild_id', sa.String(64), nullable=False),
            sa.Column('channel_id', sa.String(64), nullable=False),
            sa.Column('thread_id', sa.String(64), nullable=True),
            sa.Column('control_message_id', sa.String(64), nullable=True),
            sa.Column('name', sa.String(100), nullable=False),
            sa.Column('side_a_roles', sa.JSON(), nullable=False),
            sa.Column('side_a_users', sa.JSON(), nullable=False),
            sa.Column('side_b_roles', sa.JSON(), nullable=False),
            sa.Column('side_b_users', sa.JSON(), nullable=False),
            sa.Column('created_by', sa.String(64), nullable=False),
            sa.Column('created_utc', sa.DateTime(), nullable=False),
            sa.Column('booked_start_utc', sa.DateTime(), nullable=True),
            sa.Column('booked_by', sa.String(64), nullable=True),
            sa.Column('booked_by_name', sa.String(100), nullable=True),
            sa.Column('booking_version', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('draft_session_id', sa.String(64), nullable=True, unique=True),
        )
    if 'team_draft_action' not in tables:
        op.create_table(
            'team_draft_action',
            sa.Column('action_id', sa.String(64), primary_key=True),
            sa.Column('user_id', sa.String(64), nullable=False),
            sa.Column('result_json', sa.Text(), nullable=False),
            sa.Column('done_utc', sa.DateTime(), nullable=False),
        )
    if 'scheduler_link' in tables:
        cols = {c['name'] for c in _inspector().get_columns('scheduler_link')}
        if 'subject' not in cols:
            with op.batch_alter_table('scheduler_link') as batch:
                batch.add_column(sa.Column('subject', sa.String(64), nullable=True))


def downgrade():
    tables = set(_inspector().get_table_names())
    if 'scheduler_link' in tables:
        cols = {c['name'] for c in _inspector().get_columns('scheduler_link')}
        if 'subject' in cols:
            with op.batch_alter_table('scheduler_link') as batch:
                batch.drop_column('subject')
    for name in ('team_draft_action', 'team_draft'):
        if name in tables:
            op.drop_table(name)
