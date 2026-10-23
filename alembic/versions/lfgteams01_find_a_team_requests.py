"""find-a-team: teams, members, join requests, applied actions

Additive: four new tables, each created only if absent.

Revision ID: lfgteams01
Revises: usualweek01
Create Date: 2026-10-23
"""
from alembic import op
import sqlalchemy as sa

revision = 'lfgteams01'
down_revision = 'usualweek01'
branch_labels = None
depends_on = None


def _tables():
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade():
    tables = _tables()
    if 'lfg_team' not in tables:
        op.create_table(
            'lfg_team',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('guild_id', sa.String(64), nullable=False),
            sa.Column('captain_id', sa.String(64), nullable=False),
            sa.Column('size', sa.Integer(), nullable=False),
            sa.Column('note', sa.String(200), nullable=False),
            sa.Column('taking_requests', sa.Boolean(), nullable=False),
            sa.Column('status', sa.String(16), nullable=False),
            sa.Column('created_utc', sa.DateTime(), nullable=False),
            sa.Column('closed_utc', sa.DateTime(), nullable=True),
        )
        op.create_index('ix_lfg_team_guild_id', 'lfg_team', ['guild_id'])
        op.create_index('ix_lfg_team_captain_id', 'lfg_team', ['captain_id'])
    if 'lfg_team_member' not in tables:
        op.create_table(
            'lfg_team_member',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('team_id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.String(64), nullable=False),
            sa.Column('display_name', sa.String(100), nullable=False),
            sa.Column('joined_utc', sa.DateTime(), nullable=False),
            sa.Column('left_utc', sa.DateTime(), nullable=True),
        )
        op.create_index('ix_lfg_team_member_team_id', 'lfg_team_member', ['team_id'])
        op.create_index('ix_lfg_team_member_user_id', 'lfg_team_member', ['user_id'])
        op.create_index('uq_lfg_member_current', 'lfg_team_member', ['team_id', 'user_id'],
                        unique=True, sqlite_where=sa.text('left_utc IS NULL'))
    if 'lfg_request' not in tables:
        op.create_table(
            'lfg_request',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('team_id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.String(64), nullable=False),
            sa.Column('display_name', sa.String(100), nullable=False),
            sa.Column('note', sa.String(200), nullable=False),
            sa.Column('status', sa.String(16), nullable=False),
            sa.Column('created_utc', sa.DateTime(), nullable=False),
            sa.Column('answered_utc', sa.DateTime(), nullable=True),
        )
        op.create_index('ix_lfg_request_team_id', 'lfg_request', ['team_id'])
        op.create_index('ix_lfg_request_user_id', 'lfg_request', ['user_id'])
        op.create_index('uq_lfg_request_pending', 'lfg_request', ['team_id', 'user_id'],
                        unique=True, sqlite_where=sa.text("status = 'pending'"))
    if 'lfg_action' not in tables:
        op.create_table(
            'lfg_action',
            sa.Column('action_id', sa.String(64), primary_key=True),
            sa.Column('user_id', sa.String(64), nullable=False),
            sa.Column('result_json', sa.Text(), nullable=False),
            sa.Column('done_utc', sa.DateTime(), nullable=False),
        )


def downgrade():
    tables = _tables()
    for name in ('lfg_action', 'lfg_request', 'lfg_team_member', 'lfg_team'):
        if name in tables:
            op.drop_table(name)
