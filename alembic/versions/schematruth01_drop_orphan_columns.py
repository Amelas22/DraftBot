"""drop the two orphan columns autogenerate keeps proposing

Both exist in the database and in no model, which is what makes every
autogenerate run propose dropping them -- alongside three indexes that must
NOT be dropped, in a diff the author did not write. Removing the columns for
real is what stops that.

    match_results.guild_id    NULL on all 43,999 prod rows, no reader.
    mtgo_accounts.verified    NULL on all 117 prod rows, no reader.

Neither carries information. guild_id was already judged dead and dropped by
dropguild01 (August 2026), whose analysis is worth reading -- every guild-scoped
query joins DraftSession, which is the source of truth. But the column is still
present in production at a revision well past that migration, so alembic
believes the drop ran and the schema disagrees. This re-applies it, and the
neighbouring mtgo_accounts.verified with it.

Written to tolerate either state: a database where dropguild01 did take effect
skips that column instead of failing. Nothing is destroyed on any database --
both columns are entirely NULL -- so this needs no backup step, unlike a
migration that removes data.

Revision ID: schematruth01
Revises: cf93dc5532bb
"""
import sqlalchemy as sa
from alembic import op

revision = 'schematruth01'
down_revision = 'cf93dc5532bb'
branch_labels = None
depends_on = None


def _columns(table):
    bind = op.get_bind()
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade():
    if "guild_id" in _columns("match_results"):
        with op.batch_alter_table("match_results") as batch:
            batch.drop_column("guild_id")

    if "verified" in _columns("mtgo_accounts"):
        with op.batch_alter_table("mtgo_accounts") as batch:
            batch.drop_column("verified")


def downgrade():
    """Restores the shape, not the contents -- there were none to restore.

    Guarded the same way the upgrade is, so a rerun on a database that already
    has one of the columns does not fail halfway and leave the other undone.
    """
    if "verified" not in _columns("mtgo_accounts"):
        with op.batch_alter_table("mtgo_accounts") as batch:
            batch.add_column(sa.Column("verified", sa.Boolean(), nullable=True))
    if "guild_id" not in _columns("match_results"):
        with op.batch_alter_table("match_results") as batch:
            batch.add_column(sa.Column("guild_id", sa.String(length=64), nullable=True))
