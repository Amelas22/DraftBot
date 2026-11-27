"""add tournaments.round_breaks and give Lotus Open Season 2 its Thanksgiving break

Additive: one nullable column, nothing existing is touched. The live season,
Lotus Open Season 2, gets Sunday 2026-11-29 as a break, so Round 4 ends on
2026-12-06 and Rounds 5-7 each move back a week; a database without that row
is left as it was.

Revision ID: roundbreaks01
Revises: hosted02
Create Date: 2026-11-27
"""
import logging

from alembic import op
import sqlalchemy as sa

revision = 'roundbreaks01'
down_revision = 'hosted02'
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")


def upgrade():
    with op.batch_alter_table('tournaments') as batch:
        batch.add_column(sa.Column('round_breaks', sa.JSON(), nullable=True))
    result = op.get_bind().execute(sa.text(
        "UPDATE tournaments SET round_breaks = '[\"2026-11-29\"]' "
        "WHERE name = 'Lotus Open Season 2' AND guild_id = '1355718878298116096'"
    ))
    log.info("round_breaks backfilled on %s tournament row(s)", result.rowcount)


def downgrade():
    with op.batch_alter_table('tournaments') as batch:
        batch.drop_column('round_breaks')
