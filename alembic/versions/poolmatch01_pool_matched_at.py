"""add pool_matched_at to draft sessions

The record that a draft's prize pool was matched. match_pool sets it in the
same transaction that books its refunds, so a matched pool and its stamp cannot
exist without each other. Startup recovery reads it to tell a draft a crash
left between committing its teams and matching its pool -- which it returns to
sign-ups -- from a running draft whose sides went unequal because a player was
removed after teams formed, which it must leave alone.

Backfilled for every session already past sign-ups. A deploy is a restart, so
without it every existing draft at 'teams' would read as unmatched the moment
this lands, and the first startup would return running drafts to sign-ups.
teams_start_time is the stamp where it exists (draft_start_time otherwise);
the exact value never matters, only that it is set. Additive and nullable:
nothing is dropped, so no data is at risk on upgrade.

Revision ID: poolmatch01
Revises: libremind01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'poolmatch01'
down_revision: Union[str, Sequence[str], None] = 'libremind01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('pool_matched_at', sa.DateTime(), nullable=True))
    op.execute(
        "UPDATE draft_sessions "
        "SET pool_matched_at = COALESCE(teams_start_time, draft_start_time) "
        "WHERE session_stage IS NOT NULL")


def downgrade() -> None:
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.drop_column('pool_matched_at')
