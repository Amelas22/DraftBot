"""add drafttable_url to draft_sessions

Records where a draft's published table page actually landed. Not derivable
from friendly_id: that is not unique (festering-newt-77 and songstitcher-23
each appear twice), so a colliding draft gets a suffixed filename and the URL
is the only record of which one it got. It also makes "this draft has a page"
a fact rather than an inference from a URL pattern that might 404.

Revision ID: drafttableurl
Revises: 5d58521b7531
Create Date: 2026-09-04
"""
from alembic import op
import sqlalchemy as sa

revision = "drafttableurl"
down_revision = "5d58521b7531"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("draft_sessions") as batch_op:
        batch_op.add_column(sa.Column("drafttable_url", sa.String(512), nullable=True))


def downgrade():
    with op.batch_alter_table("draft_sessions") as batch_op:
        batch_op.drop_column("drafttable_url")
