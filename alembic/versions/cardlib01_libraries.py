"""the card library: what one is, who it serves, and who may borrow from it

A library is the unit people contribute to. Somebody who deposits into Cube
Night is lending to Cube Night's members wherever they play, and to nobody
else -- the partition is the library, not the Discord server. A library serves
any number of servers; a server draws on exactly one.

Four tables and no data:

  library         the pool itself, its kind and what borrowing costs
  library_server  which library a server draws on (the guild is the key)
  library_cube    which cubes a library offers to draft from
  library_member  who may borrow, where a library is not open to all

Nothing is backfilled because there is nothing to backfill: a library exists
only once somebody creates one with scripts/library_tool.py, which is also the
only way to bind a server to it. That is deliberate -- a server admin can write
their own guild config through the bot, so anything they can reach is a number
they can lower.

Revision ID: cardlib01
Revises: tourneydrop01
Create Date: 2026-09-23
"""
import sqlalchemy as sa
from alembic import op

revision = 'cardlib01'
down_revision = 'tourneydrop01'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'library',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('name', sa.String(128), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False,
                  server_default=sa.text("'rental'")),
        sa.Column('collateral_tix', sa.Integer(), nullable=False,
                  server_default=sa.text('0')),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('created_by', sa.String(64), nullable=True),
    )
    op.create_table(
        'library_server',
        sa.Column('guild_id', sa.String(64), primary_key=True),
        sa.Column('library_id', sa.String(64), nullable=False),
        sa.Column('bound_at', sa.DateTime(), nullable=True),
        sa.Column('bound_by', sa.String(64), nullable=True),
    )
    op.create_table(
        'library_cube',
        sa.Column('library_id', sa.String(64), primary_key=True),
        sa.Column('cube_id', sa.String(128), primary_key=True),
        sa.Column('added_at', sa.DateTime(), nullable=True),
        sa.Column('added_by', sa.String(64), nullable=True),
    )
    op.create_table(
        'library_member',
        sa.Column('library_id', sa.String(64), primary_key=True),
        sa.Column('player_id', sa.String(64), primary_key=True),
        sa.Column('added_at', sa.DateTime(), nullable=True),
        sa.Column('added_by', sa.String(64), nullable=True),
    )


def downgrade() -> None:
    for table in ('library_member', 'library_cube', 'library_server', 'library'):
        op.drop_table(table)
