"""add tournaments.round1_deadline and backfill Lotus Open Season 2

Additive: one nullable column, nothing existing is touched. The live season,
Lotus Open Season 2, gets Round 1's deadline (Sunday 2026-11-08); a database
without that row is left as it was.

Revision ID: round1dl01
Revises: escrowhold01
Create Date: 2026-11-16
"""
import logging

from alembic import op
import sqlalchemy as sa

revision = 'round1dl01'
down_revision = 'escrowhold01'
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")


def upgrade():
    with op.batch_alter_table('tournaments') as batch:
        batch.add_column(sa.Column('round1_deadline', sa.Date(), nullable=True))
    result = op.get_bind().execute(sa.text(
        "UPDATE tournaments SET round1_deadline = '2026-11-08' "
        "WHERE name = 'Lotus Open Season 2' AND guild_id = '1355718878298116096'"
    ))
    log.info("round1_deadline backfilled on %s tournament row(s)", result.rowcount)


def downgrade():
    with op.batch_alter_table('tournaments') as batch:
        batch.drop_column('round1_deadline')
